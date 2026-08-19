"""Unit tests — WhatsAppWorkerClient (mocked HTTP)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.whatsapp_worker.client import (
    WhatsAppWorkerClient,
    WhatsAppWorkerError,
    format_quote_message,
    normalize_phone_number,
)


class _FakeQuote:
    vehicle_price = Decimal("300000.00")
    term_months = 36
    down_payment = Decimal("30000.00")
    cash_down_payment = Decimal("20000.00")
    net_trade_in_equity = Decimal("10000.00")
    base_monthly_payment = Decimal("9323.47")
    estimated_monthly_payment = Decimal("10726.90")
    monthly_auto_insurance = Decimal("1000.00")
    monthly_life_insurance = Decimal("141.67")
    average_monthly_iva = Decimal("261.76")
    origination_fee = Decimal("6750.00")
    financed_principal = Decimal("276750.00")


class TestPhoneNormalize(unittest.TestCase):
    def test_local_10_digit_gets_mx_code(self):
        self.assertEqual(normalize_phone_number("614-123-4567"), "526141234567")

    def test_already_e164_digits(self):
        self.assertEqual(normalize_phone_number("+52 614 123 4567"), "526141234567")

    def test_empty_rejected(self):
        with self.assertRaises(WhatsAppWorkerError):
            normalize_phone_number("   ")


class TestFormatQuote(unittest.TestCase):
    def test_template_fields(self):
        text = format_quote_message("Ana Pérez", "Mazda CX-5 2020", _FakeQuote())
        self.assertIn("Ana Pérez", text)
        self.assertIn("Mazda CX-5 2020", text)
        self.assertIn("36 meses", text)
        self.assertIn("$30,000.00", text)
        self.assertIn("$20,000.00", text)
        self.assertIn("$10,000.00", text)
        self.assertIn("$9,323.47", text)
        self.assertIn("$10,726.90", text)
        self.assertIn("Seguro auto", text)
        self.assertIn("IVA intereses", text)

    def test_client_classmethod(self):
        text = WhatsAppWorkerClient.format_quote_message("Luis", "Vento", _FakeQuote())
        self.assertIn("Luis", text)
        self.assertIn("Vento", text)


class TestSendTextEvolution(unittest.TestCase):
    def test_payload(self):
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b'{"key":{"id":"abc"}}'
        resp.json.return_value = {"key": {"id": "abc"}}
        session.post.return_value = resp

        client = WhatsAppWorkerClient(
            base_url="https://wa.example",
            api_key="sekrit",
            instance="autosell",
            provider="evolution",
            session=session,
        )
        out = client.send_text_message("6141234567", "Hola cotización")
        self.assertEqual(out["key"]["id"], "abc")
        args, kwargs = session.post.call_args
        self.assertEqual(args[0], "https://wa.example/message/sendText/autosell")
        self.assertEqual(kwargs["json"]["number"], "526141234567")
        self.assertEqual(kwargs["json"]["text"], "Hola cotización")
        self.assertEqual(kwargs["headers"]["apikey"], "sekrit")

    def test_empty_body_rejected(self):
        client = WhatsAppWorkerClient(
            base_url="https://wa.example",
            api_key="x",
            session=MagicMock(),
        )
        with self.assertRaises(WhatsAppWorkerError):
            client.send_text_message("6141234567", "  ")


class TestSendTextOpenWA(unittest.TestCase):
    def test_chat_id_payload(self):
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 201
        resp.content = b'{"success":true}'
        resp.json.return_value = {"success": True}
        session.post.return_value = resp

        client = WhatsAppWorkerClient(
            base_url="https://openwa.example",
            api_key="tok",
            provider="openwa",
            session=session,
        )
        client.send_text_message("+52 614 000 1111", "Ping")
        _, kwargs = session.post.call_args
        self.assertEqual(kwargs["json"]["chatId"], "526140001111@c.us")
        self.assertTrue(kwargs["headers"]["Authorization"].startswith("Bearer "))


class TestSendQuotePdf(unittest.TestCase):
    def test_evolution_multipart(self):
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b'{"status":"PENDING"}'
        resp.json.return_value = {"status": "PENDING"}
        session.post.return_value = resp

        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "quote_cx5.pdf"
            pdf.write_bytes(b"%PDF-1.4 fake")
            client = WhatsAppWorkerClient(
                base_url="https://wa.example",
                api_key="sekrit",
                instance="autosell",
                provider="evolution",
                session=session,
            )
            out = client.send_quote_pdf(
                "6141234567",
                pdf,
                caption="Tu cotización CX-5",
            )
            self.assertEqual(out["status"], "PENDING")
            args, kwargs = session.post.call_args
            self.assertEqual(args[0], "https://wa.example/message/sendMedia/autosell")
            self.assertEqual(kwargs["data"]["number"], "526141234567")
            self.assertEqual(kwargs["data"]["caption"], "Tu cotización CX-5")
            self.assertEqual(kwargs["data"]["mediatype"], "document")
            self.assertIn("file", kwargs["files"])
            self.assertEqual(kwargs["headers"]["apikey"], "sekrit")

    def test_missing_pdf(self):
        client = WhatsAppWorkerClient(
            base_url="https://wa.example",
            api_key="x",
            session=MagicMock(),
        )
        with self.assertRaises(WhatsAppWorkerError):
            client.send_quote_pdf("6141234567", "/no/such/quote.pdf", "cap")


class TestConfig(unittest.TestCase):
    def test_missing_base_url(self):
        with patch.dict(
            "os.environ",
            {"WHATSAPP_API_URL": "", "WHATSAPP_BASE_URL": ""},
            clear=False,
        ):
            client = WhatsAppWorkerClient(api_key="x", session=MagicMock())
            with self.assertRaises(WhatsAppWorkerError):
                client.send_text_message("6141234567", "hi")

    def test_reads_api_url_and_instance_name(self):
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b"{}"
        resp.json.return_value = {}
        session.post.return_value = resp
        env = {
            "WHATSAPP_API_URL": "http://127.0.0.1:8082",
            "WHATSAPP_API_KEY": "k",
            "WHATSAPP_INSTANCE_PERIFERICO": "autosell_periferico",
            "WHATSAPP_PROVIDER": "evolution",
        }
        with patch.dict("os.environ", env, clear=False):
            client = WhatsAppWorkerClient(session=session)
        client.send_text_message("6141234567", "hola", branch="periferico", instance="")
        args, _kwargs = session.post.call_args
        self.assertEqual(
            args[0], "http://127.0.0.1:8082/message/sendText/autosell_periferico"
        )


if __name__ == "__main__":
    unittest.main()
