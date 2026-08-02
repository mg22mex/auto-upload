"""Integration-style unit tests — calendar events + mail.activity (mocked XML-RPC)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.odoo_sync.client import OdooCRMClient, TestDriveEventResult


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


class TestScheduleActivity(unittest.TestCase):
    def test_creates_mail_activity_payload(self):
        models = MagicMock()

        def execute_kw(db, uid, key, model, method, args, kwargs=None):
            if model == "ir.model.data" and method == "check_object_reference":
                return ["mail.activity.type", 3]
            if model == "ir.model" and method == "search":
                return [99]
            if model == "mail.activity" and method == "create":
                vals = args[0]
                self.assertEqual(vals["res_model"], "crm.lead")
                self.assertEqual(vals["res_id"], 501)
                self.assertEqual(vals["activity_type_id"], 3)
                self.assertIn("Seguimiento post-cotización", vals["summary"])
                self.assertEqual(vals["user_id"], 42)
                self.assertIn("date_deadline", vals)
                return 888
            raise AssertionError(f"unexpected {model}.{method}")

        models.execute_kw.side_effect = execute_kw
        client = _client(models)
        # Force assignee via env-style path: pass user_id
        activity_id = client.schedule_activity(
            501,
            summary=OdooCRMClient.DEFAULT_ACTIVITY_SUMMARY,
            activity_kind="call",
            user_id=42,
            branch_id=1,
        )
        self.assertEqual(activity_id, 888)

    def test_dry_run_skips_create(self):
        models = MagicMock()
        client = _client(models, dry_run=True)
        activity_id = client.schedule_activity(501, user_id=42)
        self.assertEqual(activity_id, -1)
        models.execute_kw.assert_not_called()

    def test_failure_returns_none_not_raise(self):
        models = MagicMock()

        def execute_kw(*_a, **_k):
            raise RuntimeError("boom")

        models.execute_kw.side_effect = execute_kw
        client = _client(models)
        self.assertIsNone(client.schedule_activity(501, user_id=42))


class TestCreateTestDriveEvent(unittest.TestCase):
    def test_builds_calendar_event_and_advances_stage(self):
        models = MagicMock()
        created: dict = {}

        def execute_kw(db, uid, key, model, method, args, kwargs=None):
            if model == "crm.lead" and method == "read":
                return [
                    {
                        "id": 501,
                        "partner_id": [77, "Ana"],
                        "contact_name": "Ana",
                        "phone": "6141234567",
                        "user_id": [42, "Advisor"],
                        "name": "Ana — CX-5",
                    }
                ]
            if model == "res.users" and method == "read":
                return [{"id": 42, "partner_id": [88, "Advisor Partner"]}]
            if model == "calendar.event" and method == "create":
                created["event"] = args[0]
                return 9001
            if model == "crm.stage" and method == "search_read":
                return [{"id": 15, "name": "Cita/Prueba de manejo"}]
            if model == "crm.lead" and method == "write":
                created["stage_write"] = args
                return True
            if model == "ir.model.data" and method == "check_object_reference":
                return ["mail.activity.type", 5]
            if model == "ir.model" and method == "search":
                return [99]
            if model == "mail.activity" and method == "create":
                created["activity"] = args[0]
                return 777
            raise AssertionError(f"unexpected {model}.{method} args={args}")

        models.execute_kw.side_effect = execute_kw
        client = _client(models)
        result = client.create_test_drive_event(
            lead_id=501,
            vehicle_model="Mazda CX-5",
            customer_name="Ana Pérez",
            start="2026-08-10T16:00:00-06:00",
            stop="2026-08-10T17:00:00-06:00",
            branch_id=1,
        )
        self.assertIsInstance(result, TestDriveEventResult)
        self.assertEqual(result.event_id, 9001)
        self.assertTrue(result.stage_updated)
        self.assertEqual(result.activity_id, 777)
        self.assertIsNone(result.error)

        event = created["event"]
        self.assertEqual(
            event["name"],
            "Prueba de Manejo - Mazda CX-5 - Ana Pérez",
        )
        self.assertEqual(event["opportunity_id"], 501)
        self.assertEqual(event["user_id"], 42)
        self.assertEqual(event["partner_ids"], [(6, 0, [77, 88])])
        self.assertIn("2026-08-10", event["start"])
        self.assertEqual(created["stage_write"][0], [501])
        self.assertEqual(created["stage_write"][1]["stage_id"], 15)
        self.assertIn("Confirmación de Cita", created["activity"]["summary"])

    def test_dry_run_does_not_mutate(self):
        models = MagicMock()
        client = _client(models, dry_run=True)
        result = client.create_test_drive_event(
            lead_id=501,
            vehicle_model="Vento",
            customer_name="Luis",
            start="2026-08-11T10:00:00",
        )
        self.assertTrue(result.dry_run)
        self.assertEqual(result.event_id, -1)
        models.execute_kw.assert_not_called()

    def test_calendar_failure_is_soft(self):
        models = MagicMock()

        def execute_kw(db, uid, key, model, method, args, kwargs=None):
            if model == "crm.lead" and method == "read":
                return [
                    {
                        "partner_id": False,
                        "contact_name": "X",
                        "phone": "1",
                        "user_id": False,
                        "name": "X",
                    }
                ]
            if model == "res.partner" and method == "create":
                return 10
            if model == "crm.lead" and method == "write":
                return True
            if model == "calendar.event" and method == "create":
                raise RuntimeError("calendar unavailable")
            return []

        models.execute_kw.side_effect = execute_kw
        client = _client(models)
        result = client.create_test_drive_event(
            lead_id=9,
            vehicle_model="CX-5",
            customer_name="X",
            start="2026-08-12T12:00:00Z",
            schedule_confirmation_activity=False,
        )
        self.assertIsNone(result.event_id)
        self.assertIsNotNone(result.error)
        self.assertIn("calendar", result.error.lower())


if __name__ == "__main__":
    unittest.main()
