import io
import unittest
from unittest.mock import MagicMock, patch
from src.app import extract_invoice_fields, verify_audit_rules, app
from fastapi.testclient import TestClient

class TestInvoiceEngine(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_root_endpoint(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("service", data)
        self.assertEqual(data["docs_url"], "/docs")

    def test_health_endpoint(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "healthy")
        self.assertIn("docint_configured", data)
        self.assertIn("openai_configured", data)

    def test_extract_invoice_fields_empty(self):
        mock_result = MagicMock()
        mock_result.documents = []
        result = extract_invoice_fields(mock_result)
        self.assertIn("error", result)

    def test_extract_invoice_fields_valid(self):
        mock_result = MagicMock()
        mock_doc = MagicMock()
        mock_doc.doc_type = "invoice"
        mock_doc.confidence = 0.95

        vendor_field = MagicMock()
        vendor_field.content = "Contoso Corp"
        vendor_field.confidence = 0.98

        total_field = MagicMock()
        total_field.content = "$1,500.00"
        total_field.confidence = 0.92

        mock_doc.fields = {
            "VendorName": vendor_field,
            "InvoiceTotal": total_field
        }
        mock_result.documents = [mock_doc]

        result = extract_invoice_fields(mock_result)
        self.assertEqual(result["DocumentType"], "invoice")
        self.assertEqual(result["OverallConfidence"], 0.95)
        self.assertEqual(result["VendorName"]["value"], "Contoso Corp")
        self.assertEqual(result["VendorName"]["confidence"], 0.98)
        self.assertEqual(result["InvoiceTotal"]["value"], "$1,500.00")
        self.assertEqual(result["InvoiceTotal"]["confidence"], 0.92)
        self.assertIsNone(result["InvoiceId"]["value"])
        self.assertEqual(result["InvoiceId"]["confidence"], 0.0)

    def test_deterministic_confidence_guardrail(self):
        raw_data = {
            "VendorName": {"value": "Acme Inc", "confidence": 0.95},
            "InvoiceId": {"value": "INV-100", "confidence": 0.65},  # Below 0.85
            "InvoiceTotal": {"value": "100.00", "confidence": 0.90},
            "SubTotal": {"value": "90.00", "confidence": 0.90},
            "TotalTax": {"value": "10.00", "confidence": 0.90}
        }
        llm_payload = {
            "needs_human_review": False,
            "math_check_passed": True,
            "routing_reason": "LLM passed"
        }
        verified = verify_audit_rules(raw_data, llm_payload)
        self.assertTrue(verified["needs_human_review"])
        self.assertIn("below 0.85 threshold", verified["routing_reason"])

    def test_deterministic_arithmetic_guardrail(self):
        # Subtotal (100) + Tax (10) != Total (150)
        raw_data = {
            "VendorName": {"value": "Acme Inc", "confidence": 0.95},
            "InvoiceId": {"value": "INV-100", "confidence": 0.95},
            "InvoiceTotal": {"value": "$150.00", "confidence": 0.95},
            "SubTotal": {"value": "$100.00", "confidence": 0.95},
            "TotalTax": {"value": "$10.00", "confidence": 0.95}
        }
        llm_payload = {
            "needs_human_review": False,
            "math_check_passed": True,
            "routing_reason": ""
        }
        verified = verify_audit_rules(raw_data, llm_payload)
        self.assertFalse(verified["math_check_passed"])
        self.assertTrue(verified["needs_human_review"])
        self.assertIn("Arithmetic mismatch", verified["routing_reason"])

    @patch("src.app.IDP_API_KEY", "test-key")
    def test_upload_unsupported_media_type(self):
        file_content = b"fake executable content"
        response = self.client.post(
            "/analyze-invoice",
            headers={"X-API-Key": "test-key"},
            files={"file": ("test.exe", io.BytesIO(file_content), "application/x-msdownload")}
        )
        self.assertEqual(response.status_code, 415)
        self.assertIn("Unsupported media type", response.json()["detail"])

    @patch("src.app.IDP_API_KEY", "test-key")
    def test_upload_empty_file(self):
        response = self.client.post(
            "/analyze-invoice",
            headers={"X-API-Key": "test-key"},
            files={"file": ("empty.pdf", io.BytesIO(b""), "application/pdf")}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("empty", response.json()["detail"])

    @patch("src.app.IDP_API_KEY", "")
    def test_fail_closed_when_unconfigured(self):
        # When server has no IDP_API_KEY configured, data plane returns 503 Service Unavailable
        response = self.client.post(
            "/analyze-invoice",
            headers={"X-API-Key": "any-key"},
            files={"file": ("invoice.pdf", io.BytesIO(b"%PDF dummy"), "application/pdf")}
        )
        self.assertEqual(response.status_code, 503)
        self.assertIn("disabled", response.json()["detail"])

    @patch("src.app.IDP_API_KEY", "secret-test-key")
    def test_api_key_authentication(self):
        # 1. Missing header -> 401
        res_no_auth = self.client.post(
            "/analyze-invoice",
            files={"file": ("test.pdf", io.BytesIO(b"dummy pdf bytes"), "application/pdf")}
        )
        self.assertEqual(res_no_auth.status_code, 401)

        # 2. Wrong header -> 401
        res_wrong_auth = self.client.post(
            "/analyze-invoice",
            headers={"X-API-Key": "wrong-key"},
            files={"file": ("test.pdf", io.BytesIO(b"dummy pdf bytes"), "application/pdf")}
        )
        self.assertEqual(res_wrong_auth.status_code, 401)

    @patch("src.app.IDP_API_KEY", "rate-limit-test-key")
    def test_rate_limiter(self):
        # Send 15 requests (allowed)
        for _ in range(15):
            res = self.client.post(
                "/analyze-invoice",
                headers={"X-API-Key": "rate-limit-test-key", "x-forwarded-for": "192.168.1.100"},
                files={"file": ("test.pdf", io.BytesIO(b""), "application/pdf")}
            )
            # Should reach validation (400 empty file), not 429
            self.assertEqual(res.status_code, 400)

        # 16th request must trigger 429 Too Many Requests
        res_rate_limited = self.client.post(
            "/analyze-invoice",
            headers={"X-API-Key": "rate-limit-test-key", "x-forwarded-for": "192.168.1.100"},
            files={"file": ("test.pdf", io.BytesIO(b""), "application/pdf")}
        )
        self.assertEqual(res_rate_limited.status_code, 429)
        self.assertIn("Rate limit exceeded", res_rate_limited.json()["detail"])

if __name__ == "__main__":
    unittest.main()
