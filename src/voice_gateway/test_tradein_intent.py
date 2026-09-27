"""Trade-in intent + forced Autométrica valuation tests."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.lead_routing import parse_payment_intent, parse_trade_in_details
from src.quote_engine.autometrica import lookup_valor_compra
from src.voice_gateway.vapi_chat import (
    detect_tradein_intent,
    force_get_tradein_valuation,
)
from src.voice_gateway.vapi_chat import VapiChatSessionStore
from src.whatsapp_worker.inbound import QualificationStore, QualificationSession, STATE_AI_ACTIVE


class TestTradeInIntentPhrases(unittest.TestCase):
    def test_valuation_phrases(self):
        phrases = [
            "¿En cuánto me valúan un corolla 2020 con 50 mil kilómetros?",
            "Cuanto me estimas un corolla 2020 con 50 mil kilometros",
            "cuanto me dan por mi corolla",
            "en cuanto me reciben el auto",
            "cuanto me agarran por la camioneta",
            "quiero dar a cuenta mi auto",
            "pueden tomar a cuenta mi corolla",
        ]
        for text in phrases:
            with self.subTest(text=text):
                self.assertTrue(detect_tradein_intent(text), text)
                self.assertTrue(parse_payment_intent(text).trade_in, text)


class TestParseCorollaQuery(unittest.TestCase):
    def test_parse_model_year_km_mil(self):
        details = parse_trade_in_details(
            "¿En cuánto me valúan un corolla 2020 con 50 mil kilómetros?"
        )
        self.assertEqual(details.year, 2020)
        self.assertEqual(details.make, "Toyota")
        self.assertEqual(details.model, "Corolla")
        self.assertEqual(details.mileage_km, 50000)
        self.assertEqual(details.version, "LE")  # baseline trim


class TestForceTradeinValuation(unittest.TestCase):
    def test_force_returns_autometrica_le(self):
        with tempfile.TemporaryDirectory() as tmp:
            qpath = Path(tmp) / "q.db"
            # Point qualification store via env for persist
            import os

            os.environ["WA_QUALIFICATION_DB_PATH"] = str(qpath)
            os.environ["VAPI_WA_CHAT_DB_PATH"] = str(Path(tmp) / "chat.db")
            phone = "6147778899"
            store = QualificationStore(qpath)
            store.save(
                QualificationSession(
                    phone=phone,
                    instance="autosell_periferico",
                    state=STATE_AI_ACTIVE,
                    updated_at="2026-01-01T00:00:00Z",
                )
            )
            out = force_get_tradein_valuation(
                text="¿En cuánto me valúan un corolla 2020 con 50 mil kilómetros?",
                phone=phone,
            )
            self.assertTrue(out["ok"])
            self.assertEqual(out.get("tool"), "get_tradein_valuation")
            speech = out["speech"]
            self.assertIn("201,200", speech)
            self.assertIn("Corolla 2020 LE", speech)
            self.assertIn("50,000", speech)
            self.assertTrue(out.get("results"))
            sess = store.get(phone, "autosell_periferico")
            assert sess is not None
            self.assertIn("Corolla", sess.trade_in_vehicle)
            self.assertTrue(
                sess.down_payment.startswith("201200")
                or "201200" in sess.down_payment.replace(",", "")
            )
            store.close()


class TestAutometricaBaseline(unittest.TestCase):
    def test_lookup_201200(self):
        from decimal import Decimal

        val = lookup_valor_compra(
            year=2020, make="Toyota", model="Corolla", mileage_km=50000
        )
        self.assertEqual(val.valor_compra, Decimal("201200.00"))
        self.assertEqual(val.version, "LE")


if __name__ == "__main__":
    unittest.main()
