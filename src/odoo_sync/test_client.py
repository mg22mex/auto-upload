"""Unit tests — OdooCRMClient with mocked XML-RPC."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.odoo_sync.client import OdooCRMClient, OdooCRMError, QuoteLeadResult


class TestAuthenticate(unittest.TestCase):
    def test_missing_env(self):
        client = OdooCRMClient(url="", db="", username="", api_key="")
        with self.assertRaises(OdooCRMError):
            client.authenticate()

    def test_authenticate_ok(self):
        common = MagicMock()
        common.authenticate.return_value = 42
        models = MagicMock()
        client = OdooCRMClient(
            url="https://odoo.example",
            db="autosell",
            username="api",
            api_key="secret",
            common=common,
            models=models,
        )
        uid = client.authenticate()
        self.assertEqual(uid, 42)
        common.authenticate.assert_called_once_with(
            "autosell", "api", "secret", {}
        )

    def test_authenticate_rejected(self):
        common = MagicMock()
        common.authenticate.return_value = False
        client = OdooCRMClient(
            url="https://odoo.example",
            db="autosell",
            username="api",
            api_key="bad",
            common=common,
            models=MagicMock(),
        )
        with self.assertRaises(OdooCRMError):
            client.authenticate()


def _lead_rpc_side_effect(
    *,
    lead_id: int = 1001,
    existing: list[int] | None = None,
    activity_id: int = 501,
    tag_id: int = 9,
    stage: dict | None = None,
    write_existing: bool = False,
):
    """Shared XML-RPC stub for lead upsert + follow-up activity."""
    tag_seq = {"n": 0}

    def execute_kw(db, uid, key, model, method, args, kwargs=None):
        if model == "crm.lead" and method == "search":
            return list(existing or [])
        if model == "crm.stage" and method == "search_read":
            return [stage] if stage else []
        if model == "crm.tag" and method == "search_read":
            # Distinct ids per tag name for messenger dual-tag cases
            name = args[0][0][2] if args and args[0] else ""
            tid = tag_id + (1 if "messenger" in str(name).lower() else 0)
            return [{"id": tid, "name": name}]
        if model == "crm.tag" and method == "create":
            tag_seq["n"] += 1
            return tag_id + tag_seq["n"]
        if model == "crm.lead" and method == "create":
            return lead_id
        if model == "crm.lead" and method == "write":
            return True
        if model == "ir.model.data" and method == "check_object_reference":
            return ["mail.activity.type", 3]
        if model == "mail.activity.type" and method == "search":
            return [3]
        if model == "ir.model" and method == "search":
            return [77]
        if model == "crm.team" and method == "read":
            return [{"id": 3, "member_ids": [42]}]
        if model == "mail.activity" and method == "create":
            return activity_id
        raise AssertionError(f"unexpected {model}.{method}")

    return execute_kw


class TestCreateOrUpdateLead(unittest.TestCase):
    def _client(self) -> tuple[OdooCRMClient, MagicMock]:
        models = MagicMock()
        client = OdooCRMClient(
            url="https://odoo.example",
            db="autosell",
            username="api",
            api_key="secret",
            common=MagicMock(authenticate=MagicMock(return_value=7)),
            models=models,
        )
        client.uid = 7
        return client, models

    def test_create_lead(self):
        client, models = self._client()
        models.execute_kw.side_effect = _lead_rpc_side_effect(lead_id=1001)
        result = client.create_or_update_lead(
            "Ana Pérez", "6141234567", "Mazda CX-5 2020", branch_id=3
        )
        self.assertIsInstance(result, QuoteLeadResult)
        self.assertEqual(result.lead_id, 1001)
        self.assertEqual(result.activity_id, 501)
        create_call = [
            c
            for c in models.execute_kw.call_args_list
            if c.args[3] == "crm.lead" and c.args[4] == "create"
        ][0]
        vals = create_call.args[5][0]
        self.assertEqual(vals["phone"], "6141234567")
        self.assertEqual(vals["team_id"], 3)
        self.assertIn("Mazda CX-5 2020", vals["description"])
        self.assertEqual(vals["contact_name"], "Ana Pérez")

    def test_create_lead_with_quote_fields(self):
        client, models = self._client()
        models.execute_kw.side_effect = _lead_rpc_side_effect(
            lead_id=1002,
            stage={"id": 12, "name": "Quote Generated"},
            tag_id=11,
        )
        result = client.create_or_update_lead(
            "Ana Pérez",
            "messenger:PSID-1",
            "Mazda CX-5 2020",
            branch_id=3,
            down_payment=30000,
            term_months=36,
            quote_summary="Pago mensual estimado: $9,956.11",
            stage_name="Quote Generated",
            channel="facebook_messenger",
            estimated_monthly_payment="9956.11",
            vehicle_price=300000,
        )
        self.assertEqual(result.lead_id, 1002)
        self.assertEqual(result.activity_id, 501)
        self.assertTrue(result.tag_ids)
        create_call = [
            c
            for c in models.execute_kw.call_args_list
            if c.args[3] == "crm.lead" and c.args[4] == "create"
        ][0]
        vals = create_call.args[5][0]
        self.assertEqual(vals["stage_id"], 12)
        self.assertEqual(vals["x_term_months"], 36)
        self.assertEqual(vals["x_down_payment"], 30000.0)
        self.assertIn("Requested term: 36 months", vals["description"])
        self.assertIn("Pago mensual estimado", vals["description"])
        self.assertIn("facebook_messenger", vals["description"])
        self.assertEqual(vals["tag_ids"][0][0], 6)
        self.assertEqual(vals["tag_ids"][0][1], 0)
        self.assertIn(11, vals["tag_ids"][0][2])
        self.assertIn(12, vals["tag_ids"][0][2])  # Messenger Bot

    def test_update_existing_lead(self):
        client, models = self._client()
        models.execute_kw.side_effect = _lead_rpc_side_effect(
            lead_id=55, existing=[55]
        )
        result = client.create_or_update_lead(
            "Ana Pérez", "6141234567", "Hilux 2023", branch_id=2
        )
        self.assertEqual(result.lead_id, 55)
        write_call = [
            c
            for c in models.execute_kw.call_args_list
            if c.args[3] == "crm.lead" and c.args[4] == "write"
        ][0]
        self.assertEqual(write_call.args[5][0], [55])
        self.assertEqual(write_call.args[5][1]["team_id"], 2)

    def test_phone_required(self):
        client, _ = self._client()
        with self.assertRaises(OdooCRMError):
            client.create_or_update_lead("X", "", "Car", 1)

    def test_schedules_follow_up_activity_payload(self):
        client, models = self._client()
        models.execute_kw.side_effect = _lead_rpc_side_effect(lead_id=88)
        result = client.create_or_update_lead(
            "Luis",
            "6140001111",
            "Vento 2018",
            branch_id=3,
            down_payment=20000,
            term_months=24,
            estimated_monthly_payment="7500.00",
            channel="voice_ai",
        )
        self.assertEqual(result.lead_id, 88)
        self.assertEqual(result.activity_id, 501)
        activity_call = [
            c
            for c in models.execute_kw.call_args_list
            if c.args[3] == "mail.activity" and c.args[4] == "create"
        ][0]
        payload = activity_call.args[5][0]
        self.assertEqual(payload["res_model"], "crm.lead")
        self.assertEqual(payload["res_id"], 88)
        self.assertEqual(payload["activity_type_id"], 3)
        self.assertEqual(payload["user_id"], 42)
        self.assertIn("Follow up on generated vehicle quote: Vento 2018", payload["summary"])
        self.assertIn("Down payment: 20000", payload["note"])
        self.assertIn("Loan term: 24 months", payload["note"])
        self.assertIn("Monthly payment: 7500.00", payload["note"])
        self.assertIn("Preferred channel: voice_ai", payload["note"])
        # Deadline is an ISO date ~24h out (weekday-adjusted)
        self.assertRegex(payload["date_deadline"], r"^\d{4}-\d{2}-\d{2}$")

    def test_follow_up_deadline_rolls_weekend(self):
        client, _ = self._client()
        # Force Saturday UTC morning → Monday
        from datetime import datetime, timezone
        from unittest.mock import patch

        saturday = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)  # Saturday
        with patch("src.odoo_sync.client.datetime") as mock_dt:
            mock_dt.now.return_value = saturday
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            deadline = client._follow_up_deadline(hours=24)
        # Sat + 24h = Sun → roll to Monday
        self.assertEqual(deadline.isoformat(), "2026-08-03")


class TestArchiveOrphans(unittest.TestCase):
    def test_archives_missing_skus_as_sold(self):
        models = MagicMock()
        models.execute_kw.side_effect = [
            [
                {
                    "id": 1,
                    "name": "Keep Me",
                    "default_code": "obj100",
                    "list_price": 1,
                    "categ_id": [8, "vehiculos"],
                },
                {
                    "id": 2,
                    "name": "Gone",
                    "default_code": "obj999",
                    "list_price": 2,
                    "categ_id": [8, "vehiculos"],
                },
            ],
            True,  # write sold + active=False via x_studio_state
        ]
        client = OdooCRMClient(
            url="https://odoo.example",
            db="autosell",
            username="api",
            api_key="secret",
            common=MagicMock(),
            models=models,
        )
        client.uid = 7
        archived = client.archive_orphan_vehicles({"obj100"}, categ_id=8)
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0]["default_code"], "obj999")
        self.assertEqual(archived[0]["inventory_status"], "sold")
        self.assertEqual(archived[0]["state_field"], "x_studio_state")
        self.assertEqual(archived[0]["state_value"], "sold")
        write = models.execute_kw.call_args_list[1]
        self.assertEqual(write.args[3], "product.template")
        self.assertEqual(write.args[4], "write")
        self.assertEqual(
            write.args[5],
            [[2], {"active": False, "x_studio_state": "sold"}],
        )

    def test_relist_resets_sold_to_available(self):
        models = MagicMock()

        def execute_kw(db, uid, key, model, method, args, kwargs=None):
            if model == "product.template" and method == "search_read":
                return [
                    {
                        "id": 2,
                        "name": "Old Sold",
                        "default_code": "obj999",
                        "list_price": 100,
                        "qty_available": 0,
                        "categ_id": [8, "vehiculos"],
                        "active": False,
                    }
                ]
            if model == "product.template" and method == "write":
                return True
            if model == "product.product" and method == "search":
                return []
            raise AssertionError(f"unexpected {model}.{method} {args}")

        models.execute_kw.side_effect = execute_kw
        client = OdooCRMClient(
            url="https://odoo.example",
            db="autosell",
            username="api",
            api_key="secret",
            common=MagicMock(),
            models=models,
        )
        client.uid = 7
        result = client.upsert_vehicle_product(
            name="Back On Site",
            list_price=150000,
            default_code="obj999",
            categ_id=8,
        )
        self.assertEqual(result["action"], "updated")
        self.assertEqual(result["inventory_status"], "available")
        writes = [
            c
            for c in models.execute_kw.call_args_list
            if c.args[3] == "product.template" and c.args[4] == "write"
        ]
        self.assertGreaterEqual(len(writes), 1)
        # Final status write must set available + active True
        status_write = writes[-1].args[5][1]
        self.assertTrue(status_write.get("active"))
        self.assertIn(
            status_write.get("x_studio_state")
            or status_write.get("state")
            or status_write.get("x_vehicle_state"),
            {"available", "Available", "Disponible", "disponible"},
        )


class TestVehicleInventory(unittest.TestCase):
    def test_formats_vehicle_matches(self):
        models = MagicMock()
        models.execute_kw.return_value = [
            {
                "id": 638,
                "name": "MAZDA CX3 2020",
                "list_price": 289000.0,
                "qty_available": 1.0,
                "categ_id": [8, "vehiculos"],
            }
        ]
        client = OdooCRMClient(
            url="https://odoo.example",
            db="autosell",
            username="api",
            api_key="secret",
            common=MagicMock(),
            models=models,
        )
        client.uid = 7

        vehicles = client.search_vehicle_inventory("Mazda")

        self.assertEqual(
            vehicles,
            [
                {
                    "id": 638,
                    "name": "MAZDA CX3 2020",
                    "list_price": 289000.0,
                    "qty_available": 1.0,
                    "categ_id": 8,
                    "category_name": "vehiculos",
                }
            ],
        )
        domain = models.execute_kw.call_args.args[5][0]
        self.assertIn(("name", "ilike", "Mazda"), domain)
        self.assertIn(("categ_id.name", "ilike", "vehicul"), domain)

    def test_falls_back_to_all_categories(self):
        models = MagicMock()
        models.execute_kw.side_effect = [
            [],
            [
                {
                    "id": 10,
                    "name": "Mazda",
                    "list_price": 1.0,
                    "qty_available": 0.0,
                    "categ_id": False,
                }
            ],
        ]
        client = OdooCRMClient(
            url="https://odoo.example",
            db="autosell",
            username="api",
            api_key="secret",
            common=MagicMock(),
            models=models,
        )
        client.uid = 7

        vehicles = client.search_vehicle_inventory("Mazda")

        self.assertEqual(len(vehicles), 1)
        self.assertIsNone(vehicles[0]["categ_id"])
        self.assertEqual(models.execute_kw.call_count, 2)


class TestRoundRobin(unittest.TestCase):
    def _client(self, members: list[int]) -> tuple[OdooCRMClient, MagicMock]:
        models = MagicMock()
        client = OdooCRMClient(
            url="https://odoo.example",
            db="autosell",
            username="api",
            api_key="secret",
            common=MagicMock(authenticate=MagicMock(return_value=7)),
            models=models,
        )
        client.uid = 7
        state = {"rr": -1}

        def execute_kw(db, uid, key, model, method, args, kwargs=None):
            if model == "crm.team" and method == "read":
                return [{"id": 3, "member_ids": members}]
            if model == "ir.config_parameter" and method == "search_read":
                return [{"value": str(state["rr"])}] if state["rr"] >= 0 else []
            if model == "ir.config_parameter" and method == "search":
                return [9] if state["rr"] >= 0 else []
            if model == "ir.config_parameter" and method == "write":
                state["rr"] = int(args[1]["value"])
                return True
            if model == "ir.config_parameter" and method == "create":
                state["rr"] = int(args[0]["value"])
                return 9
            raise AssertionError(f"unexpected {model}.{method} {args}")

        models.execute_kw.side_effect = execute_kw
        return client, models

    def test_round_robin_cycles(self):
        client, _ = self._client([10, 20, 30])
        self.assertEqual(client.round_robin_assign_advisor(3), 10)
        self.assertEqual(client.round_robin_assign_advisor(3), 20)
        self.assertEqual(client.round_robin_assign_advisor(3), 30)
        self.assertEqual(client.round_robin_assign_advisor(3), 10)

    def test_empty_team(self):
        client, _ = self._client([])
        with self.assertRaises(OdooCRMError):
            client.round_robin_assign_advisor(3)


class TestChatter(unittest.TestCase):
    def test_post_quote_to_chatter(self):
        models = MagicMock()
        client = OdooCRMClient(
            url="https://odoo.example",
            db="autosell",
            username="api",
            api_key="secret",
            common=MagicMock(authenticate=MagicMock(return_value=7)),
            models=models,
        )
        client.uid = 7

        def execute_kw(db, uid, key, model, method, args, kwargs=None):
            if model == "ir.model.data" and method == "check_object_reference":
                return ["mail.message.subtype", 2]
            if model == "mail.message" and method == "create":
                self.assertEqual(args[0]["model"], "crm.lead")
                self.assertEqual(args[0]["res_id"], 55)
                self.assertIn("Mensualidad", args[0]["body"])
                self.assertEqual(args[0].get("subtype_id"), 2)
                self.assertNotIn("subtype_xmlid", args[0])
                return 9001
            raise AssertionError(f"unexpected {model}.{method}")

        models.execute_kw.side_effect = execute_kw
        msg_id = client.post_quote_to_chatter(
            55, "Cotización CX-5\nMensualidad: $10,278.85"
        )
        self.assertEqual(msg_id, 9001)

    def test_empty_quote_rejected(self):
        client = OdooCRMClient(
            url="https://odoo.example",
            db="autosell",
            username="api",
            api_key="secret",
            common=MagicMock(authenticate=MagicMock(return_value=7)),
            models=MagicMock(),
        )
        client.uid = 7
        with self.assertRaises(OdooCRMError):
            client.post_quote_to_chatter(1, "  ")


class TestAttachFile(unittest.TestCase):
    def test_creates_ir_attachment(self):
        models = MagicMock()

        def execute_kw(db, uid, key, model, method, args, kwargs=None):
            if model == "ir.attachment" and method == "create":
                vals = args[0]
                self.assertEqual(vals["res_model"], "crm.lead")
                self.assertEqual(vals["res_id"], 501)
                self.assertEqual(vals["name"], "quote.pdf")
                self.assertEqual(vals["mimetype"], "application/pdf")
                self.assertTrue(vals["datas"])
                return 44
            raise AssertionError(f"unexpected {model}.{method}")

        models.execute_kw.side_effect = execute_kw
        client = OdooCRMClient(
            url="https://odoo.example",
            db="autosell",
            username="api",
            api_key="secret",
            common=MagicMock(),
            models=models,
        )
        client.uid = 7
        att_id = client.attach_file(
            model="crm.lead",
            res_id=501,
            filename="quote.pdf",
            content=b"%PDF-1.4 test",
        )
        self.assertEqual(att_id, 44)


if __name__ == "__main__":
    unittest.main()
