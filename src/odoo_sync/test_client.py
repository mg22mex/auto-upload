"""Unit tests — OdooCRMClient with mocked XML-RPC."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.odoo_sync.client import OdooCRMClient, OdooCRMError


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

        def execute_kw(db, uid, key, model, method, args, kwargs=None):
            if model == "crm.lead" and method == "search":
                return []
            if model == "crm.lead" and method == "create":
                return 1001
            raise AssertionError(f"unexpected {model}.{method}")

        models.execute_kw.side_effect = execute_kw
        lead_id = client.create_or_update_lead(
            "Ana Pérez", "6141234567", "Mazda CX-5 2020", branch_id=3
        )
        self.assertEqual(lead_id, 1001)
        create_call = [
            c
            for c in models.execute_kw.call_args_list
            if c.args[3] == "crm.lead" and c.args[4] == "create"
        ][0]
        vals = create_call.args[5][0]
        self.assertEqual(vals["phone"], "6141234567")
        self.assertEqual(vals["team_id"], 3)
        self.assertIn("Mazda CX-5 2020", vals["description"])

    def test_update_existing_lead(self):
        client, models = self._client()

        def execute_kw(db, uid, key, model, method, args, kwargs=None):
            if model == "crm.lead" and method == "search":
                return [55]
            if model == "crm.lead" and method == "write":
                return True
            raise AssertionError(f"unexpected {model}.{method}")

        models.execute_kw.side_effect = execute_kw
        lead_id = client.create_or_update_lead(
            "Ana Pérez", "6141234567", "Hilux 2023", branch_id=2
        )
        self.assertEqual(lead_id, 55)
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


if __name__ == "__main__":
    unittest.main()
