"""Unit tests — duplicate create abort + sync.db mapping."""
from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.facebook.errors import FacebookPostingError
from src.facebook.executor import execute_actions
from src.facebook.poster import is_duplicate_listing_error
from src.models import SyncAction, Vehicle
from src.store.db import SyncStore
from src.sync.engine import plan_sync_actions


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


class TestDuplicateErrorDetection(unittest.TestCase):
    def test_detects_skip_create_and_spanish(self):
        self.assertTrue(
            is_duplicate_listing_error(
                FacebookPostingError("SKIP_CREATE: publicación duplicada")
            )
        )
        self.assertTrue(
            is_duplicate_listing_error("This looks like a duplicate listing")
        )
        self.assertFalse(is_duplicate_listing_error("Required vehicle fields missing"))


class TestDuplicateBlockedDb(unittest.TestCase):
    def test_mark_and_plan_skips_create(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SyncStore(Path(tmp) / "sync.db")
            store.mark_duplicate_blocked(
                "obj1", "account_1", content_hash="abc", reason="fb_duplicate"
            )
            row = store.get_fb_listing("obj1", "account_1")
            self.assertEqual(row["status"], "duplicate_blocked")
            self.assertIsNone(row["fb_listing_url"])
            self.assertTrue(store.is_duplicate_create_blocked("obj1", "account_1"))

            actions = plan_sync_actions(
                [_vehicle("obj1")],
                ["account_1"],
                live_listings=[],
                max_creates_per_account=5,
                is_create_blocked=store.is_duplicate_create_blocked,
            )
            self.assertEqual(actions, [])
            store.close()

    def test_executor_marks_db_on_duplicate_create(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SyncStore(Path(tmp) / "sync.db")
            action = SyncAction(
                action="create",
                autosell_id="obj9",
                slug="obj9",
                account_id="account_1",
                reason="test",
                vehicle=_vehicle("obj9"),
            )
            config = {
                "facebook": {"headless": True, "max_photos_per_listing": 1},
                "sync": {"removal_action": "delete"},
            }

            @contextmanager
            def fake_ctx(*_a, **_k):
                yield MagicMock()

            with (
                patch(
                    "src.facebook.executor.create_vehicle_listing",
                    side_effect=FacebookPostingError(
                        "SKIP_CREATE: Facebook duplicate listing detected "
                        "('this looks like a duplicate')"
                    ),
                ),
                patch("src.facebook.executor.open_account_context", fake_ctx),
                patch("src.facebook.executor.get_page", return_value=MagicMock()),
                patch("src.facebook.executor.is_logged_in", return_value=True),
                patch("src.facebook.executor.page_shows_login_form", return_value=False),
                patch("src.facebook.executor.random_delay"),
                patch("src.facebook.executor.ensure_log_dir", return_value=Path(tmp)),
            ):
                result = execute_actions(
                    [action],
                    store,
                    config,
                    root=Path(tmp),
                    account_order=["account_1"],
                )

            self.assertEqual(result.creates, 0)
            self.assertTrue(any("duplicate" in e.lower() for e in result.errors))
            row = store.get_fb_listing("obj9", "account_1")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "duplicate_blocked")
            store.close()


if __name__ == "__main__":
    unittest.main()
