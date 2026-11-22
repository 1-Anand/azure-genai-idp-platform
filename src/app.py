import os
import re
import json
import time
import asyncio
import logging
import urllib.request
from collections import defaultdict
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, HTTPException, Security, Request, status
from fastapi.security import APIKeyHeader
from azure.core.credentials import AzureKeyCredential
from azure.identity import DefaultAzureCredential
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeDocumentRequest

# Configure structured enterprise logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("idp_engine")

# 1. Load environment variables from .env file
load_dotenv()

DOCINT_ENDPOINT = os.getenv("DOCINT_ENDPOINT")
DOCINT_KEY = os.getenv("DOCINT_KEY")
AOAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AOAI_KEY = os.getenv("AZURE_OPENAI_KEY")
AOAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5-mini")
IDP_API_KEY = os.getenv("IDP_API_KEY")

# Security, Concurrency & DoS Prevention Thresholds
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB maximum payload limit
ALLOWED_MEDIA_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/tiff",
    "image/bmp"
}

# Concurrency throttle: maximum 2 concurrent OCR/LLM operations in 0.5 GiB container
PROCESSING_SEMAPHORE = asyncio.Semaphore(2)

# IP-based sliding window rate limiter (max 15 requests per minute per IP)
RATE_LIMIT_WINDOW = 60.0  # seconds
RATE_LIMIT_MAX_REQUESTS = 15
_request_history = defaultdict(list)
_history_lock = asyncio.Lock()

async def enforce_rate_limit(request: Request):
    """
    In-memory rate limiter per client IP.
    Protects the container from denial-of-service floods and runaway API costs.
    """
    client_ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "unknown")
    now = time.time()
    async with _history_lock:
        timestamps = [t for t in _request_history[client_ip] if now - t < RATE_LIMIT_WINDOW]
        if len(timestamps) >= RATE_LIMIT_MAX_REQUESTS:
            logger.warning("Rate limit exceeded for client IP: %s (%d requests in window)", client_ip, len(timestamps))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. Maximum 15 requests per minute allowed."
            )
        timestamps.append(now)
        _request_history[client_ip] = timestamps

# 2. Strict Authentication Dependency (Fail-Closed by Default)
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def verify_api_key(api_key: str = Security(api_key_header)):
    """
    Fail-closed authentication check.
    - If IDP_API_KEY is missing/empty on the server, the endpoint is disabled (503).
    - If X-API-Key does not match IDP_API_KEY, access is rejected (401).
    """
    if not IDP_API_KEY or IDP_API_KEY.strip() == "":
        logger.error("Configuration error: IDP_API_KEY is not configured on server. Endpoint disabled.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service authentication is not configured. Data-plane processing is disabled."
        )
    if not api_key or api_key != IDP_API_KEY:
        logger.warning("Unauthorized access attempt: Invalid or missing X-API-Key header.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Invalid or missing X-API-Key header."
        )
    return api_key

# 3. Document Intelligence Client (Zero-Key Managed Identity in Cloud)
def get_docint_client() -> DocumentIntelligenceClient:
    if not DOCINT_ENDPOINT:
        raise ValueError("DOCINT_ENDPOINT environment variable is not set.")
    if DOCINT_KEY:
        return DocumentIntelligenceClient(
            endpoint=DOCINT_ENDPOINT,
            credential=AzureKeyCredential(DOCINT_KEY)
        )
    return DocumentIntelligenceClient(
        endpoint=DOCINT_ENDPOINT,
        credential=DefaultAzureCredential()
    )

# 4. Extract structured fields + Confidence Scores from prebuilt-invoice
def extract_invoice_fields(analyze_result) -> dict:
    documents = getattr(analyze_result, "documents", None)
    if not documents:
        return {"error": "No invoice document detected"}

    doc = documents[0]
    fields = getattr(doc, "fields", {}) or {}

    def read_field(name: str):
        field = fields.get(name)
        if not field:
            return {"value": None, "confidence": 0.0}
        return {
            "value": getattr(field, "content", None),
            "confidence": round(getattr(field, "confidence", 0.0) or 0.0, 3)
        }

    line_items = []
    items_field = fields.get("Items")
    if items_field and getattr(items_field, "value_array", None):
        for item in items_field.value_array:
            obj = getattr(item, "value_object", {}) or {}
            desc_field = obj.get("Description")
            amt_field = obj.get("Amount")
            line_items.append({
                "description": getattr(desc_field, "content", None) if desc_field else None,
                "amount": getattr(amt_field, "content", None) if amt_field else None,
                "confidence": round(getattr(item, "confidence", 0.0) or 0.0, 3)
            })

    return {
        "DocumentType": getattr(doc, "doc_type", "invoice"),
        "OverallConfidence": round(getattr(doc, "confidence", 0.0) or 0.0, 3),
        "VendorName": read_field("VendorName"),
        "CustomerName": read_field("CustomerName"),
        "InvoiceId": read_field("InvoiceId"),
        "InvoiceDate": read_field("InvoiceDate"),
        "DueDate": read_field("DueDate"),
        "SubTotal": read_field("SubTotal"),
        "TotalTax": read_field("TotalTax"),
        "InvoiceTotal": read_field("InvoiceTotal"),
        "LineItems": line_items
    }

