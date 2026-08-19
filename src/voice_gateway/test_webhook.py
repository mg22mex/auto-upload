"""Unit tests — voice webhook payload parsing + FastAPI routes."""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

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
        with patch.dict("os.environ", {"WHATSAPP_QUALIFICATION": "false"}, clear=False):
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
        with patch.dict("os.environ", {"WHATSAPP_QUALIFICATION": "false"}, clear=False):
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

    def test_whatsapp_qualification_webhook_san_felipe(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi not installed")

        from src.odoo_sync.client import QuoteLeadResult
        from src.whatsapp_worker.inbound import (
            QualificationStore,
            STATE_AWAITING_DOWN_PAYMENT,
            STATE_AWAITING_PAYMENT_METHOD,
        )

        odoo = MagicMock()
        odoo.authenticate.return_value = 1
        odoo.create_or_update_lead.return_value = QuoteLeadResult(
            lead_id=501, activity_id=None, tag_ids=()
        )
        whatsapp = MagicMock()
        whatsapp.send_text_message.return_value = {"ok": True}
        store = QualificationStore(":memory:")

        with patch.dict(
            "os.environ",
            {
                "WHATSAPP_QUALIFICATION": "true",
                "WHATSAPP_INSTANCE_SAN_FELIPE": "autosell_san_felipe",
                "ODOO_TEAM_SAN_FELIPE": "5",
            },
            clear=False,
        ):
            client = TestClient(
                create_app(
                    pipeline=MagicMock(),
                    qualification_store=store,
                    odoo_client=odoo,
                    whatsapp_client=whatsapp,
                )
            )
            r1 = client.post(
                "/webhook/whatsapp",
                json={
                    "event": "messages.upsert",
                    "instance": "autosell_san_felipe",
                    "data": {
                        "key": {
                            "remoteJid": "5216149998888@s.whatsapp.net",
                            "fromMe": False,
                            "id": "Q1",
                        },
                        "pushName": "Luis",
                        "message": {"conversation": "Precio Hilux"},
                    },
                },
            )
            self.assertEqual(r1.status_code, 200)
            body1 = r1.json()["results"][0]
            self.assertEqual(body1["qualification_state"], STATE_AWAITING_PAYMENT_METHOD)
            self.assertTrue(body1["auto_reply_sent"])
            odoo.create_or_update_lead.assert_called_once()

            r2 = client.post(
                "/webhook/whatsapp",
                json={
                    "event": "messages.upsert",
                    "instance": "autosell_san_felipe",
                    "data": {
                        "key": {
                            "remoteJid": "5216149998888@s.whatsapp.net",
                            "fromMe": False,
                            "id": "Q2",
                        },
                        "pushName": "Luis",
                        "message": {"conversation": "financiamiento"},
                    },
                },
            )
            body2 = r2.json()["results"][0]
            self.assertEqual(body2["qualification_state"], STATE_AWAITING_DOWN_PAYMENT)
            self.assertEqual(body2["branch_id"], 5)

        store.close()


@unittest.skipUnless(_HAS_WEBHOOK_DEPS, "fastapi/dotenv not installed")
class TestInboundCallParsing(unittest.TestCase):
    def test_parse_json_fields(self):
        from src.voice_gateway.inbound_call import parse_inbound_call_payload

        event = parse_inbound_call_payload(
            {
                "caller_number": "+526141111111",
                "called_number": "+526142222222",
                "call_sid": "CA123",
                "call_status": "ringing",
                "duration_sec": 42,
            }
        )
        self.assertEqual(event.caller_phone, "+526141111111")
        self.assertEqual(event.called_number, "+526142222222")
        self.assertEqual(event.call_sid, "CA123")
        self.assertEqual(event.call_status, "ringing")
        self.assertEqual(event.duration_sec, 42)

    def test_parse_twilio_form_keys(self):
        from src.voice_gateway.inbound_call import parse_inbound_call_payload

        event = parse_inbound_call_payload(
            {
                "From": "+526141111111",
                "To": "+526149876543",
                "CallSid": "CA999",
                "CallStatus": "in-progress",
                "CallerName": "María",
            }
        )
        self.assertEqual(event.caller_phone, "+526141111111")
        self.assertEqual(event.called_number, "+526149876543")
        self.assertEqual(event.caller_name, "María")

    def test_missing_caller_raises(self):
        from src.voice_gateway.inbound_call import parse_inbound_call_payload

        with self.assertRaises(ValueError):
            parse_inbound_call_payload({"To": "+526141234567"})


@unittest.skipUnless(_HAS_WEBHOOK_DEPS, "fastapi/dotenv not installed")
class TestInboundCallBranchRouting(unittest.TestCase):
    def test_did_periferico(self):
        from src.voice_gateway.inbound_call import (
            branch_context_for_inbound_call,
            parse_inbound_call_payload,
        )

        with patch.dict(
            "os.environ",
            {"VOICE_DID_PERIFERICO": "6141234567", "ODOO_TEAM_PERIFERICO": "1"},
            clear=False,
        ):
            event = parse_inbound_call_payload(
                {
                    "caller_number": "6145551234",
                    "called_number": "+526141234567",
                }
            )
            ctx = branch_context_for_inbound_call(event)
        self.assertEqual(ctx["branch"], "periferico")
        self.assertEqual(ctx["branch_id"], 1)
        self.assertEqual(ctx["physical_location"], "Periférico")

    def test_did_san_felipe(self):
        from src.voice_gateway.inbound_call import (
            branch_context_for_inbound_call,
            parse_inbound_call_payload,
        )

        with patch.dict(
            "os.environ",
            {
                "VOICE_DID_SAN_FELIPE": "6149876543",
                "ODOO_TEAM_SAN_FELIPE": "5",
                "VOICE_FORWARD_SAN_FELIPE": "+526142222222",
            },
            clear=False,
        ):
            event = parse_inbound_call_payload(
                {
                    "From": "+526145551234",
                    "To": "+526149876543",
                }
            )
            ctx = branch_context_for_inbound_call(event)
        self.assertEqual(ctx["branch"], "san_felipe")
        self.assertEqual(ctx["branch_id"], 5)
        self.assertEqual(ctx["forward_to"], "+526142222222")


@unittest.skipUnless(_HAS_WEBHOOK_DEPS, "fastapi/dotenv not installed")
class TestVoiceInboundWebhookHTTP(unittest.TestCase):
    def test_voice_inbound_periferico_json(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi not installed")

        crm = MagicMock()
        crm.log_inbound_call.return_value = {
            "lead_id": 100,
            "activity_id": 200,
            "team_id": 1,
            "status": "created",
        }
        with patch.dict(
            "os.environ",
            {
                "VOICE_DID_PERIFERICO": "6141234567",
                "ODOO_TEAM_PERIFERICO": "1",
                "VOICE_FORWARD_PERIFERICO": "+526141111111",
            },
            clear=False,
        ):
            client = TestClient(create_app(crm_manager=crm))
            resp = client.post(
                "/voice/inbound?format=json",
                json={
                    "caller_number": "6145551234",
                    "called_number": "+526141234567",
                    "CallStatus": "ringing",
                },
                headers={"Accept": "application/json"},
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["lead_id"], 100)
        self.assertEqual(body["activity_id"], 200)
        self.assertEqual(body["branch"], "periferico")
        self.assertEqual(body["branch_id"], 1)
        self.assertEqual(body["forward_to"], "+526141111111")
        crm.log_inbound_call.assert_called_once()
        kwargs = crm.log_inbound_call.call_args.kwargs
        self.assertEqual(kwargs["branch"], "periferico")
        self.assertEqual(kwargs["caller_phone"], "6145551234")

    def test_voice_inbound_san_felipe_twiml(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi not installed")

        crm = MagicMock()
        crm.log_inbound_call.return_value = {
            "lead_id": 101,
            "activity_id": 201,
            "team_id": 5,
            "status": "created",
        }
        with patch.dict(
            "os.environ",
            {
                "VOICE_DID_SAN_FELIPE": "6149876543",
                "ODOO_TEAM_SAN_FELIPE": "5",
                "VOICE_FORWARD_SAN_FELIPE": "+526142222222",
            },
            clear=False,
        ):
            client = TestClient(create_app(crm_manager=crm))
            resp = client.post(
                "/voice/inbound?format=twiml",
                json={
                    "From": "+526145551234",
                    "To": "+526149876543",
                    "CallSid": "CA-SF",
                    "CallStatus": "ringing",
                },
            )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("xml", resp.headers.get("content-type", ""))
        self.assertIn("<Dial", resp.text)
        self.assertIn("+526142222222", resp.text)
        self.assertIn("San Felipe", resp.text)
        kwargs = crm.log_inbound_call.call_args.kwargs
        self.assertEqual(kwargs["branch"], "san_felipe")
        self.assertEqual(kwargs["call_sid"], "CA-SF")


if __name__ == "__main__":
    unittest.main()
