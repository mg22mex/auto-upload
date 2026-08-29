"""Unit tests — catalog branch-tag summary (* Periférico, + San Felipe, - consignment)."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.inventory.snapshot import (
    branch_tag,
    format_catalog_summary,
    load_catalog_snapshot,
    save_catalog_snapshot,
    summarize_catalog,
)
from src.models import Vehicle


def _veh(autosell_id: str, title: str) -> Vehicle:
    return Vehicle(
        autosell_id=autosell_id,
        slug=autosell_id,
        title=title,
        brand="Mazda",
        year="2021",
        price="100000",
        mileage="10000 km",
        version="",
        url=f"https://www.autosell.mx/{autosell_id}",
        image_urls=[],
    )


class TestCatalogSummary(unittest.TestCase):
    def test_branch_tags_counted_per_branch(self):
        vehicles = [
            _veh("obj1", "Cx 30 IGT *"),
            _veh("obj2", "CX 9 IGT +"),
            _veh("obj3", "Escalade -"),
            _veh("obj4", "Macan S"),
        ]

        summary = summarize_catalog(vehicles)

        self.assertEqual(summary["count"], 4)
        self.assertEqual(summary["tags"], {"*": 1, "+": 1, "-": 1})
        self.assertEqual(summary["untagged"], 1)
        self.assertIn("periferico=1", format_catalog_summary(vehicles))

    def test_untagged_catalog_reports_zero_tags(self):
        summary = summarize_catalog([_veh("obj1", "Macan S")])

        self.assertEqual(summary["tags"], {"*": 0, "+": 0, "-": 0})
        self.assertEqual(summary["untagged"], 1)

    def test_branch_tag_survives_snapshot_roundtrip(self):
        vehicles = [_veh("obj1", "Cx 30 IGT *")]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            save_catalog_snapshot(vehicles, path)
            restored = load_catalog_snapshot(path)

        self.assertEqual(restored[0].marketplace_title, "2021 Mazda Cx 30 IGT *")
        self.assertEqual(branch_tag(restored[0]), "*")


if __name__ == "__main__":
    unittest.main()