# 5. Call Azure OpenAI (gpt-5-mini) via REST API to validate & normalize JSON
def enrich_with_llm(raw_extraction: dict) -> dict:
    if not AOAI_ENDPOINT:
        raise ValueError("AZURE_OPENAI_ENDPOINT environment variable is not set.")
    base_url = AOAI_ENDPOINT.rstrip("/")
    url = (
        f"{base_url}/openai/deployments/{AOAI_DEPLOYMENT}"
        f"/chat/completions?api-version=2025-01-01-preview"
    )

    headers = {"Content-Type": "application/json"}
    if AOAI_KEY:
        headers["api-key"] = AOAI_KEY
    else:
        token = DefaultAzureCredential().get_token(
            "https://cognitiveservices.azure.com/.default"
        ).token
        headers["Authorization"] = f"Bearer {token}"

    system_prompt = (
        "You are an Enterprise Accounts Payable AI Auditor. "
        "Given raw Azure Document Intelligence extraction with confidence scores:\n"
        "1. Normalize dates to ISO YYYY-MM-DD and monetary values to numeric floats.\n"
        "2. Set 'needs_human_review' to true if ANY key field (VendorName, InvoiceId, InvoiceTotal) "
        "has confidence < 0.85, or if SubTotal + TotalTax does not match InvoiceTotal.\n"
        "3. Return strictly valid JSON with keys: normalized_invoice, confidence_summary, "
        "math_check_passed, needs_human_review, routing_reason."
    )

    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(raw_extraction)}
        ],
        "response_format": {"type": "json_object"}
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST"
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read().decode("utf-8"))
        return json.loads(result["choices"][0]["message"]["content"])

# 6. Strict Deterministic Guardrail: Independent Code Verification
def verify_audit_rules(raw_data: dict, llm_payload: dict) -> dict:
    """
    Independently verifies confidence thresholds and arithmetic consistency in application code.
    Fails safely when required fields are missing, unparseable, or mismatched.
    """
    reasons = []
    needs_review = llm_payload.get("needs_human_review", False)
    math_passed = llm_payload.get("math_check_passed", True)

    # Rule 1: Mandatory Field & Confidence Gating (< 0.85 on any critical field)
    critical_fields = ["VendorName", "InvoiceId", "InvoiceTotal"]
    for field_name in critical_fields:
        field_data = raw_data.get(field_name, {})
        conf = field_data.get("confidence", 0.0)
        val = field_data.get("value")
        if val is None or str(val).strip() == "":
            needs_review = True
            reasons.append(f"Required field '{field_name}' is missing from document.")
        elif conf < 0.85:
            needs_review = True
            reasons.append(f"Confidence for '{field_name}' ({conf:.3f}) is below 0.85 threshold.")

    # Rule 2: Strict Arithmetic Verification (SubTotal + TotalTax == InvoiceTotal)
    def parse_amount(val) -> float | None:
        if val is None or str(val).strip() == "":
            return None
        clean = re.sub(r"[^\d.-]", "", str(val))
        try:
            return float(clean)
        except (ValueError, TypeError):
            return None

    subtotal_raw = raw_data.get("SubTotal", {}).get("value")
    tax_raw = raw_data.get("TotalTax", {}).get("value")
    total_raw = raw_data.get("InvoiceTotal", {}).get("value")

    subtotal = parse_amount(subtotal_raw)
    tax = parse_amount(tax_raw)
    total = parse_amount(total_raw)

    # Require both SubTotal and InvoiceTotal for mathematical validation
    if subtotal is None:
        math_passed = False
        needs_review = True
        reasons.append("SubTotal is missing or unparseable; arithmetic could not be verified.")

    if total is None:
        math_passed = False
        needs_review = True
        reasons.append("InvoiceTotal is missing or unparseable; arithmetic could not be verified.")

    if subtotal is not None and total is not None:
        # If tax is omitted, check if subtotal equals total
        if tax is None:
            if abs(subtotal - total) > 0.02:
                math_passed = False
                needs_review = True
                reasons.append(f"TotalTax is missing, and SubTotal ({subtotal:.2f}) does not match InvoiceTotal ({total:.2f}).")
            else:
                tax = 0.0
                math_passed = True
        else:
            computed = round(subtotal + tax, 2)
            diff = abs(computed - round(total, 2))
            if diff > 0.02:  # Tolerance of 2 cents for fractional rounding
                math_passed = False
                needs_review = True
                reasons.append(f"Arithmetic mismatch: SubTotal ({subtotal:.2f}) + TotalTax ({tax:.2f}) != InvoiceTotal ({total:.2f}).")
            else:
                math_passed = True

    # Merge code-level reasons with LLM output
    existing_reason = llm_payload.get("routing_reason", "")
    all_reasons = [existing_reason] if existing_reason else []
    for r in reasons:
        if r not in all_reasons:
            all_reasons.append(r)

    llm_payload["needs_human_review"] = needs_review
    llm_payload["math_check_passed"] = math_passed
    llm_payload["routing_reason"] = " | ".join(filter(None, all_reasons))
    return llm_payload

