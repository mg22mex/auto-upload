"""Unit tests — CRMLeadManager create/update, branch teams, fleet, dry-run."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.odoo_sync.base import OdooCRMError
from src.odoo_sync.client import OdooCRMClient
from src.odoo_sync.crm import (
    CRMLeadManager,
    PRIMARY_BRANCH,
    apply_vehicle_location_team,
    infer_physical_location,
    normalize_phone_digits,
    parse_team_id,
    resolve_team_id,
)
from src.odoo_sync.fleet import FleetLinkResult, FleetVehicle


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


class TestBranchTeams(unittest.TestCase):
    def test_parse_team_placeholder(self):
        self.assertIsNone(parse_team_id(None))
        self.assertIsNone(parse_team_id(""))
        self.assertIsNone(parse_team_id("0"))
        self.assertIsNone(parse_team_id("None"))
        self.assertEqual(parse_team_id("12"), 12)

    def test_san_felipe_falls_back_to_periferico(self):
        teams = {PRIMARY_BRANCH: 5, "san_felipe": None}
        branch, team_id, fell_back = resolve_team_id("san_felipe", teams=teams)
        self.assertEqual(branch, PRIMARY_BRANCH)
        self.assertEqual(team_id, 5)
        self.assertTrue(fell_back)

    def test_missing_all_teams_soft(self):
        branch, team_id, fell_back = resolve_team_id(
            "san_felipe", teams={PRIMARY_BRANCH: None, "san_felipe": None}
        )
        self.assertEqual(branch, PRIMARY_BRANCH)
        self.assertIsNone(team_id)
        self.assertTrue(fell_back)

    def test_phone_normalize(self):
        self.assertEqual(normalize_phone_digits("+52 (614) 123-4567"), "526141234567")

    def test_infer_physical_location(self):
        self.assertEqual(infer_physical_location("Lot San Felipe Norte"), "san_felipe")
        self.assertEqual(infer_physical_location("Sucursal Periférico"), "periferico")
        self.assertIsNone(infer_physical_location("Almacén central"))

    def test_apply_location_overrides_to_san_felipe_team(self):
        teams = {"periferico": 5, "san_felipe": 17}
        branch, team_id, fell_back, overrode = apply_vehicle_location_team(
            "periferico", "san_felipe", teams=teams
        )
        self.assertEqual(branch, "san_felipe")
        self.assertEqual(team_id, 17)
        self.assertFalse(fell_back)
        self.assertTrue(overrode)


class TestCRMLeadManagerDryRun(unittest.TestCase):
    def test_dry_run_no_rpc(self):
        models = MagicMock()
        client = _client(models, dry_run=True)
        mgr = CRMLeadManager(client=client)
        result = mgr.create_or_update_lead(
            {
                "client_name": "Ana Pérez",
                "phone": "+52 614 123 4567",
                "vehicle_info": "Mazda CX-5 2020",
                "email_from": "ana@example.com",
            },
            branch="periferico",
        )
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["lead_id"], -1)
        self.assertFalse(result["deduplicated"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["branch"], PRIMARY_BRANCH)
        self.assertIn("Consulta: Mazda CX-5 2020", result["title"])
        models.execute_kw.assert_not_called()

    def test_dry_run_san_felipe_fallback(self):
        models = MagicMock()
        client = _client(models, dry_run=True)
        mgr = CRMLeadManager(client=client)
        with patch.dict(
            "os.environ",
            {"ODOO_TEAM_PERIFERICO": "5", "ODOO_TEAM_SAN_FELIPE": "0"},
            clear=False,
        ):
            result = mgr.create_or_update_lead(
                {
                    "name": "Luis",
                    "phone": "6149998877",
                    "vehicle_name": "Ranger",
                    "vin": "1FTER4EH0PLA12345",
                },
                branch="san_felipe",
            )
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["branch"], PRIMARY_BRANCH)
        self.assertEqual(result["team_id"], 5)
        self.assertTrue(result["fell_back"])
        self.assertIsNotNone(result["fleet"])
        self.assertEqual(result["fleet"]["status"], "dry_run")
        models.execute_kw.assert_not_called()

    def test_missing_phone_raises(self):
        mgr = CRMLeadManager(client=_client(dry_run=True))
        with self.assertRaises(OdooCRMError):
            mgr.create_or_update_lead({"client_name": "X"})


class TestCRMLeadManagerLiveMocked(unittest.TestCase):
    @staticmethod
    def _crm_rpc(*, create_id: int = 801, existing: list | None = None):
        created: dict = {}
        writes: list = []
        utm_ids = {"utm.medium": {}, "utm.source": {}}
        next_utm = {"n": 40}

        def execute_kw(db, uid, key, model, method, args, kwargs=None):
            if model == "crm.lead" and method == "search":
                return list(existing or [])
            if model == "crm.lead" and method == "create":
                created["vals"] = args[0]
                return create_id
            if model == "crm.lead" and method == "write":
                writes.append(args)
                return True
            if model == "crm.tag" and method == "search_read":
                name = args[0][0][2] if args and args[0] else ""
                return [{"id": 77, "name": name}]
            if model == "crm.tag" and method == "create":
                return 77
            if model in {"utm.medium", "utm.source"} and method == "search_read":
                name = args[0][0][2] if args and args[0] else ""
                store = utm_ids[model]
                if name not in store:
                    next_utm["n"] += 1
                    store[name] = next_utm["n"]
                return [{"id": store[name], "name": name}]
            if model in {"utm.medium", "utm.source"} and method == "create":
                name = args[0].get("name", "")
                next_utm["n"] += 1
                utm_ids[model][name] = next_utm["n"]
                return next_utm["n"]
            if model == "mail.message" and method == "create":
                return 99
            if model == "ir.model.data":
                return ["mail.message.subtype", 1]
            if model == "mail.message.subtype":
                return [1]
            raise AssertionError(f"unexpected {model}.{method}")

        return execute_kw, created, writes, utm_ids

    def test_creates_when_no_existing(self):
        execute_kw, created, _writes, utm_ids = self._crm_rpc()
        models = MagicMock()
        models.execute_kw.side_effect = execute_kw
        client = _client(models)
        mgr = CRMLeadManager(client=client)
        with patch.dict(
            "os.environ",
            {"ODOO_TEAM_PERIFERICO": "5"},
            clear=False,
        ):
            result = mgr.create_or_update_lead(
                {
                    "client_name": "Ana Pérez",
                    "phone": "6141234567",
                    "email_from": "ana@example.com",
                    "vehicle_info": "CX-5",
                    "channel": "Voice / Phone",
                    "description": "Interesada en financiamiento",
                },
                branch="periferico",
            )

        self.assertEqual(result["status"], "created")
        self.assertEqual(result["lead_id"], 801)
        self.assertFalse(result["deduplicated"])
        self.assertEqual(result["team_id"], 5)
        vals = created["vals"]
        self.assertEqual(vals["name"], "Consulta: CX-5 - Ana Pérez")
        self.assertEqual(vals["partner_name"], "Ana Pérez")
        self.assertEqual(vals["contact_name"], "Ana Pérez")
        self.assertEqual(vals["phone"], "6141234567")
        self.assertEqual(vals["email_from"], "ana@example.com")
        self.assertEqual(vals["team_id"], 5)
        self.assertIn("medium_id", vals)
        self.assertIn("source_id", vals)
        self.assertEqual(vals["medium_id"], utm_ids["utm.medium"]["Phone"])
        self.assertEqual(vals["source_id"], utm_ids["utm.source"]["Inbound Call"])
        self.assertEqual(vals["tag_ids"][0][2], [77])
        self.assertIn(77, result["tag_ids"])
        self.assertIn("Vehicle interest: CX-5", vals["description"])
        tag_names = [
            c.args[5][0][0][2]
            for c in models.execute_kw.call_args_list
            if c.args[3] == "crm.tag" and c.args[4] == "search_read"
        ]
        self.assertIn("MG Quote Lead", tag_names)

    def test_whatsapp_attribution(self):
        execute_kw, created, _writes, utm_ids = self._crm_rpc(create_id=902)
        models = MagicMock()
        models.execute_kw.side_effect = execute_kw
        mgr = CRMLeadManager(client=_client(models))
        with patch.dict(
            "os.environ",
            {"ODOO_TEAM_SAN_FELIPE": "5", "ODOO_TEAM_PERIFERICO": "1"},
            clear=False,
        ):
            result = mgr.create_or_update_lead(
                {
                    "client_name": "Luis",
                    "phone": "6149998888",
                    "vehicle_info": "Hilux",
                    "channel": "WhatsApp",
                },
                branch="san_felipe",
            )
        vals = created["vals"]
        self.assertEqual(result["team_id"], 5)
        self.assertEqual(vals["team_id"], 5)
        self.assertEqual(vals["medium_id"], utm_ids["utm.medium"]["WhatsApp"])
        self.assertEqual(
            vals["source_id"], utm_ids["utm.source"]["Facebook Marketplace"]
        )

    def test_web_attribution(self):
        execute_kw, created, _writes, utm_ids = self._crm_rpc(create_id=903)
        models = MagicMock()
        models.execute_kw.side_effect = execute_kw
        mgr = CRMLeadManager(client=_client(models))
        with patch.dict(
            "os.environ",
            {"ODOO_TEAM_PERIFERICO": "1"},
            clear=False,
        ):
            mgr.create_or_update_lead(
                {
                    "client_name": "Web Lead",
                    "phone": "6140001111",
                    "vehicle_info": "Sentra",
                    "channel": "Website",
                },
                branch="periferico",
            )
        vals = created["vals"]
        self.assertEqual(vals["medium_id"], utm_ids["utm.medium"]["Website"])
        self.assertEqual(vals["source_id"], utm_ids["utm.source"]["Autosell Web"])

    def test_dedupe_posts_chatter_no_create(self):
        execute_kw, _created, writes, _utm = self._crm_rpc(existing=[4242])
        models = MagicMock()
        messages: list = []

        def wrapped(db, uid, key, model, method, args, kwargs=None):
            if model == "mail.message" and method == "create":
                messages.append(args[0])
                return 99
            return execute_kw(db, uid, key, model, method, args, kwargs)

        models.execute_kw.side_effect = wrapped
        client = _client(models)
        mgr = CRMLeadManager(client=client)
        result = mgr.create_or_update_lead(
            {
                "client_name": "Ana",
                "phone": "6141234567",
                "vehicle_info": "Q5",
                "notes": "Segunda llamada",
                "channel": "Voice / Phone",
            }
        )
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["lead_id"], 4242)
        self.assertTrue(result["deduplicated"])
        self.assertEqual(len(messages), 1)
        self.assertIn("deduplicated", messages[0]["body"].lower())
        self.assertTrue(writes)
        write_vals = writes[0][1]
        self.assertIn("medium_id", write_vals)
        self.assertIn("source_id", write_vals)
        self.assertIn("tag_ids", write_vals)
        # No create on crm.lead
        create_calls = [
            c
            for c in models.execute_kw.call_args_list
            if c.args[3] == "crm.lead" and c.args[4] == "create"
        ]
        self.assertEqual(create_calls, [])

    def test_fleet_linked_on_create(self):
        models = MagicMock()

        def execute_kw(db, uid, key, model, method, args, kwargs=None):
            if model == "crm.lead" and method == "search":
                return []
            if model == "crm.lead" and method == "create":
                return 50
            if model == "crm.lead" and method == "write":
                return True
            if model == "fleet.vehicle" and method == "search_read":
                return []
            if model == "crm.tag" and method in {"search_read", "create"}:
                return [{"id": 77, "name": "MG Quote Lead"}] if method == "search_read" else 77
            if model in {"utm.medium", "utm.source"} and method in {
                "search_read",
                "create",
            }:
                return [{"id": 1, "name": "x"}] if method == "search_read" else 1
            raise AssertionError(f"unexpected {model}.{method}")

        models.execute_kw.side_effect = execute_kw
        client = _client(models)

        def fake_link(lead_id, **kwargs):
            return FleetLinkResult(
                ok=True,
                lead_id=int(lead_id),
                vehicle_id=9,
                vin=kwargs.get("vin") or "",
                linked_via="x_vin",
            )

        client.link_fleet_vehicle_to_lead = fake_link  # type: ignore[method-assign]
        mgr = CRMLeadManager(client=client)
        result = mgr.create_or_update_lead(
            {
                "client_name": "Luis",
                "phone": "6140001111",
                "vehicle_info": "CX-5",
                "vin": "JM3KFBCM5L0123456",
            }
        )
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["lead_id"], 50)
        self.assertIsNotNone(result["fleet"])
        self.assertEqual(result["fleet"]["status"], "ok")
        self.assertEqual(result["fleet"]["linked_via"], "x_vin")
        self.assertEqual(result["fleet"]["vin"], "JM3KFBCM5L0123456")

    def test_san_felipe_vin_overrides_periferico_inbound_branch(self):
        """San Felipe unit + inbound branch=periferico → team ODOO_TEAM_SAN_FELIPE."""
        models = MagicMock()
        created: dict = {}
        sf_vin = "3VWFE21C04M000111"

        def execute_kw(db, uid, key, model, method, args, kwargs=None):
            if model == "crm.lead" and method == "search":
                return []
            if model == "crm.lead" and method == "create":
                created["vals"] = args[0]
                return 902
            if model == "fleet.vehicle" and method == "search_read":
                return [
                    {
                        "id": 44,
                        "name": "CX-5 San Felipe",
                        "vin_sn": sf_vin,
                        "license_plate": "SF-001",
                        "model_id": [3, "CX-5"],
                        "driver_id": False,
                        "x_studio_ubicacion": "San Felipe",
                        "location": "San Felipe",
                    }
                ]
            if model == "crm.lead" and method == "write":
                return True
            if model == "crm.tag" and method in {"search_read", "create"}:
                return [{"id": 77, "name": "MG Quote Lead"}] if method == "search_read" else 77
            if model in {"utm.medium", "utm.source"} and method in {
                "search_read",
                "create",
            }:
                return [{"id": 1, "name": "x"}] if method == "search_read" else 1
            raise AssertionError(f"unexpected {model}.{method} {args}")

        models.execute_kw.side_effect = execute_kw
        client = _client(models)

        def fake_link(lead_id, **kwargs):
            return FleetLinkResult(
                ok=True,
                lead_id=int(lead_id),
                vehicle_id=44,
                vin=kwargs.get("vin") or sf_vin,
                linked_via="x_vin",
            )

        client.link_fleet_vehicle_to_lead = fake_link  # type: ignore[method-assign]
        mgr = CRMLeadManager(client=client)
        with patch.dict(
            "os.environ",
            {
                "ODOO_TEAM_PERIFERICO": "5",
                "ODOO_TEAM_SAN_FELIPE": "17",
            },
            clear=False,
        ):
            result = mgr.create_or_update_lead(
                {
                    "client_name": "Rosa",
                    "phone": "6147778899",
                    "vehicle_info": "CX-5",
                    "vin": sf_vin,
                },
                branch="periferico",
            )

        self.assertEqual(result["status"], "created")
        self.assertEqual(result["lead_id"], 902)
        self.assertEqual(result["team_id"], 17)
        self.assertEqual(result["branch"], "san_felipe")
        self.assertTrue(result["location_overrode"])
        self.assertEqual(result["physical_location"], "san_felipe")
        vals = created["vals"]
        self.assertEqual(vals["team_id"], 17)
        self.assertIn(
            "Ubicación Física del Vehículo: san_felipe",
            vals["description"],
        )


if __name__ == "__main__":
    unittest.main()
