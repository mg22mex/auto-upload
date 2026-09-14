"""Unit tests — Vapi dialer + call-status parsing (mocked HTTP)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.voice_gateway.config import VapiConfig
from src.voice_gateway.dialer import (
    OutboundCallRequest,
    VapiDialer,
    VoiceCallStore,
    handle_vapi_call_status,
    normalize_customer_e164,
    parse_vapi_call_status,
    place_vapi_outbound_call,
)


class TestNormalizePhone(unittest.TestCase):
    def test_mx_local_gets_country(self):
        self.assertEqual(normalize_customer_e164("6142274381"), "+526142274381")

    def test_already_e164(self):
        self.assertEqual(normalize_customer_e164("+52 614 227 4381"), "+526142274381")


class TestVapiDialer(unittest.TestCase):
    def test_dry_run_skips_http(self):
        session = MagicMock()
        cfg = VapiConfig(api_key="k", phone_number_id="pn_1")
        with tempfile.TemporaryDirectory() as tmp:
            store = VoiceCallStore(Path(tmp) / "sync.db")
            dialer = VapiDialer(cfg, session=session, store=store)
            result = dialer.place_call(
                OutboundCallRequest(
                    customer_phone="6141234567",
                    lead_id=1937,
                    vehicle_of_interest="Camioneta",
                    valuation_amount="100",
                    monthly_payment="50",
                ),
                dry_run=True,
            )
        session.post.assert_not_called()
        self.assertTrue(result.ok)
        self.assertTrue(result.dry_run)
        self.assertEqual(result.customer_number, "+526141234567")

    def test_live_posts_vapi_payload(self):
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 201
        resp.content = b'{"id":"call_abc","status":"queued"}'
        resp.json.return_value = {"id": "call_abc", "status": "queued"}
        session.post.return_value = resp
        cfg = VapiConfig(api_key="secret", phone_number_id="pn_mx_614", assistant_id="asst_1")
        with tempfile.TemporaryDirectory() as tmp:
            store = VoiceCallStore(Path(tmp) / "sync.db")
            dialer = VapiDialer(cfg, session=session, store=store)
            result = dialer.place_call(
                OutboundCallRequest(
                    customer_phone="+526149998877",
                    lead_id=1937,
                    vehicle_of_interest="Camioneta",
                    valuation_amount="194500.00",
                    monthly_payment="9212.13",
                    agent_script="Hola test",
                ),
                dry_run=False,
            )
            self.assertTrue(result.ok)
            self.assertEqual(result.call_id, "call_abc")
            session.post.assert_called_once()
            args, kwargs = session.post.call_args
            self.assertEqual(args[0], "https://api.vapi.ai/call/phone")
            body = kwargs["json"]
            self.assertEqual(body["phoneNumberId"], "pn_mx_614")
            self.assertEqual(body["customer"]["number"], "+526149998877")
            self.assertEqual(body["assistantId"], "asst_1")
            vars_ = body["assistantOverrides"]["variableValues"]
            self.assertEqual(vars_["lead_id"], "1937")
            self.assertEqual(vars_["valuation_amount"], "194500.00")
            self.assertEqual(vars_["monthly_payment"], "9212.13")
            self.assertEqual(vars_["vehicle_of_interest"], "Camioneta")
            self.assertIn("Bearer secret", kwargs["headers"]["Authorization"])
            row = store.get_by_call_id("call_abc")
            self.assertIsNotNone(row)
            self.assertEqual(row["lead_id"], 1937)


class TestCallStatus(unittest.TestCase):
    def test_parse_structured_data(self):
        payload = {
            "message": {
                "type": "end-of-call-report",
                "call": {
                    "id": "call_xyz",
                    "customer": {"number": "+526141112233"},
                    "status": "ended",
                    "analysis": {
                        "structuredData": {
                            "appointment_time": "mañana 11am",
                            "appointment_confirmed": True,
                            "lead_id": 1937,
                            "vehicle_of_interest": "Camioneta",
                            "valuation_amount": "194500.00",
                            "monthly_payment": "9212.13",
                        }
                    },
                },
            }
        }
        parsed = parse_vapi_call_status(payload)
        self.assertEqual(parsed["call_id"], "call_xyz")
        self.assertEqual(parsed["appointment_time"], "mañana 11am")
        self.assertTrue(parsed["appointment_confirmed"])
        self.assertEqual(parsed["lead_id"], 1937)

    def test_handle_persists_and_handoffs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = VoiceCallStore(Path(tmp) / "sync.db")
            store.insert_outbound(
                phone="+526141112233",
                lead_id=1937,
                vehicle_of_interest="Camioneta",
                valuation_amount="194500.00",
                monthly_payment="9212.13",
                status="queued",
                vapi_call_id="call_xyz",
            )
            payload = {
                "call": {
                    "id": "call_xyz",
                    "customer": {"number": "+526141112233"},
                    "analysis": {
                        "structuredData": {
                            "appointment_time": "hoy 4pm",
                            "appointment_confirmed": True,
                        }
                    },
                }
            }
            with patch(
                "src.lead_routing.complete_voice_appointment_handoff"
            ) as complete:
                complete.return_value = MagicMock(
                    as_dict=lambda: {"handoff_to_advisor": True, "status": "ok"}
                )
                result = handle_vapi_call_status(
                    payload, store=store, complete_handoff=True
                )
            self.assertTrue(result["handoff_to_advisor"])
            self.assertEqual(result["status"], "handoff_complete")
            row = store.get_by_call_id("call_xyz")
            self.assertEqual(row["appointment_time"], "hoy 4pm")
            complete.assert_called_once()


if __name__ == "__main__":
    unittest.main()
