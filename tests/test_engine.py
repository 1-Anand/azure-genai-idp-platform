import unittest
from unittest.mock import MagicMock
from src.app import extract_invoice_fields, app
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

if __name__ == "__main__":
    unittest.main()
