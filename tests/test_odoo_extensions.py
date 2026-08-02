"""Unit tests — WhatsApp / Fleet / Documents Odoo extensions (mocked XML-RPC)."""
from __future__ import annotations

import base64
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.odoo_sync.client import OdooCRMClient
from src.odoo_sync.fleet import FleetVehicle
from src.odoo_sync.whatsapp import STANDARD_WHATSAPP_TEMPLATES, WhatsAppSendResult


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


class TestWhatsAppTemplates(unittest.TestCase):
    def test_standard_template_aliases(self):
        self.assertIn("sale_order", STANDARD_WHATSAPP_TEMPLATES)
        self.assertIn("Sale Order", STANDARD_WHATSAPP_TEMPLATES["sale_order"])

    def test_send_whatsapp_template_payload(self):
        models = MagicMock()
        created: dict = {}

        def execute_kw(db, uid, key, model, method, args, kwargs=None):
            if model == "whatsapp.template" and method == "search_read":
                return [{"id": 11, "name": "Sale Order"}]
            if model == "crm.lead" and method == "read":
                return [
                    {
                        "partner_id": [55, "Ana"],
                        "phone": "6141234567",
                        "mobile": False,
                    }
                ]
            if model == "whatsapp.composer" and method == "create":
                created["composer"] = args[0]
                return 200
            if model == "whatsapp.composer" and method.startswith("action_"):
                created["sent"] = method
                return True
            raise AssertionError(f"unexpected {model}.{method}")

        models.execute_kw.side_effect = execute_kw
        client = _client(models)
        result = client.send_whatsapp_template(
            501,
            "Sale Order",
            variables={"order": "S00120", "amount": "399000"},
        )
        self.assertIsInstance(result, WhatsAppSendResult)
        self.assertTrue(result.ok)
        self.assertEqual(result.composer_id, 200)
        self.assertEqual(created["composer"]["wa_template_id"], 11)
        self.assertEqual(created["composer"]["res_model"], "crm.lead")
        self.assertEqual(created["composer"]["partner_ids"], [(6, 0, [55])])
        self.assertIn("order: S00120", created["composer"]["body"])
        self.assertTrue(created.get("sent"))

    def test_missing_template_soft_fails(self):
        models = MagicMock()

        def execute_kw(db, uid, key, model, method, args, kwargs=None):
            if "whatsapp" in model and method == "search_read":
                return []
            raise AssertionError(f"unexpected {model}.{method}")

        models.execute_kw.side_effect = execute_kw
        client = _client(models)
        result = client.send_whatsapp_template(1, "Payment Link")
        self.assertFalse(result.ok)
        self.assertIn("not found", (result.error or "").lower())

    def test_whatsapp_dry_run(self):
        models = MagicMock()
        client = _client(models, dry_run=True)
        result = client.send_whatsapp_template(1, "Invoice")
        self.assertTrue(result.ok)
        self.assertTrue(result.dry_run)
        models.execute_kw.assert_not_called()


