"""Unit tests — Vapi WhatsApp text-first chat helpers + financing force."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.voice_gateway.vapi_chat import (
    VapiChatSessionStore,
    detect_down_payment_amount,
    extract_assistant_text,
    extract_tools_called,
    rewrite_reply_keep_interactive,
    vapi_wa_text_first_enabled,
)


class TestExtractAssistantText(unittest.TestCase):
    def test_last_assistant_content(self):
        payload = {
            "output": [
                {"role": "assistant", "tool_calls": [{"id": "1"}]},
                {"role": "assistant", "content": "Un momento"},
                {"role": "tool", "content": "{}"},
                {
                    "role": "assistant",
                    "content": "Tenemos un Corolla por trescientos mil pesos.",
                },
            ]
        }
        self.assertIn("Corolla", extract_assistant_text(payload))

    def test_tools_called(self):
        payload = {
            "output": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "1",
                            "function": {"name": "calculate_financing"},
                        }
                    ],
                }
            ]
        }
        self.assertEqual(extract_tools_called(payload), ["calculate_financing"])


class TestDownPaymentDetect(unittest.TestCase):
    def test_dollar_amount(self):
        self.assertEqual(detect_down_payment_amount("Mi enganche es de $200,000"), 200000.0)

    def test_mil(self):
        self.assertEqual(detect_down_payment_amount("puedo dar 80 mil de enganche"), 80000.0)

    def test_bare_large(self):
        self.assertEqual(detect_down_payment_amount("200000"), 200000.0)

    def test_small_ignored(self):
        self.assertIsNone(detect_down_payment_amount("tengo 2 hijos"))

    def test_k_suffix(self):
        self.assertEqual(
            detect_down_payment_amount(
                "Hola, quiero cotizar un mustang con 200k de enganche, 60 meses"
            ),
            200000.0,
        )


class TestTermMonthsDetect(unittest.TestCase):
    def test_60_meses(self):
        from src.voice_gateway.vapi_chat import detect_term_months

        self.assertEqual(
            detect_term_months(
                "Hola, quiero cotizar un mustang con 200k de enganche, 60 meses"
            ),
            60,
        )

    def test_missing(self):
        from src.voice_gateway.vapi_chat import detect_term_months

        self.assertIsNone(detect_term_months("quiero un mustang"))


class TestMatchFromInventoryBlob(unittest.TestCase):
    def test_year_pulled_from_inventory_json(self):
        from src.voice_gateway.vapi_chat import _match_from_tool_blobs

        price, name, year = _match_from_tool_blobs(
            [
                {
                    "found": True,
                    "vehicles": [
                        {
                            "name": "Ford Mustang GT 2025",
                            "model": "Ford Mustang GT 2025",
                            "year": 2025,
                            "price": "$689,000 MXN",
                        }
                    ],
                }
            ]
        )
        self.assertEqual(price, 689000.0)
        self.assertIn("Mustang", name or "")
        self.assertEqual(year, 2025)


class TestRewriteReply(unittest.TestCase):
    def test_strips_asesor_and_adds_cita(self):
        text = (
            "Tu mensualidad sería de diez mil pesos. "
            "Un asesor te contactará para resolver dudas. ¡Gracias!"
        )
        out = rewrite_reply_keep_interactive(text, branch="periferico")
        self.assertNotIn("asesor te contactará", out.casefold())
        self.assertIn("agendar una cita", out.casefold())
        self.assertIn("Periférico", out)


class TestTextFirstFlag(unittest.TestCase):
    def test_default_off(self):
        with patch.dict("os.environ", {"VAPI_WA_TEXT_FIRST": ""}, clear=False):
            self.assertFalse(vapi_wa_text_first_enabled())

    def test_opt_in(self):
        with patch.dict("os.environ", {"VAPI_WA_TEXT_FIRST": "true"}, clear=False):
            self.assertTrue(vapi_wa_text_first_enabled())


class TestChatStore(unittest.TestCase):
    def test_round_trip_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = VapiChatSessionStore(Path(tmp) / "chats.db")
            store.set_chat_id(
                "5216140000000",
                "chat-abc",
                "autosell",
                meta={"vehicle_price": 365000, "vehicle_name": "Corolla"},
            )
            self.assertEqual(
                store.get_chat_id("5216140000000", "autosell"), "chat-abc"
            )
            meta = store.get_meta("5216140000000", "autosell")
            self.assertEqual(meta.get("vehicle_price"), 365000)


class TestForceFinancingOnDownPayment(unittest.TestCase):
    def test_chat_forces_financing_when_vapi_skips_tool(self):
        from src.voice_gateway import vapi_chat as vc

        fake_payload = {
            "id": "chat-1",
            "output": [
                {
                    "role": "assistant",
                    "content": "Un asesor te contactará pronto. Gracias.",
                }
            ],
        }
        forced = {
            "speech": (
                "Con un enganche de doscientos mil pesos a cuarenta y ocho meses, "
                "tu mensualidad estimada con Scotiabank sería de diez mil pesos. "
                "Te envié el PDF. ¿Te gustaría agendar una cita en sucursal "
                "Periférico para ver la unidad o realizar prueba de manejo?"
            )
        }

        with tempfile.TemporaryDirectory() as tmp:
            store = VapiChatSessionStore(Path(tmp) / "chats.db")
            store.set_chat_id(
                "6141112222",
                "prev",
                meta={"vehicle_price": 450000, "vehicle_name": "Corolla"},
            )
            with patch.object(vc, "_api_key", return_value="k"), patch.object(
                vc, "_assistant_id", return_value="asst"
            ), patch.object(vc, "_http_json", return_value=fake_payload), patch.object(
                vc, "force_calculate_financing", return_value=forced
            ) as force:
                result = vc.chat_with_beatriz(
                    text="Mi enganche es de $200,000",
                    phone="6141112222",
                    customer_name="Luis",
                    branch="periferico",
                    store=store,
                )
        self.assertTrue(result.ok)
        self.assertTrue(result.financing_forced)
        self.assertTrue(result.financing_sent)
        force.assert_called_once()
        self.assertEqual(force.call_args.kwargs["down_payment"], 200000.0)
        self.assertNotIn("asesor te contactará", result.reply_text.casefold())
        self.assertIn("agendar una cita", result.reply_text.casefold())


if __name__ == "__main__":
    unittest.main()
