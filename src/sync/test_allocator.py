from __future__ import annotations

import unittest

from src.models import Vehicle
from src.sync.allocator import allocate_slots
from src.sync.engine import plan_sync_actions


def _v(i: str) -> Vehicle:
    return Vehicle(
        autosell_id=i,
        slug=i,
        title=i,
        brand="Ford",
        year="2020",
        price="100000",
        mileage="1 km",
        version="",
        url=f"https://www.autosell.mx/{i}",
        image_urls=["https://example.com/a.jpg"],
    )


def _live(aid: str, acct: str, posted: str = "2026-01-01T00:00:00+00:00") -> dict:
    return {
        "autosell_id": aid,
        "account_id": acct,
        "fb_listing_url": f"https://www.facebook.com/marketplace/item/{aid}/",
        "status": "live",
        "content_hash": "x",
        "posted_at": posted,
    }


class TestAllocateSlots(unittest.TestCase):
    def test_empty_live_fills_round_robin_up_to_cap(self):
        vehicles = [_v(f"obj{i}") for i in range(10)]
        alloc = allocate_slots(
            vehicles, ["account_1", "account_2"], [], max_per_account=3
        )
        self.assertEqual(len(alloc.by_account["account_1"]), 3)
        self.assertEqual(len(alloc.by_account["account_2"]), 3)
        self.assertEqual(len(alloc.waitlist), 4)
        self.assertEqual(len(alloc.creates), 6)

    def test_sold_frees_slot(self):
        vehicles = [_v("a"), _v("b"), _v("c")]
        live = [_live("a", "account_1")]
        alloc = allocate_slots(
            vehicles, ["account_1"], live, max_per_account=2
        )
        self.assertEqual(alloc.by_account["account_1"], ["a", "b"])
        self.assertEqual(alloc.creates, [("b", "account_1")])
        self.assertEqual(alloc.waitlist, ["c"])

    def test_duplicate_live_keeps_oldest_account(self):
        vehicles = [_v("obj1")]
        live = [
            _live("obj1", "account_2", "2026-06-01T00:00:00+00:00"),
            _live("obj1", "account_1", "2026-01-01T00:00:00+00:00"),
        ]
        alloc = allocate_slots(
            vehicles, ["account_1", "account_2"], live, max_per_account=15
        )
        self.assertEqual(alloc.by_account["account_1"], ["obj1"])
        self.assertEqual(alloc.by_account["account_2"], [])
        self.assertEqual(alloc.overflow, [("obj1", "account_2")])

    def test_over_capacity_keeps_oldest_posted(self):
        vehicles = [_v("x"), _v("y"), _v("z")]
        live = [
            _live("x", "account_1", "2026-01-01T00:00:00+00:00"),
            _live("y", "account_1", "2026-02-01T00:00:00+00:00"),
            _live("z", "account_1", "2026-03-01T00:00:00+00:00"),
        ]
        alloc = allocate_slots(
            vehicles, ["account_1"], live, max_per_account=2
        )
        self.assertEqual(alloc.by_account["account_1"], ["x", "y"])
        self.assertEqual(alloc.overflow, [("z", "account_1")])

    def test_sync_creates_only_assigned_slots(self):
        vehicles = [_v(f"obj{i}") for i in range(5)]
        alloc = allocate_slots(
            vehicles, ["account_1"], [], max_per_account=2
        )
        actions = plan_sync_actions(
            vehicles,
            ["account_1"],
            [],
            max_creates_per_account=10,
            allocation=alloc,
        )
        creates = [a for a in actions if a.action == "create"]
        self.assertEqual(len(creates), 2)
        self.assertEqual({a.autosell_id for a in creates}, set(alloc.by_account["account_1"]))


if __name__ == "__main__":
    unittest.main()
