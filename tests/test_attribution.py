"""Unit tests — lead attribution & commission ledger."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.attribution import (
    SALE_IN_PROGRESS,
    SALE_LOST,
    SALE_WON,
    classify_sale_status,
    get_commission,
    list_commissions,
    monthly_summary,
    reconcile_commissions_from_odoo,
    sync_lead_attribution,
    upsert_commission,
    CommissionRecord,
)


class AttributionStageTests(unittest.TestCase):
    def test_won_aliases(self):
        self.assertEqual(classify_sale_status("Ganado"), SALE_WON)
        self.assertEqual(classify_sale_status("closed won"), SALE_WON)
        self.assertEqual(classify_sale_status("Vendido"), SALE_WON)

    def test_lost_and_open(self):
        self.assertEqual(classify_sale_status("Perdido"), SALE_LOST)
        self.assertEqual(classify_sale_status("Beatriz Cita"), SALE_IN_PROGRESS)


class CommissionLedgerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "commissions.db"
        os.environ["COMMISSION_DEFAULT_PERCENTAGE"] = "1.0"
        os.environ["REPS_SAN_FELIPE"] = (
            '[{"phone": "+526142417711", "name": "Francisco"},'
            ' {"odoo_id": 8, "phone": "+526142349504", "name": "Aaron"}]'
        )

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop("COMMISSION_DEFAULT_PERCENTAGE", None)
        os.environ.pop("REPS_SAN_FELIPE", None)

    def test_won_computes_commission(self):
        rec = sync_lead_attribution(
            1001,
            "Ganado",
            {
                "phone": "5216141754852",
                "contact_name": "Marco Gastelum",
                "vehicle_name": "Chevrolet Aveo 2020",
                "vin": "VIN123",
                "expected_revenue": 250000,
                "user_id": [8, "Aaron"],
                "branch": "san_felipe",
                "create_date": "2026-09-15T10:00:00",
            },
            path=self.db,
        )
        self.assertEqual(rec.sale_status, SALE_WON)
        self.assertEqual(rec.assigned_rep, "Aaron")
        self.assertEqual(rec.commission_percentage, 1.0)
        self.assertEqual(rec.commission_amount, 2500.0)
        stored = get_commission(1001, path=self.db)
        assert stored is not None
        self.assertEqual(stored.commission_amount, 2500.0)

    def test_phone_only_rep_francisco(self):
        rec = sync_lead_attribution(
            1002,
            "Beatriz Cita",
            {
                "phone": "6141112222",
                "assigned_rep": "Francisco",
                "branch": "san_felipe",
                "vehicle_name": "Toyota Corolla 2020",
            },
            path=self.db,
        )
        self.assertEqual(rec.sale_status, SALE_IN_PROGRESS)
        self.assertEqual(rec.assigned_rep, "Francisco")
        self.assertIn("6142417711", rec.assigned_rep_phone.replace("+", ""))

    def test_idempotent_upsert(self):
        sync_lead_attribution(
            55,
            "Ganado",
            {"expected_revenue": 100000, "phone": "6140000000"},
            path=self.db,
        )
        sync_lead_attribution(
            55,
            "Ganado",
            {"expected_revenue": 100000, "phone": "6140000000"},
            path=self.db,
        )
        rows = list_commissions(path=self.db)
        self.assertEqual(len(rows), 1)

    def test_monthly_summary(self):
        upsert_commission(
            CommissionRecord(
                lead_id=1,
                sale_status=SALE_WON,
                deal_amount=200000,
                commission_percentage=1.0,
                commission_amount=2000,
                assigned_rep="Aaron",
                closed_at="2026-09-20T12:00:00+00:00",
                first_contact_timestamp="2026-09-01T12:00:00+00:00",
            ),
            path=self.db,
        )
        summary = monthly_summary("2026-09", path=self.db)
        self.assertEqual(summary.won_count, 1)
        self.assertEqual(summary.total_commission, 2000.0)
        self.assertIn("Aaron", summary.by_rep)


class ReconcileMockTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "commissions.db"

    def tearDown(self):
        self._tmp.cleanup()

    def test_reconcile_maps_odoo_rows(self):
        client = MagicMock()
        client.authenticate.return_value = 1
        client.execute_kw.side_effect = [
            [9],  # tag search
            [
                {
                    "id": 1937,
                    "name": "Marco / Aveo",
                    "contact_name": "Marco Gastelum",
                    "phone": "5216141754852",
                    "mobile": False,
                    "stage_id": [20, "Ganado"],
                    "user_id": [8, "Aaron"],
                    "team_id": [5, "San Felipe"],
                    "expected_revenue": 250000.0,
                    "create_date": "2026-09-10 18:00:00",
                    "write_date": "2026-09-27 12:00:00",
                    "date_closed": "2026-09-27 12:00:00",
                    "description": "Vehículo: Chevrolet Aveo 2020",
                    "tag_ids": [9],
                    "medium_id": [1, "Phone"],
                    "source_id": [2, "Inbound Call"],
                }
            ],
        ]
        out = reconcile_commissions_from_odoo(client, path=self.db, limit=50)
        self.assertTrue(out["ok"])
        self.assertEqual(out["synced"], 1)
        self.assertEqual(out["won"], 1)
        row = get_commission(1937, path=self.db)
        assert row is not None
        self.assertEqual(row.sale_status, SALE_WON)
        self.assertEqual(row.commission_amount, 2500.0)


if __name__ == "__main__":
    unittest.main()
