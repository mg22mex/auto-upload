"""Unit tests — WhatsApp instance → branch routing."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from src.whatsapp_worker.routing import (
    apply_whatsapp_branch_context,
    branch_context_for_instance,
    branch_key_for_instance,
    resolve_instance_for_branch,
)


class TestWhatsAppRouting(unittest.TestCase):
    def test_periferico_instance(self):
        with patch.dict(
            "os.environ",
            {
                "WHATSAPP_INSTANCE_PERIFERICO": "autosell_periferico",
                "WHATSAPP_INSTANCE_SAN_FELIPE": "autosell_san_felipe",
                "ODOO_TEAM_PERIFERICO": "10",
                "ODOO_TEAM_SAN_FELIPE": "20",
            },
            clear=False,
        ):
            self.assertEqual(branch_key_for_instance("autosell_periferico"), "periferico")
            ctx = branch_context_for_instance("autosell_periferico")
            self.assertEqual(ctx["branch"], "periferico")
            self.assertEqual(ctx["physical_location"], "Periférico")
            self.assertEqual(ctx["branch_id"], 10)
            self.assertEqual(ctx["whatsapp_instance"], "autosell_periferico")

    def test_san_felipe_instance(self):
        with patch.dict(
            "os.environ",
            {
                "WHATSAPP_INSTANCE_PERIFERICO": "autosell_periferico",
                "WHATSAPP_INSTANCE_SAN_FELIPE": "autosell_san_felipe",
                "ODOO_TEAM_PERIFERICO": "10",
                "ODOO_TEAM_SAN_FELIPE": "20",
            },
            clear=False,
        ):
            self.assertEqual(branch_key_for_instance("autosell_san_felipe"), "san_felipe")
            ctx = branch_context_for_instance("autosell_san_felipe")
            self.assertEqual(ctx["branch"], "san_felipe")
            self.assertEqual(ctx["physical_location"], "San Felipe")
            self.assertEqual(ctx["branch_id"], 20)

    def test_apply_merges_into_lead_data(self):
        with patch.dict(
            "os.environ",
            {
                "WHATSAPP_INSTANCE_SAN_FELIPE": "autosell_san_felipe",
                "ODOO_TEAM_SAN_FELIPE": "20",
            },
            clear=False,
        ):
            lead = {"name": "Ana", "phone": "6141234567", "vehicle_name": "Vento"}
            apply_whatsapp_branch_context(lead, "autosell_san_felipe")
            self.assertEqual(lead["branch"], "san_felipe")
            self.assertEqual(lead["physical_location"], "San Felipe")
            self.assertEqual(lead["branch_id"], 20)

    def test_apply_overrides_existing_branch_id(self):
        with patch.dict(
            "os.environ",
            {
                "WHATSAPP_INSTANCE_SAN_FELIPE": "autosell_san_felipe",
                "ODOO_TEAM_SAN_FELIPE": "20",
            },
            clear=False,
        ):
            lead = {"branch_id": 1, "branch": "periferico"}
            apply_whatsapp_branch_context(lead, "autosell_san_felipe")
            self.assertEqual(lead["branch_id"], 20)
            self.assertEqual(lead["branch"], "san_felipe")

    def test_resolve_instance_for_branch(self):
        with patch.dict(
            "os.environ",
            {
                "WHATSAPP_INSTANCE_PERIFERICO": "autosell_periferico",
                "WHATSAPP_INSTANCE_SAN_FELIPE": "autosell_san_felipe",
            },
            clear=False,
        ):
            self.assertEqual(resolve_instance_for_branch("periferico"), "autosell_periferico")
            self.assertEqual(resolve_instance_for_branch("san_felipe"), "autosell_san_felipe")


if __name__ == "__main__":
    unittest.main()
