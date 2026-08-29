"""Unit tests — standalone inbound WhatsApp receiver + mounted Odoo route."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.whatsapp_worker.inbound import QualificationStore
from src.whatsapp_worker.webhook import create_app


class _LeadResult:
    def __init__(self, lead_id: int) -> None:
        self.lead_id = lead_id


class _FakeOdoo:
    def __init__(self) -> None:
        self.leads: list[tuple] = []

    def authenticate(self) -> int:
        return 1

    def create_or_update_lead(self, name, phone, message, branch_id, **kwargs):
        self.leads.append((name, phone, message, branch_id))
        return _LeadResult(101)


class _FakeWhatsApp:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send_text_message(self, phone, text, *, branch=None, instance=None):
        self.sent.append({"phone": phone, "text": text})
        return {"ok": True}


def _payload(text: str) -> dict:
    return {
        "event": "messages.upsert",
        "instance": "autosell_periferico",
        "data": {
            "key": {"remoteJid": "5216145550000@s.whatsapp.net", "id": "M1", "fromMe": False},
            "pushName": "Cliente",
            "message": {"conversation": text},
        },
    }


class TestWhatsAppWorkerWebhook(unittest.TestCase):
    def setUp(self) -> None:
        self.store = QualificationStore(":memory:")
        self.odoo = _FakeOdoo()
        self.whatsapp = _FakeWhatsApp()
        self.client = TestClient(
            create_app(
                qualification_store=self.store,
                odoo_client=self.odoo,
                whatsapp_client=self.whatsapp,
            )
        )

    def tearDown(self) -> None:
        self.store.close()

    def test_health(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["worker"], "whatsapp")

    def test_first_message_creates_lead_and_replies(self):
        response = self.client.post("/webhook/whatsapp", json=_payload("Hola, el CX-30"))

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["processed"], 1)
        result = body["results"][0]
        self.assertEqual(result["lead_id"], 101)
        self.assertTrue(result["auto_reply_sent"])
        self.assertIsNone(result["rep_notification"])
        self.assertEqual(len(self.whatsapp.sent), 1)

    def test_cash_choice_hands_off_and_alerts_rep(self):
        self.client.post("/webhook/whatsapp", json=_payload("Hola, el CX-30"))
        with patch.dict(
            "os.environ",
            {"REPS_PERIFERICO": '[{"odoo_id": 1, "phone": "+526141111111"}]'},
        ):
            response = self.client.post("/webhook/whatsapp", json=_payload("contado"))

        result = response.json()["results"][0]
        self.assertEqual(result["qualification_state"], "HANDOFF_TO_HUMAN")
        self.assertTrue(result["rep_notification"]["sent"])
        rep_messages = [m for m in self.whatsapp.sent if "Nuevo Lead Asignado" in m["text"]]
        self.assertEqual(len(rep_messages), 1)
        self.assertEqual(rep_messages[0]["phone"], "+526141111111")

    def test_non_message_payload_is_ignored(self):
        response = self.client.post("/webhook/whatsapp", json={"event": "connection.update"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["processed"], 0)
        self.assertEqual(response.json()["ignored"], 1)

    def test_invalid_body_rejected(self):
        self.assertEqual(self.client.post("/webhook/whatsapp", json=[1, 2]).status_code, 400)


class TestOdooRouteMounted(unittest.TestCase):
    def test_voice_gateway_exposes_odoo_webhook(self):
        from src.voice_gateway.webhook import create_app as create_voice_app

        paths = {route.path for route in create_voice_app().routes}

        self.assertIn("/webhook/odoo", paths)
        self.assertIn("/odoo/webhook", paths)

    def test_odoo_webhook_dispatches_payload(self):
        from src.voice_gateway.webhook import create_app as create_voice_app

        captured: dict = {}

        def _fake_process(payload, **kwargs):
            captured["payload"] = payload
            return {"ok": True, "event": "lead", "lead": {"lead_id": 7}, "error": None}

        client = TestClient(create_voice_app(crm_manager=object()))
        with patch("src.voice_gateway.webhook.process_incoming_webhook", _fake_process):
            response = client.post(
                "/webhook/odoo",
                json={"event": "lead", "phone": "+526145550000", "client_name": "Ana"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(captured["payload"]["client_name"], "Ana")


if __name__ == "__main__":
    unittest.main()
