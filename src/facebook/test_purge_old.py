"""Unit tests — Marketplace listing age parse + purge candidate selection."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src.facebook.purge_old import (
    ShelfListing,
    collect_selling_listings,
    hydrate_selling_shelf_scrolls,
    is_purge_age_eligible,
    parse_listing_age_days,
    purge_old_listings_on_page,
    resolve_listing_age_for_purge,
    wait_for_selling_shelf_cards,
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

    def test_listed_on_slash_us_date(self):
        """FB shelf: 'Listed on 10/24' must drive 7-day purge (not sync.db)."""
        now = datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc)
        age_oct24 = parse_listing_age_days("Listed on 10/24", now=now)
        age_oct28 = parse_listing_age_days("Listed on 10/28", now=now)
        self.assertIsNotNone(age_oct24)
        self.assertIsNotNone(age_oct28)
        assert age_oct24 is not None and age_oct28 is not None
        self.assertGreaterEqual(age_oct24, 12.0)
        self.assertGreaterEqual(age_oct28, 8.0)
        self.assertTrue(is_purge_age_eligible(age_oct24, 7.0))
        self.assertTrue(is_purge_age_eligible(age_oct28, 7.0))

    def test_publicado_slash_es_date(self):
        now = datetime(2026, 11, 5, tzinfo=timezone.utc)
        age = parse_listing_age_days("Publicado el 24/10", now=now)
        self.assertIsNotNone(age)
        assert age is not None
        self.assertGreaterEqual(age, 12.0)

    def test_listed_on_month_name(self):
        now = datetime(2026, 11, 5, tzinfo=timezone.utc)
        age = parse_listing_age_days("Listed on Oct 24", now=now)
        self.assertEqual(age, 12.0)

    def test_unknown(self):
        self.assertIsNone(parse_listing_age_days("Mercedes-Benz G63 $100000"))

    def test_live_age_wins_over_db(self):
        age, source = resolve_listing_age_for_purge(
            live_age_days=None,
            live_age_text="Listed on 10/24",
            live_card_text="2020 Audi A4 Listed on 10/24 $250000",
            db_age_days=1.0,
            now=datetime(2026, 11, 5, tzinfo=timezone.utc),
        )
        self.assertIsNotNone(age)
        assert age is not None
        self.assertGreaterEqual(age, 12.0)
        self.assertIn("ignored", source)
        self.assertTrue(is_purge_age_eligible(age, 7.0))

    def test_exactly_max_days_is_eligible(self):
        self.assertTrue(is_purge_age_eligible(7.0, 7.0))
        self.assertFalse(is_purge_age_eligible(6.9, 7.0))


class TestSellingShelfDiscoveryHydration(unittest.TestCase):
    def test_wait_for_selling_shelf_cards_uses_listed_on_text(self):
        page = MagicMock()
        label = MagicMock()
        label.wait_for.return_value = None
        page.get_by_text.return_value.first = label
        ok = wait_for_selling_shelf_cards(page, timeout_ms=1000)
        self.assertTrue(ok)
        page.get_by_text.assert_called()
        # First wait uses combined EN/ES + bare-date regex.
        first_arg = page.get_by_text.call_args.args[0]
        self.assertTrue(hasattr(first_arg, "search") or first_arg == "Listed on")

    def test_hydrate_selling_shelf_scrolls_runs_loop(self):
        page = MagicMock()
        hydrate_selling_shelf_scrolls(page, scrolls=3)
        self.assertEqual(page.evaluate.call_count, 4)  # 3 scrolls + scroll-top
        self.assertGreaterEqual(page.wait_for_timeout.call_count, 3)

    def test_collect_selling_listings_from_listed_on_cards(self):
        page = MagicMock()
        page.locator.return_value.count.return_value = 0
        with (
            patch(
                "src.facebook.purge_old.wait_for_selling_shelf_cards",
                return_value=True,
            ) as wait,
            patch(
                "src.facebook.purge_old.hydrate_selling_shelf_scrolls",
            ) as hydrate,
            patch(
                "src.facebook.purge_old._extract_selling_cards_via_js",
                return_value=[
                    {
                        "item_id": "1111222233334444",
                        "href": (
                            "https://www.facebook.com/marketplace/item/"
                            "1111222233334444/"
                        ),
                        "date_text": "Listed on 10/24",
                        "blob": "2020 Audi Listed on 10/24 $100000",
                    }
                ],
            ),
            patch(
                "src.facebook.purge_old._extract_selling_cards_via_text_locators",
                return_value=[],
            ),
        ):
            cards = collect_selling_listings(page, max_scrolls=2)
        wait.assert_called()
        hydrate.assert_called()
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].item_id, "1111222233334444")
        self.assertIn("Listed on 10/24", cards[0].age_text or cards[0].card_text)
        self.assertGreaterEqual(cards[0].age_days or 0, 7.0)

    def test_js_extract_script_targets_listed_on_text(self):
        """Ensure the in-page script is text-first, not role=listitem."""
        import inspect

        from src.facebook import purge_old as mod

        src = inspect.getsource(mod._extract_selling_cards_via_js)
        self.assertIn("Listed", src)
        self.assertIn("Publicado", src)
        self.assertIn("climbCard", src)
        self.assertIn("date_text", src)
        self.assertIn("RAW_DATE_RE", src)
        self.assertIn("TITLE_RE", src)
        self.assertIn("depth >= 2 && depth <= 4", src)
        self.assertIn("failed", src)

    def test_bare_slash_dates_es_and_en(self):
        now = datetime(2026, 11, 5, tzinfo=timezone.utc)
        self.assertGreaterEqual(parse_listing_age_days("10/24", now=now) or 0, 12.0)
        self.assertGreaterEqual(parse_listing_age_days("24/10", now=now) or 0, 12.0)
        self.assertGreaterEqual(parse_listing_age_days("10/28", now=now) or 0, 8.0)
        self.assertGreaterEqual(parse_listing_age_days("28/10", now=now) or 0, 8.0)
        self.assertGreaterEqual(
            parse_listing_age_days("Publicado 24/10", now=now) or 0, 12.0
        )

    def test_vehicle_title_from_card_text(self):
        from src.facebook.purge_old import vehicle_title_from_card_text

        self.assertIn(
            "2022 Mercedes-Benz",
            vehicle_title_from_card_text(
                "2022 Mercedes-Benz C300 Publicado el 24/10 $450000"
            ),
        )
        self.assertIn(
            "1957 Plymouth Savoy",
            vehicle_title_from_card_text("1957 Plymouth Savoy Listed on 10/28"),
        )

    def test_title_only_dom_card_ingested(self):
        page = MagicMock()
        page.locator.return_value.count.return_value = 0
        with (
            patch(
                "src.facebook.purge_old.wait_for_selling_shelf_cards",
                return_value=True,
            ),
            patch("src.facebook.purge_old.hydrate_selling_shelf_scrolls"),
            patch(
                "src.facebook.purge_old._extract_selling_cards_via_js",
                return_value=[
                    {
                        "item_id": "",
                        "href": "",
                        "date_text": "24/10",
                        "title": "2022 Mercedes-Benz",
                        "blob": "2022 Mercedes-Benz 24/10 $450000",
                    }
                ],
            ),
            patch(
                "src.facebook.purge_old._extract_selling_cards_via_text_locators",
                return_value=[],
            ),
        ):
            cards = collect_selling_listings(page, max_scrolls=1)
        self.assertEqual(len(cards), 1)
        self.assertTrue(cards[0].item_id.startswith("dom-"))
        self.assertIn("2022 Mercedes-Benz", cards[0].card_text)
        self.assertGreaterEqual(cards[0].age_days or 0, 7.0)


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

    def test_listed_on_text_purges_despite_fresh_db(self):
        """Oct 24 shelf label must purge even if sync.db posted_at is recent."""
        page = MagicMock()
        store = MagicMock()
        store.get_live_listings.return_value = [
            {
                "autosell_id": "obj_stale",
                "account_id": "account_1",
                "fb_listing_url": "https://www.facebook.com/marketplace/item/999/",
                "posted_at": "2026-11-04T12:00:00+00:00",  # ~1d in DB
            }
        ]
        fixed_now = datetime(2026, 11, 5, tzinfo=timezone.utc)
        live_age = parse_listing_age_days("Listed on 10/24", now=fixed_now)
        cards = [
            ShelfListing(
                "999",
                "/marketplace/item/999/",
                live_age,
                "Listed on 10/24",
                "2020 Audi Listed on 10/24 $100000",
            ),
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
                max_days=7,
                dry_run=False,
            )
        self.assertEqual(stats.deleted, 1)
        self.assertEqual(stats.skipped_young, 0)
        remove.assert_called_once()
        store.mark_fb_listing_removed.assert_called_once_with(
            "obj_stale", "account_1", clear_url=True
        )

    def test_reparse_listed_on_when_age_days_missing(self):
        page = MagicMock()
        store = MagicMock()
        store.get_live_listings.return_value = []
        cards = [
            ShelfListing(
                "888",
                "/marketplace/item/888/",
                None,
                "",
                "Mazda CX-5 Listed on 10/28 $180000",
            ),
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
            patch(
                "src.facebook.purge_old.resolve_listing_age_for_purge",
                wraps=resolve_listing_age_for_purge,
            ) as resolve,
        ):
            # Force calendar math to Nov 5 2026 inside resolve wrapper.
            def _resolve(**kwargs):
                return resolve_listing_age_for_purge(
                    **{
                        **kwargs,
                        "now": datetime(2026, 11, 5, tzinfo=timezone.utc),
                    }
                )

            resolve.side_effect = _resolve
            stats = purge_old_listings_on_page(
                page,
                store,
                account_id="account_1",
                max_days=7,
                dry_run=False,
            )
        self.assertEqual(stats.deleted, 1)
        remove.assert_called_once()

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

    def test_item_id_miss_falls_back_to_title_match_delete(self):
        page = MagicMock()
        store = MagicMock()
        store.get_live_listings.return_value = []
        store.lookup_by_fb_item_id.return_value = None
        cards = [
            ShelfListing(
                "999",
                "/marketplace/item/999/",
                10.0,
                "Listed on Oct 24",
                "2018 Audi Q5 Listed on Oct 24 $350000",
            ),
        ]
        with (
            patch(
                "src.facebook.purge_old.collect_selling_listings",
                return_value=cards,
            ),
            patch(
                "src.facebook.purge_old._remove_from_selling_shelf",
                return_value=False,
            ),
            patch(
                "src.facebook.purge_old.remove_from_selling_by_title",
                return_value=True,
            ) as title_rm,
            patch("src.facebook.purge_old.random_delay"),
            patch(
                "src.facebook.purge_old._catalog_by_autosell_id",
                return_value={},
            ),
            patch(
                "src.facebook.purge_old._lookup_db_row",
                return_value=None,
            ),
        ):
            stats = purge_old_listings_on_page(
                page,
                store,
                account_id="account_1",
                max_days=7,
                dry_run=False,
            )
        self.assertEqual(stats.deleted, 1)
        self.assertEqual(stats.failed, 0)
        title_rm.assert_called_once()
        needles = title_rm.call_args.args[1]
        self.assertTrue(any("2018 Audi Q5" in n for n in needles))

    def test_title_needles_from_card_text(self):
        from src.facebook.purge_old import _title_needles_for_purge

        store = MagicMock()
        store.lookup_by_fb_item_id.return_value = None
        with patch(
            "src.facebook.purge_old._catalog_by_autosell_id",
            return_value={},
        ), patch(
            "src.facebook.purge_old._lookup_db_row",
            return_value=None,
        ):
            needles = _title_needles_for_purge(
                store=store,
                account_id="account_1",
                item_id="1",
                autosell_id="",
                vehicle_title="2018 Audi Q5",
                card_text="2018 Audi Q5 Listed on Oct 24 $350000",
            )
        self.assertIn("2018 Audi Q5", needles)

    def test_hard_delete_title_fallback_when_item_href_misses(self):
        from src.facebook.purge_old import hard_delete_listing_via_playwright

        page = MagicMock()
        store = MagicMock()
        with (
            patch(
                "src.facebook.purge_old._remove_from_selling_shelf",
                return_value=False,
            ),
            patch(
                "src.facebook.purge_old.remove_from_selling_by_title",
                return_value=True,
            ) as title_rm,
        ):
            ok = hard_delete_listing_via_playwright(
                page,
                item_id="123",
                listing_url="",
                autosell_id="obj1",
                store=store,
                account_id="account_1",
                vehicle_title="2018 Audi Q5",
                card_text="2018 Audi Q5 Listed on Oct 24",
            )
        self.assertTrue(ok)
        title_rm.assert_called_once()
        self.assertEqual(title_rm.call_args.kwargs.get("action"), "delete")


if __name__ == "__main__":
    unittest.main()
