"""Trade-in intent + forced Autométrica valuation tests."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.lead_routing import (
    extract_desired_vehicle,
    parse_payment_intent,
    parse_trade_in_details,
)
from src.quote_engine.autometrica import lookup_valor_compra
from src.voice_gateway.vapi_chat import (
    detect_tradein_intent,
    force_get_tradein_valuation,
    reset_chat_store_for_tests,
)
from src.whatsapp_worker.inbound import (
    QualificationSession,
    QualificationStore,
    STATE_AI_ACTIVE,
)


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
            chat_path = Path(tmp) / "chat.db"
            phone = "6147778899"
            with patch.dict(
                os.environ,
                {
                    "WA_QUALIFICATION_DB_PATH": str(qpath),
                    "VAPI_WA_CHAT_DB_PATH": str(chat_path),
                },
                clear=False,
            ):
                reset_chat_store_for_tests()
                store = QualificationStore(qpath)
                try:
                    store.save(
                        QualificationSession(
                            phone=phone,
                            instance="autosell_periferico",
                            state=STATE_AI_ACTIVE,
                            updated_at="2026-01-01T00:00:00Z",
                        )
                    )
                    out = force_get_tradein_valuation(
                        text=(
                            "¿En cuánto me valúan un corolla 2020 "
                            "con 50 mil kilómetros?"
                        ),
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
                finally:
                    store.close()
                    reset_chat_store_for_tests()


class TestDesiredVehicle(unittest.TestCase):
    def test_aveo_purchase_intent(self):
        label = extract_desired_vehicle(
            "Hola. Tomas a cuenta vehiculos? Tengo un corolla 2020 le "
            "con 50000kms. Quiero un aveo 2020; lo vi en san felipe."
        )
        self.assertEqual(label, "Chevrolet Aveo 2020")

    def test_trade_in_only_no_desire(self):
        self.assertIsNone(
            extract_desired_vehicle(
                "Tengo un corolla 2020 le con 50000 kms a cuenta"
            )
        )

    def test_mustang_desire(self):
        self.assertEqual(
            extract_desired_vehicle("Quiero un mustang 2024"),
            "Ford Mustang 2024",
        )

    def test_chat_binds_aveo_before_tradein(self):
        from src.voice_gateway.vapi_chat import (
            VapiChatSessionStore,
            chat_with_beatriz,
            reset_chat_store_for_tests,
        )

        with tempfile.TemporaryDirectory() as tmp:
            qpath = Path(tmp) / "q.db"
            chat_path = Path(tmp) / "chat.db"
            phone = "6149998877"
            with patch.dict(
                os.environ,
                {
                    "WA_QUALIFICATION_DB_PATH": str(qpath),
                    "VAPI_WA_CHAT_DB_PATH": str(chat_path),
                    "VAPI_API_KEY": "",
                },
                clear=False,
            ):
                reset_chat_store_for_tests()
                store = QualificationStore(qpath)
                chat = VapiChatSessionStore(chat_path)
                try:
                    store.save(
                        QualificationSession(
                            phone=phone,
                            instance="autosell_san_felipe",
                            state=STATE_AI_ACTIVE,
                            branch="san_felipe",
                            updated_at="2026-01-01T00:00:00Z",
                        )
                    )
                    with patch(
                        "src.voice_gateway.vapi_chat.force_get_tradein_valuation",
                        return_value={
                            "ok": True,
                            "speech": (
                                "Estimación de toma a cuenta para Toyota Corolla "
                                "2020 LE (50,000 km): ~$201,200 MXN"
                            ),
                            "details": {
                                "make": "Toyota",
                                "model": "Corolla",
                                "year": 2020,
                                "mileage_km": 50000,
                                "version": "LE",
                            },
                        },
                    ), patch(
                        "src.inventory.catalog_match.load_public_catalog",
                        return_value=[],
                    ):
                        result = chat_with_beatriz(
                            text=(
                                "Tomas a cuenta? Tengo un corolla 2020 le "
                                "con 50000kms. Quiero un aveo 2020; lo vi "
                                "en san felipe."
                            ),
                            phone=phone,
                            instance="autosell_san_felipe",
                            branch="san_felipe",
                            store=chat,
                        )
                    meta = chat.get_meta(phone, "autosell_san_felipe")
                    interested = str(meta.get("interested_vehicle") or "")
                    self.assertIn("Aveo", interested)
                    self.assertIn("2020", interested)
                    self.assertTrue(
                        result.tradein_sent
                        or meta.get("trade_in_label")
                        or meta.get("tradein_summary"),
                        msg=f"trade-in missing: result={result!r} meta={meta}",
                    )
                    sess = store.get(phone, "autosell_san_felipe")
                    assert sess is not None
                    self.assertIn("Aveo", sess.vehicle_interest or "")
                finally:
                    store.close()
                    reset_chat_store_for_tests()


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