# 7. FastAPI Web Endpoints
app = FastAPI(
    title="Azure GenAI IDP Engine",
    description="Intelligent Document Processing and GenAI auditing service using Azure AI Document Intelligence and Azure OpenAI.",
    version="1.0.0"
)

@app.get("/")
async def root():
    return {
        "service": "Azure GenAI IDP Engine",
        "version": "1.0.0",
        "docs_url": "/docs",
        "health_url": "/health",
        "auth_enabled": bool(IDP_API_KEY and IDP_API_KEY.strip())
    }

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "genai-idp-engine",
        "docint_configured": bool(DOCINT_ENDPOINT),
        "openai_configured": bool(AOAI_ENDPOINT),
        "auth_enabled": bool(IDP_API_KEY and IDP_API_KEY.strip())
    }

@app.post("/analyze-invoice")
async def analyze_invoice(
    request: Request,
    file: UploadFile = File(...),
    api_key: str = Security(verify_api_key)
):
    # 1. Enforce Client IP Rate Limiting (15 req/min)
    await enforce_rate_limit(request)

    # 2. Validate Media Type
    content_type = file.content_type or "application/pdf"
    if content_type.lower() not in ALLOWED_MEDIA_TYPES:
        logger.warning("Rejected upload with unsupported media type: %s", content_type)
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported media type '{content_type}'. Allowed types: PDF, PNG, JPEG, TIFF, BMP."
        )

    # 3. Enforce File Size Limit via 1MB Chunk Streaming (DoS / OOM protection)
    contents = bytearray()
    while chunk := await file.read(1024 * 1024):
        contents.extend(chunk)
        if len(contents) > MAX_FILE_SIZE:
            logger.warning("Rejected upload exceeding maximum size limit of %d bytes", MAX_FILE_SIZE)
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Payload too large. Maximum allowed file size is {MAX_FILE_SIZE // (1024 * 1024)} MB."
            )

    file_bytes = bytes(contents)
    if len(file_bytes) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty."
        )

    # 4. Concurrency-throttled Processing with Azure Document Intelligence & OpenAI
    async with PROCESSING_SEMAPHORE:
        try:
            doc_client = get_docint_client()
            poller = doc_client.begin_analyze_document(
                "prebuilt-invoice",
                body=file_bytes,
                content_type=content_type
            )
            raw_data = extract_invoice_fields(poller.result())
            llm_payload = enrich_with_llm(raw_data)

            # 5. Independent Deterministic Verification
            verified_payload = verify_audit_rules(raw_data, llm_payload)

            return {
                "raw_document_intelligence": raw_data,
                "erp_validated_output": verified_payload
            }
        except HTTPException:
            raise
        except Exception as e:
            # Privately log detailed diagnostic trace without disclosing to clients
            logger.exception("Internal error processing invoice document: %s", str(e))
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="An internal error occurred while processing the invoice. Diagnostic details have been logged privately."
            )

# 8. CLI Test Mode
if __name__ == "__main__":
    sample_url = "https://raw.githubusercontent.com/Azure-Samples/cognitive-services-REST-api-samples/master/curl/form-recognizer/sample-invoice.pdf"
    print(f"1. Analyzing sample invoice via Document Intelligence ({DOCINT_ENDPOINT})...")
    doc_client = get_docint_client()
    poller = doc_client.begin_analyze_document(
        "prebuilt-invoice",
        AnalyzeDocumentRequest(url_source=sample_url)
    )
    raw = extract_invoice_fields(poller.result())
    print("\n--- STEP 1: Raw Document Intelligence Output ---")
    print(json.dumps(raw, indent=2))

    print(f"\n2. Validating & normalizing via Azure OpenAI ({AOAI_DEPLOYMENT})...")
    enriched = enrich_with_llm(raw)
    verified = verify_audit_rules(raw, enriched)
    print("\n--- STEP 2: Final Deterministically Verified ERP Output ---")
    print(json.dumps(verified, indent=2))
