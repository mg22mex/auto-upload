"""Unit tests — Marketplace listing age parse + purge candidate selection."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src.facebook.purge_old import (
    ShelfListing,
    parse_listing_age_days,
    purge_old_listings_on_page,
)


class TestParseListingAgeDays(unittest.TestCase):
    def test_spanish_days(self):
        self.assertEqual(parse_listing_age_days("Publicado hace 4 días"), 4.0)
        self.assertEqual(parse_listing_age_days("publicado hace 3 dias"), 3.0)

    def test_english_days(self):
        self.assertEqual(parse_listing_age_days("Listed 5 days ago"), 5.0)

    def test_hours_and_yesterday(self):
        self.assertAlmostEqual(parse_listing_age_days("Publicado hace 12 horas"), 0.5)
        self.assertEqual(parse_listing_age_days("Listed yesterday"), 1.0)
        self.assertEqual(parse_listing_age_days("Publicado hoy"), 0.0)

    def test_weeks(self):
        self.assertEqual(parse_listing_age_days("publicado hace 2 semanas"), 14.0)

    def test_absolute_spanish_date(self):
        now = datetime(2026, 8, 23, tzinfo=timezone.utc)
        age = parse_listing_age_days("Publicado el 20 de agosto", now=now)
        self.assertEqual(age, 3.0)

    def test_unknown(self):
        self.assertIsNone(parse_listing_age_days("Mercedes-Benz G63 $100000"))


class TestPurgeOldListingsOnPage(unittest.TestCase):
    def test_skips_young_and_deletes_old(self):
        page = MagicMock()
        store = MagicMock()
        store.get_live_listings.return_value = [
            {
                "autosell_id": "obj_old",
                "account_id": "account_1",
                "fb_listing_url": "https://www.facebook.com/marketplace/item/111/",
                "posted_at": "2026-01-01T00:00:00+00:00",
            }
        ]
        cards = [
            ShelfListing("111", "/marketplace/item/111/", 5.0, "hace 5 días", "x"),
            ShelfListing("222", "/marketplace/item/222/", 1.0, "hace 1 día", "y"),
            ShelfListing("333", "/marketplace/item/333/", None, "", "z"),
        ]
        with (
            patch(
                "src.facebook.purge_old.collect_selling_listings",
                return_value=cards,
            ),
            patch(
                "src.facebook.purge_old._remove_from_selling_shelf",
                return_value=True,
            ) as remove,
            patch("src.facebook.purge_old.random_delay"),
        ):
            stats = purge_old_listings_on_page(
                page,
                store,
                account_id="account_1",
                max_days=3,
                dry_run=False,
            )
        self.assertEqual(stats.found, 3)
        self.assertEqual(stats.skipped_young, 1)
        self.assertEqual(stats.skipped_unknown_age, 1)
        self.assertEqual(stats.deleted, 1)
        remove.assert_called_once()
        store.mark_fb_listing_removed.assert_called_once_with(
            "obj_old", "account_1", clear_url=True
        )

    def test_dry_run_does_not_delete(self):
        page = MagicMock()
        store = MagicMock()
        store.get_live_listings.return_value = []
        cards = [
            ShelfListing("111", "/marketplace/item/111/", 9.0, "hace 9 días", "x"),
        ]
        with (
            patch(
                "src.facebook.purge_old.collect_selling_listings",
                return_value=cards,
            ),
            patch("src.facebook.purge_old._remove_from_selling_shelf") as remove,
        ):
            stats = purge_old_listings_on_page(
                page,
                store,
                account_id="account_1",
                max_days=3,
                dry_run=True,
            )
        self.assertEqual(stats.dry_run_candidates, 1)
        self.assertEqual(stats.deleted, 0)
        remove.assert_not_called()
        store.mark_fb_listing_removed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
