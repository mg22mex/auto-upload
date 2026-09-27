"""Unit tests — PDF quote / spec sheet generator."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import reportlab  # noqa: F401

    _HAS_REPORTLAB = True
except ImportError:
    _HAS_REPORTLAB = False

from src.pdf_engine.generator import (  # noqa: E402
    PdfEngineError,
    build_quote_pdf_bytes,
    generate_vehicle_quote_pdf,
)


QUOTE = {
    "vehicle_price": 300000,
    "down_payment": 30000,
    "cash_down_payment": 30000,
    "net_trade_in_equity": 0,
    "financed_principal": 280500,
    "origination_fee": 7500,
    "term_months": 36,
    "estimated_monthly_payment": 9956.11,
    "monthly_admin_fee": 0,
}

VEHICLE = {
    "name": "Mazda CX-5 2020",
    "year": 2020,
    "make": "Mazda",
    "model": "CX-5",
    "vin": "JM3KFBCM5L0123456",
    "mileage_km": 45000,
    "transmission": "Automática",
    "autosell_id": "obj969",
    "features": ["Piel", "Cámara", "Android Auto"],
}


@unittest.skipUnless(_HAS_REPORTLAB, "reportlab not installed")
class TestBuildQuotePdf(unittest.TestCase):
    def test_returns_non_empty_pdf_bytes(self):
        pdf = build_quote_pdf_bytes(QUOTE, VEHICLE)
        self.assertIsInstance(pdf, (bytes, bytearray))
        self.assertGreater(len(pdf), 500)
        self.assertTrue(pdf.startswith(b"%PDF"))

    def test_contains_key_fields(self):
        pdf = build_quote_pdf_bytes(QUOTE, VEHICLE)
        # ReportLab embeds text as PDF operators; check readable strings present.
        text = pdf.decode("latin-1", errors="ignore")
        self.assertIn("Autosell", text)
        self.assertIn("Mazda", text)
        self.assertIn("Enganche", text)
        self.assertIn("Mensualidad", text)
        self.assertIn("300,000.00", text)
        self.assertIn("9,956.11", text)
        self.assertIn("obj969", text)
        self.assertIn("JM3KFBCM5L0123456", text)

    def test_rejects_non_dicts(self):
        with self.assertRaises(PdfEngineError):
            build_quote_pdf_bytes("bad", VEHICLE)  # type: ignore[arg-type]


@unittest.skipUnless(_HAS_REPORTLAB, "reportlab not installed")
class TestGenerateVehicleQuotePdf(unittest.TestCase):
    def test_writes_file_and_returns_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "quote_test.pdf"
            result = generate_vehicle_quote_pdf(
                QUOTE,
                VEHICLE,
                output_path=out,
                lead_id=501,
            )
            self.assertEqual(result, out)
            self.assertTrue(out.is_file())
            self.assertGreater(out.stat().st_size, 500)
            self.assertTrue(out.read_bytes().startswith(b"%PDF"))

    def test_returns_bytes_without_path(self):
        result = generate_vehicle_quote_pdf(QUOTE, VEHICLE, lead_id=12)
        self.assertIsInstance(result, (bytes, bytearray))
        self.assertTrue(result.startswith(b"%PDF"))

    def test_attaches_to_odoo_when_requested(self):
        odoo = MagicMock()
        odoo.attach_file.return_value = 9001
        with tempfile.TemporaryDirectory() as tmp:
            path = generate_vehicle_quote_pdf(
                QUOTE,
                VEHICLE,
                output_dir=tmp,
                lead_id=501,
                attach_to_odoo=True,
                odoo_client=odoo,
            )
            self.assertIsInstance(path, Path)
            odoo.attach_file.assert_called_once()
            kwargs = odoo.attach_file.call_args.kwargs
            self.assertEqual(kwargs["model"], "crm.lead")
            self.assertEqual(kwargs["res_id"], 501)
            self.assertEqual(kwargs["mimetype"], "application/pdf")
            self.assertTrue(kwargs["filename"].endswith(".pdf"))
            self.assertGreater(len(kwargs["content"]), 500)


@unittest.skipUnless(_HAS_REPORTLAB, "reportlab not installed")
class TestFinancingQuotePdf(unittest.TestCase):
    def test_amortization_pdf_named_financing_quote(self):
        from src.pdf_engine.generator import (
            generate_financing_quote_pdf,
            sanitize_vehicle_title,
        )
        from src.quote_engine.engine import CalibratedQuoteEngine

        self.assertEqual(
            sanitize_vehicle_title(
                "Hola, quiero cotizar una Ford Ranger XLT 2021 por favor"
            ),
            "Ford Ranger XLT 2021",
        )
        self.assertEqual(
            sanitize_vehicle_title(
                "chat noise",
                make="Ford",
                model="Ranger XLT",
                year=2021,
            ),
            "Ford Ranger XLT 2021",
        )

        quote = CalibratedQuoteEngine().calculate(450000, 48, down_payment=90000)
        with tempfile.TemporaryDirectory() as tmp:
            path = generate_financing_quote_pdf(
                quote,
                output_dir=tmp,
                vehicle_data={
                    "name": "Hola, quiero cotizar una Ford Ranger XLT 2021"
                },
                customer_name="Luis",
                filename="financing_quote.pdf",
            )
            self.assertEqual(path.name, "financing_quote.pdf")
            self.assertTrue(path.is_file())
            raw = path.read_bytes()
            self.assertTrue(raw.startswith(b"%PDF"))
            text = raw.decode("latin-1", errors="ignore")
            self.assertIn("amortizaci", text.lower())
            self.assertIn("CrediAuto", text)
            self.assertIn("Calendario", text)
            self.assertIn("Ford Ranger XLT 2021", text)
            self.assertNotIn("Hola, quiero cotizar", text)
            self.assertIn("IMPORTANTE", text)
            self.assertIn("estimaci", text.lower())
            self.assertIn("aprobaci", text.lower())
            self.assertIn("instituci", text.lower())
            # Disclaimer is rendered twice (under summary + footer block).
            self.assertGreaterEqual(text.count("IMPORTANTE"), 2)


if __name__ == "__main__":
    unittest.main()
