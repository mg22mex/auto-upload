"""Unit tests — brand-fair catalog / relist ordering."""
from __future__ import annotations

import unittest

from src.models import Vehicle
from src.sync.fair_queue import (
    brand_fair_round_robin,
    brand_key,
    newest_catalog_key,
    order_vehicles_newest_first,
)
from src.sync.repost import plan_repost_actions


def _v(aid: str, brand: str, year: str = "2020") -> Vehicle:
    return Vehicle(
        autosell_id=aid,
        slug=f"{brand.lower()}-{aid}",
        title=aid,
        brand=brand,
        year=year,
        price="100000",
        mileage="10000 km",
        version="",
        url=f"https://www.autosell.mx/catalogo/{aid}",
        image_urls=[],
    )


class TestNewestFirst(unittest.TestCase):
    def test_year_then_obj_id(self):
        vehicles = [
            _v("obj100", "Audi", "2018"),
            _v("obj500", "Chevrolet", "2024"),
            _v("obj400", "Ford", "2024"),
            _v("obj50", "GMC", "2022"),
        ]
        ordered = order_vehicles_newest_first(vehicles)
        self.assertEqual(
            [v.autosell_id for v in ordered],
            ["obj500", "obj400", "obj50", "obj100"],
        )


class TestBrandFair(unittest.TestCase):
    def test_interleaves_brands_under_same_age(self):
        # Three Audis + Chevy + Ford all same posted_at → brand RR, not AAA.
        items = [
            ("obj_a1", "2026-01-01"),
            ("obj_a2", "2026-01-01"),
            ("obj_a3", "2026-01-01"),
            ("obj_c1", "2026-01-01"),
            ("obj_f1", "2026-01-01"),
        ]
        brands = {
            "obj_a1": "Audi",
            "obj_a2": "Audi",
            "obj_a3": "Audi",
            "obj_c1": "Chevrolet",
            "obj_f1": "Ford",
        }
        ordered = brand_fair_round_robin(
            items,
            brand_of=lambda item: brands[item[0]].casefold(),
            primary_key=lambda item: (item[1], item[0]),
        )
        brands_seq = [brands[aid] for aid, _ in ordered[:3]]
        self.assertEqual(len(set(brands_seq)), 3, brands_seq)


class TestPlanRepostBrandFair(unittest.TestCase):
    def test_cap_is_not_all_audi(self):
        vehicles = [
            _v("obj_a1", "Audi"),
            _v("obj_a2", "Audi"),
            _v("obj_a3", "Audi"),
            _v("obj_c1", "Chevrolet"),
            _v("obj_f1", "Ford"),
            _v("obj_g1", "GMC"),
        ]
        live = [
            {
                "autosell_id": v.autosell_id,
                "account_id": "account_1",
                "status": "live",
                "fb_listing_url": f"https://facebook.com/item/{v.autosell_id}",
                "posted_at": "2026-01-01T00:00:00+00:00",
            }
            for v in vehicles
        ]
        actions, _ = plan_repost_actions(
            vehicles,
            ["account_1"],
            live,
            all_eligible=True,
            older_than_days=0,
            max_per_account=3,
            is_on_hold=lambda *_a: False,
        )
        brands = [a.vehicle.brand for a in actions if a.vehicle]
        self.assertEqual(len(brands), 3)
        self.assertGreaterEqual(len(set(brands)), 3, brands)


if __name__ == "__main__":
    unittest.main()
