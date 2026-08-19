"""Unit tests — voice webhook payload parsing + FastAPI routes."""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.pipeline import PipelineResult
from src.voice_gateway.intent import VOICE_CHANNEL

try:
    from src.voice_gateway.webhook import create_app, parse_voice_lead_payload

    _HAS_WEBHOOK_DEPS = True
except ImportError:  # pragma: no cover — minimal env without fastapi/dotenv
    create_app = None  # type: ignore[assignment]
    parse_voice_lead_payload = None  # type: ignore[assignment]
    _HAS_WEBHOOK_DEPS = False


@unittest.skipUnless(_HAS_WEBHOOK_DEPS, "fastapi/dotenv not installed")
class TestParseVoiceLeadPayload(unittest.TestCase):
    def test_full_payload(self):
        lead = parse_voice_lead_payload(
            {
                "caller_phone": "614-123-4567",
                "caller_name": "Ana Pérez",
                "vehicle_interest": {
                    "name": "Mazda CX-5 2020",
                    "price": 300000,
                },
                "term": 36,
                "down_payment": 30000,
                "trade_in_info": {
                    "year": 2018,
                    "make": "Nissan",
                    "model": "Sentra",
                    "mileage_km": 72000,
                    "outstanding_lien": 20000,
                    "manual_guide_value": 70000,
                },
                "branch_id": 3,
            }
        )
        self.assertEqual(lead["phone"], "614-123-4567")
        self.assertEqual(lead["name"], "Ana Pérez")
        self.assertEqual(lead["vehicle_name"], "Mazda CX-5 2020")
        self.assertEqual(lead["vehicle_price"], 300000)
        self.assertEqual(lead["term_months"], 36)
        self.assertEqual(lead["down_payment"], 30000)
        self.assertEqual(lead["branch_id"], 3)
        self.assertEqual(lead["channel"], VOICE_CHANNEL)
        self.assertEqual(lead["trade_in"]["make"], "Nissan")
        self.assertEqual(lead["trade_in"]["manual_guide_value"], 70000)

    def test_string_vehicle_interest(self):
        lead = parse_voice_lead_payload(
            {
                "caller_phone": "526141111111",
                "caller_name": "Luis",
                "vehicle_interest": "Vento 2018",
                "vehicle_price": 150000,
                "term": 12,
            }
        )
        self.assertEqual(lead["vehicle_name"], "Vento 2018")
        self.assertEqual(lead["vehicle_price"], 150000)
        self.assertEqual(lead["term_months"], 12)
        self.assertNotIn("trade_in", lead)

    def test_missing_phone(self):
        with self.assertRaises(ValueError):
            parse_voice_lead_payload(
                {
                    "caller_name": "X",
                    "vehicle_interest": "Car",
                    "vehicle_price": 1,
                }
            )

    def test_missing_vehicle_price_allows_odoo_resolve(self):
        lead = parse_voice_lead_payload(
            {
                "caller_phone": "6140000000",
                "caller_name": "X",
                "vehicle_interest": "Car only",
            }
        )
        self.assertEqual(lead["vehicle_name"], "Car only")
        self.assertNotIn("vehicle_price", lead)

    def test_missing_price_strict_when_resolve_disabled(self):
        with self.assertRaises(ValueError):
            parse_voice_lead_payload(
                {
                    "caller_phone": "6140000000",
                    "caller_name": "X",
                    "vehicle_interest": "Car only",
                    "resolve_price_from_odoo": False,
                }
            )


