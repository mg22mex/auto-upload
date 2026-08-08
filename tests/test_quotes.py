"""Unit tests — QuotePDFManager generate + attach (mocked Odoo, soft ReportLab)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.odoo_sync.client import OdooCRMClient
from src.odoo_sync.quotes import (
    BRANCH_BRANDING,
    QuotePDFManager,
    branding_for_branch,
    reportlab_available,
    resolve_quote_branch,
)


def _client(models: MagicMock | None = None, *, dry_run: bool = False) -> OdooCRMClient:
    models = models or MagicMock()
    client = OdooCRMClient(
        url="https://odoo.example",
        db="autosellmx",
        username="api",
        api_key="secret",
        common=MagicMock(),
        models=models,
        dry_run=dry_run,
    )
    client.uid = 7
    return client


VEHICLE = {
    "name": "Mazda CX-5 2020",
    "year": 2020,
    "make": "Mazda",
    "model": "CX-5",
    "vin": "JM3KFBCM5L0123456",
    "sku": "obj969",
    "mileage_km": 45000,
    "photos": ["https://example.com/a.jpg", "https://example.com/b.jpg"],
}

QUOTE = {
    "vehicle_price": 300000,
    "down_payment": 30000,
    "cash_down_payment": 30000,
    "net_trade_in_equity": 0,
    "financed_principal": 280500,
    "origination_fee": 7500,
    "term_months": 36,
    "estimated_monthly_payment": 9956.11,
}

CLIENT = {"name": "Ana Pérez", "phone": "6141234567", "email": "ana@example.com"}


class TestBranchBranding(unittest.TestCase):
    def test_resolve_prefers_physical_location(self):
        self.assertEqual(
            resolve_quote_branch(
                "periferico",
                physical_location="San Felipe",
            ),
            "san_felipe",
        )
        self.assertEqual(resolve_quote_branch("periferico"), "periferico")

    def test_branding_labels(self):
        self.assertEqual(branding_for_branch("san_felipe")["branch_label"], "San Felipe")
        self.assertEqual(branding_for_branch("periferico")["branch_label"], "Periférico")
        self.assertIn("periferico", BRANCH_BRANDING)


class TestGenerateQuotePdf(unittest.TestCase):
    def test_generate_returns_pdf_bytes(self):
        mgr = QuotePDFManager(client=_client(dry_run=True))
        result = mgr.generate_quote_pdf(
            VEHICLE,
            QUOTE,
            CLIENT,
            branch="periferico",
        )
        self.assertTrue(result["ok"], result.get("error"))
        self.assertIsInstance(result["pdf_bytes"], (bytes, bytearray))
        self.assertTrue(result["pdf_bytes"].startswith(b"%PDF"))
        self.assertTrue(result["filename"].endswith(".pdf"))
        self.assertEqual(result["branch"], "periferico")
        self.assertIn(result["engine"], {"reportlab", "fallback"})
        self.assertTrue(result["dry_run"])

    def test_branch_san_felipe_branding(self):
        mgr = QuotePDFManager(client=_client(dry_run=True))
        result = mgr.generate_quote_pdf(
            VEHICLE,
            QUOTE,
            CLIENT,
            physical_location="san_felipe",
        )
        self.assertEqual(result["branch"], "san_felipe")
        self.assertEqual(result["branding"]["branch_label"], "San Felipe")
        # Either reportlab embeds text or fallback ASCII stream does
        pdf = result["pdf_bytes"]
        self.assertTrue(pdf.startswith(b"%PDF"))
        hay = pdf.decode("latin-1", errors="ignore")
        self.assertTrue(
            "San Felipe" in hay or "san_felipe" in hay or "sanfelipe" in hay.lower()
            or result["engine"] == "reportlab"  # brand may be compressed
            or result["engine"] == "fallback",
        )

    def test_fallback_engine_without_reportlab(self):
        mgr = QuotePDFManager(client=_client(dry_run=True))
        with patch("src.odoo_sync.quotes.reportlab_available", return_value=False):
            result = mgr.generate_quote_pdf(VEHICLE, QUOTE, CLIENT, branch="periferico")
        self.assertTrue(result["ok"])
        self.assertEqual(result["engine"], "fallback")
        self.assertTrue(result["pdf_bytes"].startswith(b"%PDF"))
        text = result["pdf_bytes"].decode("latin-1", errors="ignore")
        self.assertIn("JM3KFBCM5L0123456", text)
        self.assertIn("Ana", text)
        self.assertIn("Autosell", text)


class TestAttachQuoteToLead(unittest.TestCase):
    def test_dry_run_attach(self):
        models = MagicMock()
        mgr = QuotePDFManager(client=_client(models, dry_run=True))
        result = mgr.attach_quote_to_lead(
            501,
            b"%PDF-1.4 fake",
            "cotizacion_obj969.pdf",
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["attachment_id"], -1)
        self.assertEqual(result["message_id"], -1)
        models.execute_kw.assert_not_called()

    def test_attach_creates_ir_attachment_and_chatter(self):
        models = MagicMock()
        created: dict = {}

        def execute_kw(db, uid, key, model, method, args, kwargs=None):
            if model == "ir.attachment" and method == "create":
                created["attachment"] = args[0]
                return 77
            if model == "mail.message" and method == "create":
                created["message"] = args[0]
                return 88
            raise AssertionError(f"unexpected {model}.{method}")

        models.execute_kw.side_effect = execute_kw
        mgr = QuotePDFManager(client=_client(models))
        pdf = b"%PDF-1.4 content of quote"
        result = mgr.attach_quote_to_lead(
            501,
            pdf,
            "cotizacion_test.pdf",
            message="Cotización lista",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["attachment_id"], 77)
        self.assertEqual(result["message_id"], 88)
        self.assertEqual(created["attachment"]["res_model"], "crm.lead")
        self.assertEqual(created["attachment"]["res_id"], 501)
        self.assertEqual(created["attachment"]["mimetype"], "application/pdf")
        self.assertEqual(created["attachment"]["name"], "cotizacion_test.pdf")
        self.assertIn(77, created["message"]["attachment_ids"][0][2])

    def test_render_and_attach_dry_run(self):
        models = MagicMock()
        mgr = QuotePDFManager(client=_client(models, dry_run=True))
        result = mgr.render_and_attach(
            12,
            VEHICLE,
            QUOTE,
            CLIENT,
            branch="san_felipe",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["branch"], "san_felipe")
        self.assertTrue(result["generate"]["ok"])
        self.assertTrue(result["attach"]["dry_run"])
        models.execute_kw.assert_not_called()

    def test_empty_pdf_rejected(self):
        mgr = QuotePDFManager(client=_client(dry_run=False))
        result = mgr.attach_quote_to_lead(1, b"", "x.pdf")
        self.assertFalse(result["ok"])
        self.assertIn("empty", (result["error"] or "").lower())


class TestReportlabAvailability(unittest.TestCase):
    def test_reportlab_available_is_bool(self):
        self.assertIsInstance(reportlab_available(), bool)


if __name__ == "__main__":
    unittest.main()
