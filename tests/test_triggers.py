"""Unit tests — OdooTriggerManager stage change + webhook (dry-run, mocked)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.odoo_sync.client import OdooCRMClient
from src.odoo_sync.crm import CRMLeadManager
from src.odoo_sync.quotes import QuotePDFManager
from src.odoo_sync.triggers import (
    OdooTriggerManager,
    is_quote_stage,
    process_incoming_webhook,
)


def _client(models: MagicMock | None = None, *, dry_run: bool = False) -> OdooCRMClient:
    models = models or MagicMock()
    client = OdooCRMClient(
        url="https://odoo.example",
        db="autosellmx",
        username="api",
        api_key="secret",
        common=MagicMock(),
        models=models,
        dry_run=dry_run,
    )
    client.uid = 7
    return client


LEAD_DATA = {
    "client_name": "Ana Pérez",
    "phone": "6141234567",
    "email_from": "ana@example.com",
    "vehicle_name": "Mazda CX-5 2020",
    "vin": "JM3KFBCM5L0123456",
    "sku": "obj969",
    "vehicle_price": 300000,
    "down_payment": 30000,
    "term_months": 36,
    "estimated_monthly_payment": 9956.11,
    "branch": "periferico",
    "physical_location": "periferico",
}


class TestStageHelpers(unittest.TestCase):
    def test_is_quote_stage(self):
        self.assertTrue(is_quote_stage("quoted"))
        self.assertTrue(is_quote_stage("Cotizado"))
        self.assertTrue(is_quote_stage("Quote Generated"))
        self.assertFalse(is_quote_stage("New"))
        self.assertFalse(is_quote_stage("won"))


class TestOnLeadStageChange(unittest.TestCase):
    def test_quoted_triggers_pdf_and_queues_whatsapp_dry_run(self):
        client = _client(dry_run=True)
        mgr = OdooTriggerManager(client=client, dry_run=True)
        result = mgr.on_lead_stage_change(501, "quoted", LEAD_DATA)

        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["action"], "quote_generated")
        self.assertTrue(result["dry_run"])
        self.assertIsNotNone(result["quote"])
        self.assertTrue(result["quote"]["ok"])
        self.assertTrue(result["quote"]["generate"]["ok"])
        self.assertTrue(result["quote"]["attach"]["dry_run"])

        wa = result["whatsapp"]
        self.assertIsNotNone(wa)
        self.assertEqual(wa["status"], "queued_pending_meta")
        self.assertFalse(wa["meta"]["ready"])
        self.assertEqual(wa["template_name"], "payment_link")
        self.assertEqual(wa["lead_id"], 501)
        self.assertEqual(len(mgr.whatsapp_queue), 1)

    def test_cotizado_alias_triggers(self):
        mgr = OdooTriggerManager(client=_client(dry_run=True), dry_run=True)
        result = mgr.on_lead_stage_change(10, "cotizado", LEAD_DATA)
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "quote_generated")

    def test_non_quote_stage_ignored(self):
        mgr = OdooTriggerManager(client=_client(dry_run=True), dry_run=True)
        result = mgr.on_lead_stage_change(10, "New", LEAD_DATA)
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "ignored")
        self.assertIsNone(result["quote"])
        self.assertEqual(mgr.whatsapp_queue, [])

    def test_quoted_no_network_on_dry_run(self):
        models = MagicMock()
        client = _client(models, dry_run=True)
        mgr = OdooTriggerManager(client=client, dry_run=True)
        mgr.on_lead_stage_change(1, "quoted", LEAD_DATA)
        models.execute_kw.assert_not_called()


class TestProcessIncomingWebhook(unittest.TestCase):
    def test_lead_form_creates_lead_dry_run(self):
        result = process_incoming_webhook(
            {
                "event": "lead_form",
                "client_name": "Luis",
                "phone": "6149998877",
                "vehicle_info": "Ranger",
                "branch": "periferico",
            },
            dry_run=True,
        )
        self.assertTrue(result["ok"], result.get("error"))
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["lead"]["status"], "created")
        self.assertTrue(result["lead"]["dry_run"])
        self.assertIsNone(result["stage_trigger"])

    def test_voice_note_channel(self):
        result = process_incoming_webhook(
            {
                "event": "voice_note",
                "caller_name": "Mia",
                "caller_phone": "6141112222",
                "vehicle_name": "Vento",
                "utterance": "Interesada a 24 meses",
            },
            dry_run=True,
        )
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["lead"]["deduplicated"], False)

    def test_webhook_with_trigger_quote(self):
        result = process_incoming_webhook(
            {
                "event": "inquiry",
                "client_name": "Rosa",
                "phone": "6147778899",
                "vehicle_name": "CX-5",
                "vehicle_price": 300000,
                "term_months": 36,
                "estimated_monthly_payment": 9000,
                "trigger_quote": True,
                "physical_location": "san_felipe",
            },
            dry_run=True,
        )
        self.assertTrue(result["ok"], result.get("error"))
        self.assertIsNotNone(result["stage_trigger"])
        self.assertEqual(result["stage_trigger"]["action"], "quote_generated")
        self.assertEqual(
            result["stage_trigger"]["whatsapp"]["status"],
            "queued_pending_meta",
        )

    def test_stage_change_webhook(self):
        result = process_incoming_webhook(
            {
                "event": "stage_change",
                "lead_id": 900,
                "new_stage": "quoted",
                "vehicle_name": "CX-5",
                "vehicle_price": 250000,
                "client_name": "Ana",
                "phone": "6140000001",
                "estimated_monthly_payment": 8000,
            },
            dry_run=True,
        )
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["stage_trigger"]["action"], "quote_generated")
        self.assertIsNone(result["lead"])

    def test_san_felipe_location_routing_on_webhook(self):
        """Webhook inquire for SF unit keeps physical_location in CRM path."""
        models = MagicMock()
        client = _client(models, dry_run=False)
        # Force CRM dry path by using dry_run manager while still testing payload mapping
        mgr = OdooTriggerManager(client=_client(dry_run=True), dry_run=True)

        with patch.object(
            CRMLeadManager,
            "create_or_update_lead",
            return_value={
                "status": "created",
                "lead_id": -1,
                "deduplicated": False,
                "branch": "san_felipe",
                "physical_location": "san_felipe",
                "dry_run": True,
            },
        ) as mock_create:
            result = process_incoming_webhook(
                {
                    "event": "vehicle_inquiry",
                    "client_name": "Pedro",
                    "phone": "6145551212",
                    "vehicle_info": "Q5",
                    "vin": "WAUZZZxxx",
                    "branch": "periferico",
                    "physical_location": "san_felipe",
                },
                manager=mgr,
            )
        self.assertTrue(result["ok"])
        mock_create.assert_called_once()
        args, kwargs = mock_create.call_args
        self.assertEqual(kwargs.get("branch") or args[1], "periferico")
        payload = args[0]
        self.assertEqual(payload.get("physical_location"), "san_felipe")

    def test_invalid_payload(self):
        result = process_incoming_webhook("not-a-dict")  # type: ignore[arg-type]
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
