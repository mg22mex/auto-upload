"""Unit tests — customer WhatsApp confirmation (Evolution)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.notifications.whatsapp import (
    format_customer_phone,
    format_lead_confirmation,
    notify_lead_confirmation,
    send_whatsapp_message,
)


class TestFormatLeadConfirmation(unittest.TestCase):
    def test_full_message(self):
        text = format_lead_confirmation(
            name="María",
            interested_vehicle="Mazda CX-5 2020",
            financing_summary="Enganche $50,000 · 48 meses · ~$8,500/mes",
            tradein_summary="Corolla 2018 · $95,000–$110,000",
            appointment_date="viernes 18:00",
            branch="periferico",
        )
        self.assertIn("Hola María, ¡gracias por comunicarte a Autosell! 🚗", text)
        self.assertIn("📌 Vehículo de interés: Mazda CX-5 2020", text)
        self.assertIn("💰 Financiamiento / Enganche:", text)
        self.assertIn("🔄 Avalúo Trade-In: Corolla 2018", text)
        self.assertIn("📅 Cita Agendada: viernes 18:00", text)
        self.assertIn("Quedamos a tus órdenes", text)
        self.assertNotIn("autosell.mx", text)

    def test_phone_normalize_mx10(self):
        self.assertEqual(format_customer_phone("6141234567"), "526141234567")
        self.assertEqual(format_customer_phone("+52 614 123 4567"), "526141234567")

    def test_phone_normalize_mx_521_prefix(self):
        with patch.dict("os.environ", {"WHATSAPP_MX_COUNTRY_PREFIX": "521"}):
            self.assertEqual(format_customer_phone("6141234567"), "5216141234567")


class TestSendWhatsApp(unittest.TestCase):
    def test_send_delegates_to_worker(self):
        client = MagicMock()
        client.send_text_message.return_value = {"ok": True}
        send_whatsapp_message("6145551212", "hola", branch="periferico", client=client)
        client.send_text_message.assert_called_once()
        args = client.send_text_message.call_args
        self.assertEqual(args.args[0], "526145551212")
        self.assertEqual(args.args[1], "hola")
        self.assertEqual(args.kwargs.get("branch"), "periferico")

    def test_notify_success(self):
        client = MagicMock()
        client.send_text_message.return_value = {"ok": True}
        result = notify_lead_confirmation(
            name="Ana",
            phone="6141112233",
            appointment_date="mañana 11:00",
            whatsapp_client=client,
        )
        self.assertTrue(result.sent)
        self.assertEqual(result.phone, "526141112233")
        self.assertIn("Ana", result.message)
        self.assertIn("📅 Cita Agendada: mañana 11:00", result.message)
        client.send_text_message.assert_called_once()

    def test_notify_failure_soft(self):
        client = MagicMock()
        client.send_text_message.side_effect = RuntimeError("connection reset")
        result = notify_lead_confirmation(
            name="Ana",
            phone="6141112233",
            whatsapp_client=client,
        )
        self.assertFalse(result.sent)
        self.assertIn("connection reset", result.error or "")

    def test_notify_disabled(self):
        client = MagicMock()
        with patch.dict("os.environ", {"VAPI_CUSTOMER_WHATSAPP": "false"}):
            result = notify_lead_confirmation(
                name="Ana",
                phone="6141112233",
                whatsapp_client=client,
            )
        self.assertFalse(result.sent)
        self.assertIn("VAPI_CUSTOMER_WHATSAPP", result.skipped_reason or "")
        client.send_text_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
