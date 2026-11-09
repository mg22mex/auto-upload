"""Unit tests — Marketplace listing age parse + purge candidate selection."""
from __future__ import annotations

import sqlite3
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

    def test_dom_calendar_overrides_wrong_live_age_days(self):
        """GraphQL ~4.9d must not SKIP-young cards labeled 10/24 or 10/28."""
        now = datetime(2026, 11, 5, tzinfo=timezone.utc)
        age24, src24 = resolve_listing_age_for_purge(
            live_age_days=4.9,
            live_age_text="10/24",
            live_card_text=(
                "2022 Mercedes-Benz Listed on Marketplace · 12 clicks "
                "on listing 10/24 $450000"
            ),
            db_age_days=4.9,
            now=now,
        )
        self.assertEqual(age24, 12.0)
        self.assertIn("dom_calendar", src24)
        self.assertTrue(is_purge_age_eligible(age24, 7.0))

        age28, src28 = resolve_listing_age_for_purge(
            live_age_days=4.9,
            live_age_text="10/28",
            live_card_text="1957 Plymouth Savoy 10/28",
            db_age_days=2.0,
            now=now,
        )
        self.assertEqual(age28, 8.0)
        self.assertIn("dom_calendar", src28)
        self.assertTrue(is_purge_age_eligible(age28, 7.0))

    def test_bare_oct_dates_vs_nov5(self):
        from src.facebook.purge_old import parse_explicit_dom_calendar_age

        now = datetime(2026, 11, 5, tzinfo=timezone.utc)
        age24 = parse_explicit_dom_calendar_age("10/24", now=now)
        age28 = parse_explicit_dom_calendar_age("10/28", now=now)
        self.assertEqual(age24, 12.0)
        self.assertEqual(age28, 8.0)
        assert age24 is not None and age28 is not None
        self.assertTrue(is_purge_age_eligible(age24, 7.0))
        self.assertTrue(is_purge_age_eligible(age28, 7.0))

    def test_newer_meta_date_does_not_beat_october_listing(self):
        """Card blob with 11/1 meta before 10/24 must still yield 12.0d."""
        from src.facebook.purge_old import parse_explicit_dom_calendar_age

        now = datetime(2026, 11, 5, tzinfo=timezone.utc)
        blob = "Boosted 11/1 · 2022 Mercedes-Benz 10/24 $450000"
        age = parse_explicit_dom_calendar_age(blob, now=now)
        self.assertEqual(age, 12.0)
        # 11/1 alone would be ~4.9d — must not win over 10/24.
        self.assertEqual(parse_explicit_dom_calendar_age("11/1", now=now), 4.0)
        age_r, src = resolve_listing_age_for_purge(
            live_age_days=4.9,
            live_age_text="",
            live_card_text=blob,
            db_age_days=5.9,
            now=now,
        )
        self.assertEqual(age_r, 12.0)
        self.assertIn("dom_calendar", src)
        self.assertTrue(is_purge_age_eligible(age_r or 0, 7.0))

    def test_exactly_max_days_is_eligible(self):
        self.assertTrue(is_purge_age_eligible(7.0, 7.0))
        self.assertFalse(is_purge_age_eligible(6.9, 7.0))

    def test_october_hard_purge_floors_1031_in_november(self):
        """FB may show 10/31 on Oct 24/28 cards — still ≥7d in November."""
        from src.facebook.purge_old import (
            apply_october_hard_purge_floor,
            dom_text_has_october_date,
        )

        now = datetime(2026, 11, 5, tzinfo=timezone.utc)
        self.assertTrue(dom_text_has_october_date("10/31"))
        self.assertTrue(dom_text_has_october_date("Listed on 10/31"))
        raw = parse_listing_age_days("10/31", now=now)
        self.assertEqual(raw, 5.0)
        age, src = resolve_listing_age_for_purge(
            live_age_days=5.0,
            live_age_text="10/31",
            live_card_text="2022 Mercedes-Benz Listed on 10/31",
            db_age_days=5.0,
            now=now,
        )
        self.assertEqual(age, 7.0)
        self.assertIn("october_hard_purge", src)
        self.assertTrue(is_purge_age_eligible(age or 0, 7.0))
        floored, hit = apply_october_hard_purge_floor(5.0, "10/28", when=now)
        self.assertTrue(hit)
        self.assertEqual(floored, 7.0)
        # Rule only applies in November.
        oct_now = datetime(2026, 10, 20, tzinfo=timezone.utc)
        _, hit_oct = apply_october_hard_purge_floor(5.0, "10/31", when=oct_now)
        self.assertFalse(hit_oct)


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
        self.assertIn("CLICKS_RE", src)
        self.assertIn("Listed on Marketplace", src)
        self.assertIn("extractDateText", src)
        self.assertIn("withTitle", src)
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

    def test_clicks_label_not_used_as_date_snip(self):
        from src.facebook.purge_old import extract_listed_on_snip

        blob = (
            "2022 Mercedes-Benz C300 Listed on Marketplace · 12 clicks on listing "
            "10/24 $450000"
        )
        snip = extract_listed_on_snip(blob)
        self.assertNotIn("Marketplace", snip)
        self.assertNotIn("clicks", snip.lower())
        self.assertIn("10/24", snip)
        title = __import__(
            "src.facebook.purge_old", fromlist=["vehicle_title_from_card_text"]
        ).vehicle_title_from_card_text(blob)
        self.assertIn("2022 Mercedes-Benz", title)

    def test_october_1031_card_queued_despite_5d_math(self):
        """Visible 10/31 (5.0d raw) still purges via title-match in November."""
        page = MagicMock()
        store = MagicMock()
        store.get_live_listings.return_value = []
        cards = [
            ShelfListing(
                "dom-oct31",
                "",
                5.0,
                "10/31",
                "2018 Audi Q5 Listed on Marketplace · 8 clicks on listing 10/31",
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
            patch(
                "src.facebook.purge_old.resolve_listing_age_for_purge",
                wraps=resolve_listing_age_for_purge,
            ) as resolve,
        ):
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
                account_id="account_2",
                max_days=7,
                dry_run=False,
            )
        self.assertEqual(stats.deleted, 1)
        self.assertEqual(stats.skipped_young, 0)
        title_rm.assert_called_once()

    def test_clicks_label_card_queued_for_ui_delete(self):
        """Title+date from clicks-label climb enters purge (≥7d → UI delete)."""
        page = MagicMock()
        store = MagicMock()
        store.get_live_listings.return_value = []
        cards = [
            ShelfListing(
                "dom-abc123",
                "",
                12.0,
                "10/24",
                "1957 Plymouth Savoy Listed on Marketplace · 3 clicks on listing 10/24",
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
                account_id="account_2",
                max_days=7,
                dry_run=False,
            )
        self.assertEqual(stats.deleted, 1)
        title_rm.assert_called_once()
        needles = title_rm.call_args.args[1]
        self.assertTrue(
            any("1957 Plymouth Savoy" in n or "Plymouth" in n for n in needles)
        )


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

    def test_db_fallback_needles_require_year_no_yearless_cores(self):
        """obj1025 (2019) must not expand to yearless 'Porsche Cayenne'."""
        from src.facebook.purge_old import _title_needles_for_purge

        vehicle = MagicMock()
        vehicle.title = "Porsche Cayenne s +"
        vehicle.year = "2019"
        vehicle.brand = "Porsche"
        vehicle.marketplace_title = "2019 Porsche Cayenne s +"

        store = MagicMock()
        with patch(
            "src.facebook.purge_old._catalog_by_autosell_id",
            return_value={"obj1025": vehicle},
        ), patch(
            "src.facebook.purge_old._lookup_db_row",
            return_value=None,
        ), patch(
            "src.facebook.purge_old._match_needles",
            return_value=[
                "2019 Porsche Cayenne s +",
                "Porsche Cayenne s +",
                "Porsche",
            ],
        ):
            needles = _title_needles_for_purge(
                store=store,
                account_id="account_1",
                item_id="999",
                autosell_id="obj1025",
                vehicle_title="",
                card_text="obj1025 https://www.facebook.com/marketplace/item/999/",
                require_year=True,
            )
        self.assertTrue(needles)
        self.assertTrue(all("2019" in n for n in needles))
        self.assertFalse(any(n.lower() == "porsche cayenne" for n in needles))

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
            patch(
                "src.facebook.purge_old._title_needles_for_purge",
                return_value=["2018 Audi Q5"],
            ),
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
        self.assertTrue(title_rm.call_args.kwargs.get("require_year"))

    def test_hard_delete_already_removed_clears_dead_sync_db(self):
        """404 + year-title absent → ALREADY_REMOVED, not FAIL."""
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
                return_value=False,
            ),
            patch(
                "src.facebook.purge_old._title_needles_for_purge",
                return_value=["2019 Porsche Cayenne"],
            ),
            patch(
                "src.facebook.remover.remove_vehicle_listing",
                return_value=False,
            ),
            patch(
                "src.facebook.purge_old._db_listing_already_gone_on_facebook",
                return_value=True,
            ) as gone,
            patch(
                "src.facebook.purge_old._mark_db_row_already_removed",
            ) as clear,
        ):
            ok = hard_delete_listing_via_playwright(
                page,
                item_id="555",
                listing_url="https://www.facebook.com/marketplace/item/555/",
                autosell_id="obj1025",
                store=store,
                account_id="account_1",
                vehicle_title="2019 Porsche Cayenne s +",
            )
        self.assertTrue(ok)
        gone.assert_called_once()
        clear.assert_called_once()
        self.assertEqual(clear.call_args.kwargs.get("autosell_id"), "obj1025")

    def test_row_as_dict_supports_sqlite_row_get(self):
        """sqlite3.Row has no .get(); _row_as_dict must make .get() safe."""
        from src.facebook.purge_old import (
            _lookup_db_row,
            _posted_at_age_days,
            _row_as_dict,
        )

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE fb_listings ("
            "autosell_id TEXT, account_id TEXT, fb_listing_url TEXT, posted_at TEXT)"
        )
        conn.execute(
            "INSERT INTO fb_listings VALUES (?,?,?,?)",
            (
                "obj1025",
                "account_2",
                "https://www.facebook.com/marketplace/item/555/",
                "2026-10-01T00:00:00+00:00",
            ),
        )
        raw = conn.execute("SELECT * FROM fb_listings").fetchone()
        self.assertIsInstance(raw, sqlite3.Row)
        with self.assertRaises(AttributeError):
            raw.get("autosell_id")  # type: ignore[attr-defined]

        data = _row_as_dict(raw)
        self.assertEqual(data.get("autosell_id"), "obj1025")
        self.assertEqual(data.get("missing", "x"), "x")
        self.assertGreaterEqual(_posted_at_age_days(raw) or 0.0, 7.0)

        store = MagicMock()
        store.get_live_listings.return_value = [raw]
        found = _lookup_db_row(store, "account_2", "555")
        self.assertIsNotNone(found)
        self.assertEqual(found.get("autosell_id"), "obj1025")
        conn.close()

    def test_db_fallback_purge_clears_already_removed_without_fail(self):
        """Empty DOM shelf + dead sync.db row → deleted, not failed."""

        class FakeRow(dict):
            def keys(self):
                return dict.keys(self)

        page = MagicMock()
        store = MagicMock()
        live = FakeRow(
            {
                "account_id": "account_1",
                "fb_listing_url": "https://www.facebook.com/marketplace/item/555/",
                "autosell_id": "obj1025",
                "posted_at": "2026-10-01T00:00:00+00:00",
            }
        )
        store.get_live_listings.return_value = [live]
        store.mark_fb_listing_removed = MagicMock()

        with (
            patch(
                "src.facebook.purge_old.collect_selling_listings",
                return_value=[],
            ),
            patch(
                "src.facebook.purge_old._catalog_by_autosell_id",
                return_value={},
            ),
            patch(
                "src.facebook.purge_old._sort_selling_oldest_first",
            ),
            patch(
                "src.facebook.purge_old.remove_from_selling_by_title",
                return_value=False,
            ) as title_rm,
            patch(
                "src.facebook.purge_old._title_needles_for_purge",
                return_value=["2019 Porsche Cayenne"],
            ),
            patch(
                "src.facebook.purge_old._title_needles_for_row",
                return_value=["2019 Porsche Cayenne"],
            ),
            patch(
                "src.facebook.purge_old._db_listing_already_gone_on_facebook",
                return_value=True,
            ),
            patch("src.facebook.purge_old.random_delay"),
        ):
            stats = purge_old_listings_on_page(
                page,
                store,
                account_id="account_1",
                max_days=7,
                dry_run=False,
            )
        self.assertEqual(stats.failed, 0)
        self.assertEqual(stats.deleted, 1)
        title_rm.assert_called()
        self.assertTrue(title_rm.call_args.kwargs.get("require_year"))
        store.mark_fb_listing_removed.assert_called()