class TestFleetMapping(unittest.TestCase):
    def test_find_by_vin(self):
        models = MagicMock()

        def execute_kw(db, uid, key, model, method, args, kwargs=None):
            self.assertEqual(model, "fleet.vehicle")
            domain = args[0]
            self.assertEqual(domain[0][0], "vin_sn")
            return [
                {
                    "id": 9,
                    "name": "CX-5 / ABC123",
                    "vin_sn": "JM3KFBCM5L0123456",
                    "license_plate": "ABC123",
                    "model_id": [3, "CX-5"],
                    "driver_id": False,
                }
            ]

        models.execute_kw.side_effect = execute_kw
        client = _client(models)
        vehicle = client.find_fleet_vehicle_by_vin("JM3KFBCM5L0123456")
        self.assertIsInstance(vehicle, FleetVehicle)
        self.assertEqual(vehicle.id, 9)
        self.assertEqual(vehicle.vin_sn, "JM3KFBCM5L0123456")
        self.assertEqual(vehicle.model_name, "CX-5")

    def test_link_vin_via_x_vin(self):
        models = MagicMock()
        writes: list = []

        def execute_kw(db, uid, key, model, method, args, kwargs=None):
            if model == "fleet.vehicle" and method == "search_read":
                return []
            if model == "crm.lead" and method == "write":
                writes.append(args)
                return True
            raise AssertionError(f"unexpected {model}.{method}")

        models.execute_kw.side_effect = execute_kw
        client = _client(models)
        result = client.link_fleet_vehicle_to_lead(501, vin="JM3KFBCM5L0123456")
        self.assertTrue(result.ok)
        self.assertEqual(result.linked_via, "x_vin")
        self.assertEqual(writes[0][1]["x_vin"], "JM3KFBCM5L0123456")

    def test_link_falls_back_to_description(self):
        models = MagicMock()

        def execute_kw(db, uid, key, model, method, args, kwargs=None):
            if model == "fleet.vehicle":
                return []
            if model == "crm.lead" and method == "write" and "x_vin" in args[1]:
                raise RuntimeError("unknown field x_vin")
            if model == "crm.lead" and method == "read":
                return [{"description": "Vehicle interest: CX-5"}]
            if model == "crm.lead" and method == "write":
                self.assertIn("VIN: JM3TEST", args[1]["description"])
                return True
            if model == "mail.message":
                return 1
            if model == "ir.model.data":
                return ["mail.message.subtype", 1]
            if model == "mail.message.subtype":
                return [1]
            raise AssertionError(f"unexpected {model}.{method} {args}")

        models.execute_kw.side_effect = execute_kw
        client = _client(models)
        result = client.link_fleet_vehicle_to_lead(77, vin="JM3TEST")
        self.assertTrue(result.ok)
        self.assertIn("description", result.linked_via)

    def test_fleet_dry_run(self):
        models = MagicMock()
        client = _client(models, dry_run=True)
        result = client.link_fleet_vehicle_to_lead(1, vin="X")
        self.assertTrue(result.dry_run)
        models.execute_kw.assert_not_called()


class TestDocuments(unittest.TestCase):
    def test_attach_document_bytes_payload(self):
        models = MagicMock()

        def execute_kw(db, uid, key, model, method, args, kwargs=None):
            self.assertEqual(model, "ir.attachment")
            self.assertEqual(method, "create")
            vals = args[0]
            self.assertEqual(vals["res_model"], "crm.lead")
            self.assertEqual(vals["res_id"], 501)
            self.assertEqual(vals["name"], "id_scan.pdf")
            self.assertEqual(vals["mimetype"], "application/pdf")
            raw = base64.b64decode(vals["datas"])
            self.assertEqual(raw, b"%PDF-1.4 test")
            return 44

        models.execute_kw.side_effect = execute_kw
        client = _client(models)
        att_id = client.attach_document_to_lead(
            501, b"%PDF-1.4 test", filename="id_scan.pdf"
        )
        self.assertEqual(att_id, 44)

    def test_attach_document_from_path(self):
        models = MagicMock()
        models.execute_kw.return_value = 99
        client = _client(models)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(b"%PDF-bytes")
            path = tmp.name
        try:
            att_id = client.attach_document_to_lead(12, path)
            self.assertEqual(att_id, 99)
            vals = models.execute_kw.call_args.args[5][0]
            self.assertEqual(vals["res_id"], 12)
            self.assertTrue(vals["name"].endswith(".pdf"))
        finally:
            Path(path).unlink(missing_ok=True)

    def test_documents_dry_run(self):
        models = MagicMock()
        client = _client(models, dry_run=True)
        att_id = client.attach_document_to_lead(1, b"data", filename="a.pdf")
        self.assertEqual(att_id, -1)
        models.execute_kw.assert_not_called()


class TestModularSession(unittest.TestCase):
    def test_client_exposes_all_extension_apis(self):
        client = _client(dry_run=True)
        self.assertTrue(hasattr(client, "send_whatsapp_template"))
        self.assertTrue(hasattr(client, "find_fleet_vehicle_by_vin"))
        self.assertTrue(hasattr(client, "attach_document_to_lead"))
        self.assertTrue(hasattr(client, "create_or_update_lead"))
        self.assertTrue(client.dry_run)


if __name__ == "__main__":
    unittest.main()
