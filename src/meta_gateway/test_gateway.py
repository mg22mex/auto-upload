"""Tests for Meta verification, Messenger parsing, quoting, and replies."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.meta_gateway.client import MessengerClient
from src.meta_gateway.gateway import (
    MetaWebhookGateway,
    parse_messenger_events,
)
from src.odoo_sync.client import QuoteLeadResult
from src.voice_gateway.webhook import create_app


MESSENGER_PAYLOAD = {
    "object": "page",
    "entry": [
        {
            "messaging": [
                {
                    "sender": {"id": "PSID-123"},
                    "message": {
                        "text": "Cotízame este vehículo a 36 meses",
                        "quick_reply": {
                            "payload": (
                                '{"vehicle_name":"Mazda CX-5 2020",'
                                '"vehicle_price":300000,"customer_name":"Ana"}'
                            )
                        },
                    },
                }
            ]
        }
    ],
}


class TestMessengerParsing(unittest.TestCase):
    def test_parse_sender_text_and_vehicle_context(self):
        events = parse_messenger_events(MESSENGER_PAYLOAD)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].sender_id, "PSID-123")
        self.assertEqual(events[0].context["vehicle_name"], "Mazda CX-5 2020")
        self.assertEqual(events[0].context["vehicle_price"], 300000)

    def test_ignore_echo_and_non_page_payload(self):
        self.assertEqual(parse_messenger_events({"object": "whatsapp_business_account"}), [])
        echo = {
            "object": "page",
            "entry": [
                {
                    "messaging": [
                        {
                            "sender": {"id": "1"},
                            "message": {"is_echo": True, "text": "echo"},
                        }
                    ]
                }
            ],
        }
        self.assertEqual(parse_messenger_events(echo), [])


class TestMessengerClient(unittest.TestCase):
    def test_graph_api_payload(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"message_id": "mid.1"}
        session = MagicMock()
        session.post.return_value = response
        client = MessengerClient(page_access_token="token", session=session)

        result = client.send_text_message("PSID-123", "Cotización")

        self.assertEqual(result["message_id"], "mid.1")
        call = session.post.call_args
        self.assertTrue(call.args[0].endswith("/me/messages"))
        self.assertEqual(call.kwargs["params"], {"access_token": "token"})
        self.assertEqual(call.kwargs["json"]["recipient"]["id"], "PSID-123")
        self.assertEqual(call.kwargs["json"]["message"]["text"], "Cotización")


class TestMetaGateway(unittest.TestCase):
    def test_quote_upserts_odoo_and_replies(self):
        odoo = MagicMock()
        odoo.create_or_update_lead.return_value = QuoteLeadResult(
            lead_id=501, activity_id=880, tag_ids=(9, 11)
        )
        odoo.post_quote_to_chatter.return_value = 701
        messenger = MagicMock()
        messenger.send_text_message.return_value = {"message_id": "mid.1"}
        gateway = MetaWebhookGateway(
            verify_token="verify",
            odoo=odoo,
            messenger=messenger,
            branch_id=3,
        )

        result = gateway.process_event(parse_messenger_events(MESSENGER_PAYLOAD)[0])

        self.assertEqual(result["status"], "quoted")
        self.assertEqual(result["lead_id"], 501)
        odoo.authenticate.assert_called_once()
        odoo.create_or_update_lead.assert_called_once()
        lead_kwargs = odoo.create_or_update_lead.call_args
        self.assertEqual(lead_kwargs.args[0], "Ana")
        self.assertEqual(lead_kwargs.args[1], "messenger:PSID-123")
        self.assertEqual(lead_kwargs.kwargs.get("channel"), "facebook_messenger")
        self.assertEqual(lead_kwargs.kwargs.get("term_months"), 36)
        self.assertEqual(lead_kwargs.kwargs.get("stage_name"), "Quote Generated")
        odoo.post_quote_to_chatter.assert_called_once()
        messenger.send_text_message.assert_called_once()
        self.assertIn("Pago mensual estimado", messenger.send_text_message.call_args.args[1])

    def test_verification_uses_exact_token(self):
        gateway = MetaWebhookGateway(
            verify_token="correct",
            odoo=MagicMock(),
            messenger=MagicMock(),
        )
        self.assertTrue(gateway.verify("subscribe", "correct"))
        self.assertFalse(gateway.verify("subscribe", "wrong"))
        self.assertFalse(gateway.verify("unsubscribe", "correct"))


class TestMetaWebhookHTTP(unittest.TestCase):
    def test_get_verification_and_post_ack(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi not installed")

        gateway = MagicMock()
        gateway.verify.side_effect = (
            lambda mode, token: mode == "subscribe" and token == "correct"
        )
        gateway.process_event.return_value = {"status": "quoted", "lead_id": 501}
        client = TestClient(create_app(pipeline=MagicMock(), meta_gateway=gateway))

        verified = client.get(
            "/webhook/facebook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "correct",
                "hub.challenge": "challenge-123",
            },
        )
        self.assertEqual(verified.status_code, 200)
        self.assertEqual(verified.text, "challenge-123")

        rejected = client.get(
            "/webhook/facebook",
            params={"hub.mode": "subscribe", "hub.verify_token": "wrong"},
        )
        self.assertEqual(rejected.status_code, 403)

        received = client.post("/webhook/facebook", json=MESSENGER_PAYLOAD)
        self.assertEqual(received.status_code, 200)
        self.assertEqual(received.json()["status"], "event_received")
        self.assertEqual(received.json()["processed"], 1)
        gateway.process_event.assert_called_once()


if __name__ == "__main__":
    unittest.main()