@unittest.skipUnless(_HAS_WEBHOOK_DEPS, "fastapi/dotenv not installed")
class TestVoiceWebhookHTTP(unittest.TestCase):
    def test_post_returns_200_json(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi not installed")

        pipeline = MagicMock()
        pipeline.process_lead.return_value = PipelineResult(
            ok=True,
            lead_id=501,
            advisor_user_id=42,
            net_trade_in_equity=Decimal("50000.00"),
            estimated_monthly_payment=Decimal("10891.67"),
            whatsapp_message="hola",
            channel=VOICE_CHANNEL,
            pdf_path="/tmp/quote.pdf",
            pdf_attachment_id=9001,
            vehicle_sku="obj969",
            steps=[
                {"step": "quote", "status": "ok", "down_payment": "30000.00"},
                {"step": "pdf_spec_sheet", "status": "ok", "attachment_id": 9001},
            ],
        )
        client = TestClient(create_app(pipeline=pipeline))
        resp = client.post(
            "/webhook/voice-lead",
            json={
                "caller_phone": "6141234567",
                "caller_name": "Ana",
                "vehicle_interest": {
                    "name": "CX-5",
                    "price": 300000,
                    "sku": "obj969",
                },
                "term": 36,
                "branch_id": 3,
            },
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["confirmation"], "voice lead processed")
        self.assertEqual(body["lead_id"], 501)
        self.assertEqual(body["estimated_monthly_payment"], "10891.67")
        self.assertEqual(body["channel"], VOICE_CHANNEL)
        self.assertEqual(body["pdf_attachment_id"], 9001)
        self.assertIn("mensualidad", body["tts_text"].lower())
        pipeline.process_lead.assert_called_once()
        lead_arg = pipeline.process_lead.call_args.args[0]
        self.assertEqual(lead_arg["name"], "Ana")
        self.assertEqual(lead_arg["vehicle_name"], "CX-5")
        self.assertEqual(lead_arg["channel"], VOICE_CHANNEL)

    def test_voice_webhook_alias_and_stream(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi not installed")

        pipeline = MagicMock()
        pipeline.process_lead.return_value = PipelineResult(
            ok=True,
            lead_id=77,
            estimated_monthly_payment=Decimal("5000.00"),
            vehicle_sku="sku1",
            steps=[{"step": "quote", "status": "ok"}],
        )
        client = TestClient(create_app(pipeline=pipeline))
        alias = client.post(
            "/voice/webhook",
            json={
                "caller_phone": "6141112222",
                "caller_name": "Mia",
                "vehicle_interest": "Vento",
                "vehicle_price": 150000,
            },
        )
        self.assertEqual(alias.status_code, 200)
        self.assertEqual(alias.json()["lead_id"], 77)

        stream = client.post(
            "/voice/stream",
            json={
                "caller_phone": "6141112222",
                "caller_name": "Mia",
                "vehicle_interest": "Vento",
                "vehicle_price": 150000,
                "utterance": "Quiero el Vento a 24 meses",
            },
        )
        self.assertEqual(stream.status_code, 200)
        self.assertEqual(stream.json()["status"], "ok")
        self.assertTrue(stream.json()["tts_text"])

    def test_degraded_audio_soft_capture(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi not installed")

        pipeline = MagicMock()
        pipeline.process_lead.return_value = PipelineResult(
            ok=True,
            lead_id=88,
            channel=VOICE_CHANNEL,
            steps=[
                {"step": "odoo_lead", "status": "ok", "soft_capture": True},
                {"step": "odoo_follow_up", "status": "ok", "activity_id": 1},
                {"step": "pdf_spec_sheet", "status": "skipped"},
            ],
        )
        client = TestClient(create_app(pipeline=pipeline))
        resp = client.post(
            "/webhook/voice-lead",
            json={
                "caller_phone": "6145556677",
                "caller_name": "Pedro",
                "audio_status": "failed",
                "stt_confidence": 0.05,
            },
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["mode"], "generic_capture")
        self.assertTrue(body["audio_degraded"])
        self.assertIn("degraded", body["confirmation"])
        lead_arg = pipeline.process_lead.call_args.args[0]
        self.assertTrue(lead_arg.get("soft_capture"))
        self.assertEqual(lead_arg.get("channel"), VOICE_CHANNEL)

    def test_invalid_payload_422(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi not installed")

        client = TestClient(create_app(pipeline=MagicMock()))
        resp = client.post("/webhook/voice-lead", json={"caller_name": "only"})
        self.assertEqual(resp.status_code, 422)

    def test_whatsapp_evolution_upsert_periferico(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi not installed")

        pipeline = MagicMock()
        pipeline.process_lead.return_value = PipelineResult(
            ok=True,
            lead_id=91,
            channel="WhatsApp",
        )
        client = TestClient(create_app(pipeline=pipeline))
        resp = client.post(
            "/webhook/whatsapp",
            json={
                "event": "messages.upsert",
                "instance": "autosell_periferico",
                "data": {
                    "key": {
                        "remoteJid": "5216141234567@s.whatsapp.net",
                        "fromMe": False,
                        "id": "WA1",
                    },
                    "pushName": "Ana",
                    "message": {"conversation": "Hola, cotiza un Vento 2018 precio 150000"},
                },
            },
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["processed"], 1)
        self.assertEqual(body["results"][0]["lead_id"], 91)
        self.assertEqual(body["results"][0]["branch"], "periferico")
        lead = pipeline.process_lead.call_args.args[0]
        self.assertEqual(lead["channel"], "WhatsApp")
        self.assertEqual(lead["branch"], "periferico")
        self.assertEqual(lead["physical_location"], "Periférico")

    def test_whatsapp_evolution_upsert_san_felipe(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi not installed")

        pipeline = MagicMock()
        pipeline.process_lead.return_value = PipelineResult(
            ok=True,
            lead_id=92,
            channel="WhatsApp",
        )
        client = TestClient(create_app(pipeline=pipeline))
        resp = client.post(
            "/webhook/whatsapp",
            json={
                "event": "messages.upsert",
                "instance": "autosell_san_felipe",
                "data": {
                    "key": {
                        "remoteJid": "5216149998888@s.whatsapp.net",
                        "fromMe": False,
                        "id": "WA2",
                    },
                    "pushName": "Luis",
                    "message": {"conversation": "Precio de una Hilux"},
                },
            },
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["results"][0]["branch"], "san_felipe")
        lead = pipeline.process_lead.call_args.args[0]
        self.assertEqual(lead["branch"], "san_felipe")
        self.assertEqual(lead["physical_location"], "San Felipe")
        self.assertTrue(lead.get("auto_reply"))

    def test_whatsapp_greeting_template_san_felipe(self):
        from src.whatsapp_worker.client import format_inbound_greeting

        text = format_inbound_greeting("San Felipe")
        self.assertIn("Autosell San Felipe", text)
        self.assertIn("vehículo", text.lower())


if __name__ == "__main__":
    unittest.main()
