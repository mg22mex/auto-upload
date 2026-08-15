"""Unit tests — strict delete-before-create for Marketplace repost."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from src.facebook.errors import FacebookPostingError
from src.facebook.remover import extract_item_id
from src.facebook.reposter import _repost_one, execute_reposts
from src.models import SyncAction, Vehicle
from src.store.db import SyncStore


def _vehicle(autosell_id: str = "obj1") -> Vehicle:
    return Vehicle(
        autosell_id=autosell_id,
        slug=autosell_id,
        title=f"Car {autosell_id}",
        brand="Audi",
        year="2020",
        price="100000",
        mileage="10000 km",
        version="",
        url=f"https://www.autosell.mx/{autosell_id}",
        image_urls=["https://example.com/a.jpg"],
    )


def _action(autosell_id: str = "obj1") -> SyncAction:
    return SyncAction(
        action="repost",
        autosell_id=autosell_id,
        slug=autosell_id,
        account_id="account_1",
        reason="test",
        vehicle=_vehicle(autosell_id),
        fb_listing_url="https://www.facebook.com/marketplace/item/1773123456/",
    )


class TestExtractItemId(unittest.TestCase):
    def test_from_url(self):
        self.assertEqual(
            extract_item_id("https://www.facebook.com/marketplace/item/1773123456/"),
            "1773123456",
        )
        self.assertIsNone(extract_item_id(""))


class TestRepostDeleteBeforeCreate(unittest.TestCase):
    def test_create_not_called_when_remove_fails(self):
        page = MagicMock()
        store = MagicMock()
        result = MagicMock(reposts=0)
        order: list[str] = []

        def fail_remove(*_a, **_k):
            order.append("remove")
            return False

        def boom_create(*_a, **_k):
            order.append("create")
            return "https://www.facebook.com/marketplace/item/new/"

        with (
            patch("src.facebook.reposter.remove_vehicle_listing", side_effect=fail_remove),
            patch("src.facebook.reposter.create_vehicle_listing", side_effect=boom_create),
            patch("src.facebook.reposter.ensure_no_matching_shelf_listings", return_value=True),
        ):
            _repost_one(
                page,
                _action(),
                store,
                fb_config={},
                max_photos=5,
                removal_action="delete",
                log_dir=Path("/tmp"),
                result=result,
            )

        self.assertEqual(order, ["remove"])
        store.mark_fb_listing_removed.assert_not_called()
        store.record_repost.assert_not_called()

    def test_unavailable_remove_clears_db_and_creates(self):
        """Content-isn't-available → treat removed, purge URL, then create."""
        page = MagicMock()
        store = MagicMock()
        result = MagicMock(reposts=0)
        order: list[str] = []

        def gone_remove(*_a, **_k):
            order.append("remove")
            return True

        def ok_create(*_a, **_k):
            order.append("create")
            return "https://www.facebook.com/marketplace/item/999/"

        with (
            patch("src.facebook.reposter.remove_vehicle_listing", side_effect=gone_remove),
            patch("src.facebook.reposter.create_vehicle_listing", side_effect=ok_create),
            patch("src.facebook.reposter.ensure_no_matching_shelf_listings", return_value=True),
        ):
            _repost_one(
                page,
                _action(),
                store,
                fb_config={},
                max_photos=5,
                removal_action="delete",
                log_dir=Path("/tmp"),
                result=result,
            )

        self.assertEqual(order, ["remove", "create"])
        store.mark_fb_listing_removed.assert_called()
        store.record_repost.assert_called()
        self.assertEqual(result.reposts, 1)

    def test_remove_before_create_order_and_db_cleared(self):
        page = MagicMock()
        store = MagicMock()
        result = MagicMock(reposts=0)
        order: list[str] = []

        def ok_remove(*_a, **kwargs):
            order.append("remove")
            self.assertTrue(kwargs.get("require_verified"))
            self.assertEqual(kwargs.get("removal_action"), "delete")
            return True

        def ok_create(*_a, **_k):
            order.append("create")
            return "https://www.facebook.com/marketplace/item/999888777/"

        def mark_removed(*_a, **kwargs):
            order.append("db_clear")
            self.assertTrue(kwargs.get("clear_url"))

        def rec_repost(*_a, **_k):
            order.append("db_record")

        store.mark_fb_listing_removed.side_effect = mark_removed
        store.record_repost.side_effect = rec_repost

        with (
            patch("src.facebook.reposter.remove_vehicle_listing", side_effect=ok_remove),
            patch("src.facebook.reposter.create_vehicle_listing", side_effect=ok_create),
            patch("src.facebook.reposter.ensure_no_matching_shelf_listings", return_value=True),
        ):
            _repost_one(
                page,
                _action(),
                store,
                fb_config={},
                max_photos=5,
                removal_action="delete",
                log_dir=Path("/tmp"),
                result=result,
            )

        self.assertEqual(order, ["remove", "db_clear", "create", "db_record"])
        self.assertEqual(result.reposts, 1)

    def test_remove_unverified_skips_create(self):
        page = MagicMock()
        store = MagicMock()
        result = MagicMock(reposts=0)

        with (
            patch(
                "src.facebook.reposter.remove_vehicle_listing",
                return_value=False,
            ),
            patch("src.facebook.reposter.create_vehicle_listing") as create,
        ):
            _repost_one(
                page,
                _action(),
                store,
                fb_config={},
                max_photos=5,
                removal_action="delete",
                log_dir=Path("/tmp"),
                result=result,
            )
        create.assert_not_called()
        store.mark_fb_listing_removed.assert_not_called()
        store.record_repost.assert_not_called()
        self.assertEqual(result.reposts, 0)

    def test_shelf_title_match_blocks_create(self):
        page = MagicMock()
        store = MagicMock()
        result = MagicMock(reposts=0)

        with (
            patch("src.facebook.reposter.remove_vehicle_listing", return_value=True),
            patch(
                "src.facebook.reposter.ensure_no_matching_shelf_listings",
                return_value=False,
            ),
            patch("src.facebook.reposter.create_vehicle_listing") as create,
        ):
            _repost_one(
                page,
                _action(),
                store,
                fb_config={},
                max_photos=5,
                removal_action="delete",
                log_dir=Path("/tmp"),
                result=result,
            )
        create.assert_not_called()
        store.record_repost.assert_not_called()
        self.assertEqual(result.reposts, 0)

    def test_mark_fb_listing_removed_clears_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SyncStore(Path(tmp) / "sync.db")
            store.upsert_fb_listing(
                "obj1",
                "account_1",
                fb_listing_url="https://www.facebook.com/marketplace/item/111/",
                content_hash="abc",
                status="live",
            )
            store.mark_fb_listing_removed("obj1", "account_1", clear_url=True)
            row = store.get_fb_listing("obj1", "account_1")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "removed")
            self.assertIsNone(row["fb_listing_url"])

            store.record_repost(
                "obj1",
                "account_1",
                fb_listing_url="https://www.facebook.com/marketplace/item/222/",
                content_hash="def",
            )
            row2 = store.get_fb_listing("obj1", "account_1")
            self.assertEqual(row2["status"], "live")
            self.assertIn("222", row2["fb_listing_url"])
            store.close()

    def test_execute_reposts_passes_require_path_via_repost_one(self):
        """Removal failures surface as errors; create never runs."""
        actions = [_action("obj1")]
        store = MagicMock()
        root = Path("/tmp")
        create_calls = {"n": 0}

        def fail_one(*_a, **_k):
            raise FacebookPostingError("SKIP_CREATE: remove failed")

        with (
            patch("src.facebook.reposter.open_account_context") as ctx,
            patch("src.facebook.reposter.get_page", return_value=MagicMock()),
            patch("src.facebook.reposter.is_logged_in", return_value=True),
            patch("src.facebook.reposter.page_shows_login_form", return_value=False),
            patch("src.facebook.reposter._repost_one", side_effect=fail_one),
            patch("src.facebook.reposter.random_delay"),
            patch("src.facebook.reposter.ensure_log_dir", return_value=root),
            patch(
                "src.facebook.reposter.create_vehicle_listing",
                side_effect=lambda *_a, **_k: create_calls.__setitem__("n", create_calls["n"] + 1),
            ),
        ):
            from contextlib import contextmanager

            @contextmanager
            def fake_ctx(*_a, **_k):
                yield MagicMock()

            ctx.side_effect = fake_ctx
            res = execute_reposts(
                actions,
                store,
                {
                    "facebook": {"headless": True},
                    "sync": {"repost": {"restart_browser_every": 99, "removal_action": "delete"}},
                },
                root=root,
            )
        self.assertEqual(res.reposts, 0)
        self.assertEqual(len(res.errors), 1)
        self.assertIn("SKIP_CREATE", res.errors[0])
        self.assertEqual(create_calls["n"], 0)


if __name__ == "__main__":
    unittest.main()
