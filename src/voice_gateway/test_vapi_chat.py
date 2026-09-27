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

    def test_mileage_mil_km_not_enganche(self):
        self.assertIsNone(
            detect_down_payment_amount(
                "¿En cuánto me valúan un Corolla 2020 con 50 mil km?"
            )
        )

    def test_tradein_phrase_blocks_enganche(self):
        self.assertIsNone(
            detect_down_payment_amount(
                "Cuanto me estimas un corolla 2020 con 50 mil kilometros"
            )
        )


class TestSessionResetDetect(unittest.TestCase):
    def test_phrases(self):
        from src.voice_gateway.vapi_chat import detect_session_reset

        for text in ("reiniciar", "reset", "empezar de nuevo", "nueva conversación"):
            with self.subTest(text=text):
                self.assertTrue(detect_session_reset(text), text)
        self.assertFalse(detect_session_reset("cuanto cuesta el mustang"))


class TestTradeinVersionFollowup(unittest.TestCase):
    def test_version_le_reruns_valuation(self):
        from src.voice_gateway import vapi_chat as vc

        forced = {
            "ok": True,
            "tool": "get_tradein_valuation",
            "speech": (
                "Estimación de toma a cuenta para Toyota Corolla 2020 LE (50,000 km): "
                "~$201,200 MXN (Sujeto a inspección física y mecánica en sucursal)."
            ),
            "details": {
                "make": "Toyota",
                "model": "Corolla",
                "year": 2020,
                "version": "LE",
                "mileage_km": 50000,
                "trim": "LE",
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            store = VapiChatSessionStore(Path(tmp) / "chats.db")
            store.set_chat_id(
                "6143231198",
                "prev",
                meta={
                    "tradein_make": "Toyota",
                    "tradein_model": "Corolla",
                    "tradein_year": 2020,
                    "tradein_version": "Base",
                    "tradein_mileage_km": 50000,
                    "tradein_summary": "Estimación previa Base",
                    "valor_compra": 195500,
                },
            )
            with patch.object(vc, "_api_key", return_value="k"), patch.object(
                vc, "_assistant_id", return_value="asst"
            ), patch.object(vc, "_http_json") as http, patch.object(
                vc, "force_get_tradein_valuation", return_value=forced
            ) as force_ti:
                result = vc.chat_with_beatriz(
                    text="Es versión LE",
                    phone="6143231198",
                    branch="periferico",
                    store=store,
                )
        http.assert_not_called()
        force_ti.assert_called_once()
        self.assertEqual(force_ti.call_args.kwargs.get("version"), "LE")
        self.assertTrue(result.tradein_forced)
        self.assertIn("201,200", result.reply_text)
        self.assertNotIn("ciento cincuenta", result.reply_text.casefold())

    def test_brief_ee_gets_apply_cta(self):
        from src.voice_gateway import vapi_chat as vc
        from src.voice_gateway.vapi_chat import TRADEIN_APPLY_CTA

        with tempfile.TemporaryDirectory() as tmp:
            store = VapiChatSessionStore(Path(tmp) / "chats.db")
            store.set_chat_id(
                "6143231198",
                "prev",
                meta={
                    "tradein_make": "Toyota",
                    "tradein_model": "Corolla",
                    "tradein_year": 2020,
                    "tradein_mileage_km": 50000,
                    "tradein_summary": (
                        "Estimación de toma a cuenta para Toyota Corolla 2020 LE "
                        "(50,000 km): ~$201,200 MXN "
                        "(Sujeto a inspección física y mecánica en sucursal)."
                    ),
                    "valor_compra": 201200,
                },
            )
            with patch.object(vc, "_api_key", return_value="k"), patch.object(
                vc, "_assistant_id", return_value="asst"
            ), patch.object(vc, "_http_json") as http:
                result = vc.chat_with_beatriz(
                    text="ee",
                    phone="6143231198",
                    branch="periferico",
                    store=store,
                )
        http.assert_not_called()
        self.assertEqual(result.reply_text, TRADEIN_APPLY_CTA)
        self.assertIn("tradein_apply_cta", result.tools_called)


class TestTradeinOverridesMustangContext(unittest.TestCase):
    def test_tradein_short_circuits_before_vapi(self):
        """Trade-in short-circuits Vapi; inventory interest stays sticky."""
        from src.voice_gateway import vapi_chat as vc

        forced = {
            "ok": True,
            "tool": "get_tradein_valuation",
            "speech": (
                "Estimación de toma a cuenta para Toyota Corolla 2020 LE (50,000 km): "
                "~$201,200 MXN (Sujeto a inspección física y mecánica en sucursal)."
            ),
            "details": {
                "make": "Toyota",
                "model": "Corolla",
                "year": 2020,
                "version": "LE",
                "mileage_km": 50000,
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            store = VapiChatSessionStore(Path(tmp) / "chats.db")
            store.set_chat_id(
                "6143231198",
                "prev-mustang",
                meta={
                    "vehicle_price": 890000,
                    "vehicle_name": "Ford Mustang GT 2025",
                    "interested_vehicle": "Ford Mustang GT 2025",
                },
            )
            with patch.object(vc, "_api_key", return_value="k"), patch.object(
                vc, "_assistant_id", return_value="asst"
            ), patch.object(vc, "_http_json") as http, patch.object(
                vc, "force_get_tradein_valuation", return_value=forced
            ) as force_ti, patch.object(
                vc, "force_calculate_financing"
            ) as force_fin:
                result = vc.chat_with_beatriz(
                    text="¿En cuánto me valúan un Corolla 2020 con 50 mil km?",
                    phone="6143231198",
                    customer_name="Test",
                    branch="periferico",
                    vehicle_interest="Ford Mustang GT 2025",
                    store=store,
                )
            meta = store.get_meta("6143231198")
        http.assert_not_called()
        force_fin.assert_not_called()
        force_ti.assert_called_once()
        self.assertTrue(result.tradein_forced)
        self.assertIn("get_tradein_valuation", result.tools_called)
        self.assertIn("201,200", result.reply_text)
        self.assertNotIn("Mustang", result.reply_text)
        self.assertIn("Corolla", result.tradein_summary or "")
        self.assertIn("Mustang", result.interested_vehicle or "")
        self.assertIn("Corolla", str(meta.get("trade_in_label") or ""))


class TestAppointmentSkipsFinancing(unittest.TestCase):
    def test_cita_after_tradein_books_without_financing(self):
        from src.voice_gateway import vapi_chat as vc

        booked = {
            "ok": True,
            "speech": (
                "¡Perfecto, Test! Agendamos tu cita en Autosell Periférico "
                "(mañana a las 4 pm). Te esperamos."
            ),
            "tool": "book_appointment",
            "when": "mañana a las 4 pm",
            "vehicle": "Toyota Corolla 2020 LE",
            "tradein_note": (
                "Cita para valuación física / prueba de manejo - "
                "Toyota Corolla 2020 LE (Trade-in toma a cuenta: $201,200)"
            ),
            "crm": {"status": "created", "lead_id": 1, "dry_run": False},
            "branch": "periferico",
        }
        with tempfile.TemporaryDirectory() as tmp:
            store = VapiChatSessionStore(Path(tmp) / "chats.db")
            store.set_chat_id(
                "6143231198",
                "prev",
                meta={
                    "tradein_make": "Toyota",
                    "tradein_model": "Corolla",
                    "tradein_year": 2020,
                    "tradein_version": "LE",
                    "tradein_mileage_km": 50000,
                    "valor_compra": 201200,
                    "tradein_summary": (
                        "Estimación de toma a cuenta para Toyota Corolla 2020 LE "
                        "(50,000 km): ~$201,200 MXN"
                    ),
                },
            )
            with patch.object(vc, "_api_key", return_value="k"), patch.object(
                vc, "_assistant_id", return_value="asst"
            ), patch.object(vc, "_http_json") as http, patch.object(
                vc, "force_book_appointment", return_value=booked
            ) as book, patch.object(
                vc, "force_calculate_financing"
            ) as force_fin:
                result = vc.chat_with_beatriz(
                    text="Sí, quiero agendar una cita para mañana a las 4 pm",
                    phone="6143231198",
                    customer_name="Test",
                    branch="periferico",
                    store=store,
                )
        http.assert_not_called()
        force_fin.assert_not_called()
        book.assert_called_once()
        self.assertIn("book_appointment", result.tools_called)
        self.assertIn("Agendamos tu cita", result.reply_text)
        self.assertNotIn("precio del vehículo", result.reply_text.casefold())
        self.assertNotIn("ValueError", result.reply_text)


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
