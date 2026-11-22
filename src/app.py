import os
import json
import urllib.request
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, HTTPException
from azure.core.credentials import AzureKeyCredential
from azure.identity import DefaultAzureCredential
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeDocumentRequest

# 1. Load environment variables from .env file
load_dotenv()

DOCINT_ENDPOINT = os.getenv("DOCINT_ENDPOINT")
DOCINT_KEY = os.getenv("DOCINT_KEY")
AOAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AOAI_KEY = os.getenv("AZURE_OPENAI_KEY")
AOAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5-mini")

# 2. Document Intelligence Client (Uses Key locally, Managed Identity in Cloud)
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

# 3. Extract structured fields + Confidence Scores from prebuilt-invoice
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

# 4. Call Azure OpenAI (gpt-5-mini) via REST API to validate & normalize JSON
def enrich_with_llm(raw_extraction: dict) -> dict:
    if not AOAI_ENDPOINT:
        raise ValueError("AZURE_OPENAI_ENDPOINT environment variable is not set.")
    base_url = AOAI_ENDPOINT.rstrip("/")
    url = (
        f"{base_url}/openai/deployments/{AOAI_DEPLOYMENT}"
        f"/chat/completions?api-version=2025-01-01-preview"
    )

    # Dual-auth: Use 'api-key' header for local dev, Entra ID Bearer token in production
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

# 5. FastAPI Web Endpoints
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
        "health_url": "/health"
    }

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "genai-idp-engine",
        "docint_configured": bool(DOCINT_ENDPOINT),
        "openai_configured": bool(AOAI_ENDPOINT)
    }

@app.post("/analyze-invoice")
async def analyze_invoice(file: UploadFile = File(...)):
    try:
        file_bytes = await file.read()
        doc_client = get_docint_client()
        poller = doc_client.begin_analyze_document(
            "prebuilt-invoice",
            body=file_bytes,
            content_type=file.content_type or "application/pdf"
        )
        raw_data = extract_invoice_fields(poller.result())
        erp_payload = enrich_with_llm(raw_data)
        return {
            "raw_document_intelligence": raw_data,
            "erp_validated_output": erp_payload
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# 6. CLI Test Mode (Runs when you execute: python src/app.py)
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
    print("\n--- STEP 2: Final ERP-Ready JSON Output ---")
    print(json.dumps(enriched, indent=2))
