"""Unit tests — voice webhook payload parsing + POST /webhook/voice-lead."""
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
from src.voice_gateway.webhook import create_app, parse_voice_lead_payload


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

    def test_missing_vehicle_price(self):
        with self.assertRaises(ValueError):
            parse_voice_lead_payload(
                {
                    "caller_phone": "6140000000",
                    "caller_name": "X",
                    "vehicle_interest": "Car only",
                }
            )


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
            steps=[{"step": "quote", "status": "ok"}],
        )
        client = TestClient(create_app(pipeline=pipeline))
        resp = client.post(
            "/webhook/voice-lead",
            json={
                "caller_phone": "6141234567",
                "caller_name": "Ana",
                "vehicle_interest": {"name": "CX-5", "price": 300000},
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
        pipeline.process_lead.assert_called_once()
        lead_arg = pipeline.process_lead.call_args.args[0]
        self.assertEqual(lead_arg["name"], "Ana")
        self.assertEqual(lead_arg["vehicle_name"], "CX-5")

    def test_invalid_payload_422(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi not installed")

        client = TestClient(create_app(pipeline=MagicMock()))
        resp = client.post("/webhook/voice-lead", json={"caller_name": "only"})
        self.assertEqual(resp.status_code, 422)


if __name__ == "__main__":
    unittest.main()