class TestRequierenAtencion(unittest.TestCase):
    def test_extract_requieren_atencion_cards_parses_js_payload(self):
        from src.facebook.purge_old import extract_requieren_atencion_cards

        page = MagicMock()
        page.evaluate.return_value = {
            "section_found": True,
            "heading": "Requieren atención",
            "cards": [
                {
                    "item_id": "",
                    "href": "",
                    "title": "2019 Porsche Cayenne",
                    "blob": (
                        "2019 Porsche Cayenne No se publicó porque parece un "
                        "duplicado. Elimínala y vuelve a intentarlo"
                    ),
                    "has_duplicate_banner": "1",
                    "in_attention_section": "1",
                }
            ],
        }
        cards = extract_requieren_atencion_cards(page)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["title"], "2019 Porsche Cayenne")
        self.assertEqual(cards[0]["has_duplicate_banner"], "1")
        self.assertIn("Requieren atención", page.evaluate.call_args[0][0])

    def test_title_matches_needles(self):
        from src.facebook.purge_old import _title_matches_needles

        self.assertTrue(
            _title_matches_needles(
                "2019 Porsche Cayenne",
                ["2019 Porsche Cayenne S"],
            )
        )
        self.assertFalse(
            _title_matches_needles(
                "2023 Porsche Cayenne",
                ["2019 Porsche Cayenne"],
                require_year=True,
            )
        )

    def test_purge_requieren_atencion_matches_force_deletes(self):
        from src.facebook.purge_old import purge_requieren_atencion_matches

        page = MagicMock()
        with (
            patch(
                "src.facebook.purge_old.extract_requieren_atencion_cards",
                return_value=[
                    {
                        "title": "2019 Porsche Cayenne",
                        "has_duplicate_banner": "1",
                        "blob": "2019 Porsche Cayenne parece un duplicado",
                        "item_id": "555",
                        "href": "https://www.facebook.com/marketplace/item/555/",
                    }
                ],
            ),
            patch(
                "src.facebook.purge_old.force_delete_requieren_atencion_card",
                return_value=True,
            ) as force,
            patch(
                "src.facebook.purge_old._count_attention_title_cards",
                side_effect=[1, 0],
            ),
            patch("src.facebook.purge_old.wait_for_selling_shelf_cards"),
            patch("src.facebook.purge_old.hydrate_selling_shelf_scrolls"),
            patch("src.facebook.purge_old.random_delay"),
        ):
            stats = purge_requieren_atencion_matches(
                page,
                account_id="account_2",
                queue_titles=["2019 Porsche Cayenne"],
                dry_run=False,
            )
        self.assertEqual(stats.found, 1)
        self.assertEqual(stats.deleted, 1)
        self.assertEqual(stats.ui_fallback_deletes, 1)
        force.assert_called_once()
        self.assertEqual(force.call_args.args[1], "2019 Porsche Cayenne")
        self.assertEqual(force.call_args.kwargs.get("item_id"), "555")

    def test_force_delete_uses_action_bar_more_then_delete_text(self):
        from src.facebook.purge_old import force_delete_requieren_atencion_card

        page = MagicMock()
        card = MagicMock()
        with (
            patch("src.facebook.purge_old.hydrate_selling_shelf_scrolls"),
            patch(
                "src.facebook.purge_old._locate_attention_card_root",
                return_value=card,
            ),
            patch(
                "src.facebook.purge_old._href_from_attention_card",
                return_value=("", ""),
            ),
            patch(
                "src.facebook.purge_old._click_card_direct_delete",
                return_value=False,
            ),
            patch(
                "src.facebook.purge_old._click_attention_delete_cta",
                return_value=False,
            ),
            patch(
                "src.facebook.purge_old._click_card_action_bar_more",
                return_value=True,
            ) as more,
            patch(
                "src.facebook.purge_old._click_card_scoped_more",
            ) as scoped,
            patch(
                "src.facebook.purge_old._wait_dropdown_then_click_delete",
                return_value=True,
            ) as menu_del,
            patch(
                "src.facebook.purge_old._count_attention_title_cards",
                side_effect=[2, 1, 1, 1, 1, 1],
            ),
            patch(
                "src.facebook.purge_old._wait_attention_title_count_drop",
                return_value=1,
            ),
        ):
            ok = force_delete_requieren_atencion_card(
                page,
                "2021 Audi Q3 Dinamyc",
                item_id="",
                href="",
            )
        self.assertTrue(ok)
        more.assert_called_once()
        scoped.assert_not_called()
        menu_del.assert_called_once()

    def test_click_card_action_bar_more_prefers_more_aria(self):
        from src.facebook.purge_old import _click_card_action_bar_more

        page = MagicMock()
        card = MagicMock()
        btn = MagicMock()
        btn.is_visible.return_value = True
        btn.get_attribute.side_effect = lambda name: (
            "More options" if name == "aria-label" else ""
        )
        btn.inner_text.return_value = ""
        loc = MagicMock()
        loc.count.return_value = 1
        loc.nth.return_value = btn
        card.locator.return_value = loc
        with patch("src.facebook.purge_old._force_click", return_value=True) as click:
            ok = _click_card_action_bar_more(page, card)
        self.assertTrue(ok)
        click.assert_called()
        # First preferred selector should include More options.
        self.assertTrue(
            any("More options" in str(c) for c in card.locator.call_args_list)
            or card.locator.called
        )

    def test_force_delete_direct_delete_without_menu(self):
        from src.facebook.purge_old import force_delete_requieren_atencion_card

        page = MagicMock()
        card = MagicMock()
        with (
            patch("src.facebook.purge_old.hydrate_selling_shelf_scrolls"),
            patch(
                "src.facebook.purge_old._locate_attention_card_root",
                return_value=card,
            ),
            patch(
                "src.facebook.purge_old._href_from_attention_card",
                return_value=("", ""),
            ),
            patch(
                "src.facebook.purge_old._click_card_direct_delete",
                return_value=True,
            ) as direct,
            patch(
                "src.facebook.purge_old._click_card_scoped_more",
            ) as more,
            patch(
                "src.facebook.purge_old._count_attention_title_cards",
                side_effect=[2, 1, 1, 1, 1, 1],
            ),
            patch(
                "src.facebook.purge_old._wait_attention_title_count_drop",
                return_value=1,
            ),
        ):
            ok = force_delete_requieren_atencion_card(
                page,
                "2022 Toyota Corolla XLE",
            )
        self.assertTrue(ok)
        direct.assert_called_once()
        more.assert_not_called()

    def test_force_delete_banner_when_no_action_control(self):
        from src.facebook.purge_old import force_delete_requieren_atencion_card

        page = MagicMock()
        card = MagicMock()
        with (
            patch("src.facebook.purge_old.hydrate_selling_shelf_scrolls"),
            patch(
                "src.facebook.purge_old._locate_attention_card_root",
                return_value=card,
            ),
            patch(
                "src.facebook.purge_old._href_from_attention_card",
                return_value=("", ""),
            ),
            patch(
                "src.facebook.purge_old._click_card_direct_delete",
                return_value=False,
            ),
            patch(
                "src.facebook.purge_old._click_attention_delete_cta",
                return_value=False,
            ),
            patch(
                "src.facebook.purge_old._click_card_action_bar_more",
                return_value=False,
            ),
            patch(
                "src.facebook.purge_old._click_card_scoped_more",
                return_value=False,
            ),
            patch(
                "src.facebook.purge_old._click_attention_banner_text",
                return_value=True,
            ) as banner,
            patch(
                "src.facebook.purge_old._wait_confirm_delete_modal",
                return_value=True,
            ),
            patch(
                "src.facebook.purge_old._delete_attention_via_item_url",
                return_value=False,
            ),
            patch(
                "src.facebook.purge_old._count_attention_title_cards",
                side_effect=[2, 1, 1, 1, 1, 1],
            ),
            patch(
                "src.facebook.purge_old._wait_attention_title_count_drop",
                return_value=1,
            ),
        ):
            ok = force_delete_requieren_atencion_card(page, "2021 Audi Q5")
        self.assertTrue(ok)
        banner.assert_called()

    def test_force_delete_ok_when_duplicate_title_still_visible(self):
        """count 2→1 is success even though title text remains in the DOM."""
        from src.facebook.purge_old import force_delete_requieren_atencion_card

        page = MagicMock()
        card = MagicMock()
        with (
            patch("src.facebook.purge_old.hydrate_selling_shelf_scrolls"),
            patch(
                "src.facebook.purge_old._locate_attention_card_root",
                return_value=card,
            ),
            patch(
                "src.facebook.purge_old._href_from_attention_card",
                return_value=("", ""),
            ),
            patch(
                "src.facebook.purge_old._click_card_direct_delete",
                return_value=True,
            ),
            patch(
                "src.facebook.purge_old._wait_confirm_delete_modal",
                return_value=True,
            ) as confirm,
            patch(
                "src.facebook.purge_old._count_attention_title_cards",
                side_effect=[2, 1],
            ),
            patch(
                "src.facebook.purge_old._wait_attention_title_count_drop",
                return_value=1,
            ) as drop,
            patch(
                "src.facebook.purge_old._attention_title_still_visible",
                return_value=True,
            ),
        ):
            ok = force_delete_requieren_atencion_card(
                page, "2022 Toyota Corolla XLE"
            )
        self.assertTrue(ok)
        confirm.assert_called()
        drop.assert_called()

    def test_force_delete_confirms_modal_before_count_check(self):
        """Delete listing? must be confirmed before count_after is evaluated."""
        from src.facebook.purge_old import force_delete_requieren_atencion_card

        page = MagicMock()
        card = MagicMock()
        order: list[str] = []

        def _confirm(*_a, **_k):
            order.append("confirm")
            return True

        def _drop(*_a, **_k):
            order.append("count")
            return 1

        with (
            patch("src.facebook.purge_old.hydrate_selling_shelf_scrolls"),
            patch(
                "src.facebook.purge_old._locate_attention_card_root",
                return_value=card,
            ),
            patch(
                "src.facebook.purge_old._href_from_attention_card",
                return_value=("", ""),
            ),
            patch(
                "src.facebook.purge_old._click_card_direct_delete",
                return_value=True,
            ),
            patch(
                "src.facebook.purge_old._wait_confirm_delete_modal",
                side_effect=_confirm,
            ),
            patch(
                "src.facebook.purge_old._count_attention_title_cards",
                side_effect=[2, 1, 1],
            ),
            patch(
                "src.facebook.purge_old._wait_attention_title_count_drop",
                side_effect=_drop,
            ),
        ):
            ok = force_delete_requieren_atencion_card(
                page, "2022 Toyota Corolla XLE"
            )
        self.assertTrue(ok)
        self.assertEqual(order[:2], ["confirm", "count"])

    def test_force_delete_skips_count_when_confirm_modal_stuck(self):
        """Stuck Delete listing? dialog → no success / no count-based OK."""
        from src.facebook.purge_old import force_delete_requieren_atencion_card

        page = MagicMock()
        card = MagicMock()
        with (
            patch("src.facebook.purge_old.hydrate_selling_shelf_scrolls"),
            patch(
                "src.facebook.purge_old._locate_attention_card_root",
                return_value=card,
            ),
            patch(
                "src.facebook.purge_old._href_from_attention_card",
                return_value=("", ""),
            ),
            patch(
                "src.facebook.purge_old._click_card_direct_delete",
                return_value=True,
            ),
            patch(
                "src.facebook.purge_old._click_attention_delete_cta",
                return_value=False,
            ),
            patch(
                "src.facebook.purge_old._click_card_action_bar_more",
                return_value=False,
            ),
            patch(
                "src.facebook.purge_old._click_card_scoped_more",
                return_value=False,
            ),
            patch(
                "src.facebook.purge_old._click_attention_banner_text",
                return_value=False,
            ),
            patch(
                "src.facebook.purge_old._delete_attention_via_item_url",
                return_value=False,
            ),
            patch(
                "src.facebook.purge_old.remove_from_selling_by_title",
                return_value=False,
            ),
            patch(
                "src.facebook.purge_old._wait_confirm_delete_modal",
                return_value=False,
            ),
            patch(
                "src.facebook.purge_old._count_attention_title_cards",
                return_value=2,
            ),
            patch(
                "src.facebook.purge_old._wait_attention_title_count_drop",
            ) as drop,
        ):
            ok = force_delete_requieren_atencion_card(
                page, "2022 Toyota Corolla XLE"
            )
        self.assertFalse(ok)
        drop.assert_not_called()

    def test_click_delete_confirm_in_dialog_clicks_primary_delete(self):
        from src.facebook.purge_old import _click_delete_confirm_in_dialog

        page = MagicMock()
        dlg = MagicMock()
        dlg.is_visible.return_value = True
        btn = MagicMock()
        btn.count.return_value = 1
        btn.first.is_visible.return_value = True
        dlg.get_by_role.return_value = btn
        page.locator.return_value.last = dlg
        with patch("src.facebook.purge_old._force_click", return_value=True) as click:
            ok = _click_delete_confirm_in_dialog(page)
        self.assertTrue(ok)
        dlg.get_by_role.assert_any_call("button", name="Delete", exact=True)
        click.assert_called()

    def test_force_delete_url_fallback_when_menu_delete_misses(self):
        from src.facebook.purge_old import force_delete_requieren_atencion_card

        page = MagicMock()
        card = MagicMock()
        page.get_by_text.return_value.first.count.return_value = 0
        page.get_by_text.return_value.count.return_value = 0
        with (
            patch("src.facebook.purge_old.hydrate_selling_shelf_scrolls"),
            patch(
                "src.facebook.purge_old._locate_attention_card_root",
                return_value=card,
            ),
            patch(
                "src.facebook.purge_old._href_from_attention_card",
                return_value=(
                    "999",
                    "https://www.facebook.com/marketplace/item/999/",
                ),
            ),
            patch(
                "src.facebook.purge_old._click_card_direct_delete",
                return_value=False,
            ),
            patch(
                "src.facebook.purge_old._click_attention_delete_cta",
                return_value=False,
            ),
            patch(
                "src.facebook.purge_old._click_card_action_bar_more",
                return_value=True,
            ),
            patch(
                "src.facebook.purge_old._click_card_scoped_more",
                return_value=False,
            ),
            patch(
                "src.facebook.purge_old._wait_dropdown_then_click_delete",
                return_value=False,
            ),
            patch(
                "src.facebook.purge_old._click_attention_banner_text",
                return_value=False,
            ),
            patch(
                "src.facebook.purge_old._delete_attention_via_item_url",
                return_value=True,
            ) as url_rm,
            patch(
                "src.facebook.purge_old.remove_from_selling_by_title",
                return_value=False,
            ),
            patch(
                "src.facebook.purge_old._count_attention_title_cards",
                side_effect=[2, 1, 1, 1, 1, 1],
            ),
            patch(
                "src.facebook.purge_old._wait_attention_title_count_drop",
                return_value=1,
            ),
        ):
            ok = force_delete_requieren_atencion_card(
                page,
                "2022 Toyota Corolla XLE",
                item_id="",
                href="",
            )
        self.assertTrue(ok)
        url_rm.assert_called()
        self.assertEqual(url_rm.call_args.kwargs.get("item_id"), "999")

    def test_purge_loops_identical_duplicate_titles(self):
        """Two cards with the same title → two successful force-deletes."""
        from src.facebook.purge_old import purge_requieren_atencion_matches

        page = MagicMock()
        twin = {
            "title": "2022 Toyota Corolla XLE",
            "has_duplicate_banner": "1",
            "blob": "2022 Toyota Corolla XLE parece un duplicado",
            "item_id": "",
            "href": "",
        }
        counts = iter([2, 1, 0])  # remaining before each round / after last

        def _count(_page, title, cards=None):
            try:
                return next(counts)
            except StopIteration:
                return 0

        with (
            patch(
                "src.facebook.purge_old.extract_requieren_atencion_cards",
                return_value=[twin, dict(twin)],
            ),
            patch("src.facebook.purge_old.wait_for_selling_shelf_cards"),
            patch("src.facebook.purge_old.hydrate_selling_shelf_scrolls"),
            patch("src.facebook.purge_old.random_delay"),
            patch(
                "src.facebook.purge_old._count_attention_title_cards",
                side_effect=_count,
            ),
            patch(
                "src.facebook.purge_old.force_delete_requieren_atencion_card",
                return_value=True,
            ) as force,
        ):
            stats = purge_requieren_atencion_matches(
                page,
                account_id="account_1",
                queue_titles=["2022 Toyota Corolla XLE"],
                dry_run=False,
            )
        self.assertEqual(stats.found, 2)
        self.assertEqual(stats.deleted, 2)
        self.assertEqual(stats.failed, 0)
        self.assertEqual(force.call_count, 2)

    def test_click_delete_text_direct_uses_get_by_text(self):
        from src.facebook.purge_old import _click_delete_text_direct

        page = MagicMock()
        loc = MagicMock()
        loc.count.return_value = 1
        item = MagicMock()
        item.is_visible.return_value = True
        loc.nth.return_value = item
        page.get_by_text.return_value = loc
        page.locator.return_value.count.return_value = 0
        with patch("src.facebook.purge_old._force_click", return_value=True) as click:
            ok = _click_delete_text_direct(page)
        self.assertTrue(ok)
        page.get_by_text.assert_any_call("Delete listing", exact=True)
        click.assert_called()

    def test_collect_selling_includes_attention_extract(self):
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
                return_value=[],
            ),
            patch(
                "src.facebook.purge_old._extract_selling_cards_via_text_locators",
                return_value=[],
            ),
            patch(
                "src.facebook.purge_old.extract_requieren_atencion_cards",
                return_value=[
                    {
                        "item_id": "",
                        "href": "",
                        "date_text": "",
                        "title": "2020 BMW X5",
                        "blob": (
                            "2020 BMW X5 No se publicó porque parece un duplicado"
                        ),
                        "has_duplicate_banner": "1",
                        "in_attention_section": "1",
                    }
                ],
            ),
        ):
            cards = collect_selling_listings(page, max_scrolls=1)
        self.assertEqual(len(cards), 1)
        self.assertIn("2020 BMW X5", cards[0].card_text)


if __name__ == "__main__":
    unittest.main()
