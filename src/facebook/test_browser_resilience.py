"""Unit tests — browser-dead detection + resilient repost loop."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.facebook.browser_health import is_browser_dead
from src.facebook.reposter import execute_reposts
from src.models import SyncAction, Vehicle


class TargetClosedError(Exception):
    """Stand-in for Playwright TargetClosedError."""


class BrowserHealthTests(unittest.TestCase):
    def test_target_closed_by_type_name(self):
        self.assertTrue(is_browser_dead(TargetClosedError("gone")))

    def test_target_closed_by_message(self):
        self.assertTrue(is_browser_dead(RuntimeError("Target closed")))
        self.assertTrue(is_browser_dead(Exception("Browser has been closed")))

    def test_normal_errors_are_not_dead(self):
        self.assertFalse(is_browser_dead(ValueError("bad price")))
        self.assertFalse(is_browser_dead(Exception("Mark-as-sold control not found")))


def _vehicle(autosell_id: str) -> Vehicle:
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


def _repost_action(autosell_id: str, account_id: str = "account_1") -> SyncAction:
    return SyncAction(
        action="repost",
        autosell_id=autosell_id,
        slug=autosell_id,
        account_id=account_id,
        reason="test",
        vehicle=_vehicle(autosell_id),
        fb_listing_url=f"https://www.facebook.com/marketplace/item/{autosell_id}/",
    )


class ResilientRepostTests(unittest.TestCase):
    def test_reopens_browser_and_retries_after_target_closed(self):
        actions = [_repost_action("obj1"), _repost_action("obj2")]
        store = MagicMock()
        config = {
            "facebook": {"headless": True, "max_photos_per_listing": 1},
            "sync": {
                "removal_action": "mark_sold",
                "repost": {"restart_browser_every": 99, "max_browser_reopens": 5},
            },
        }
        root = Path("/tmp")
        call_count = {"n": 0}
        contexts_opened = {"n": 0}

        @contextmanager
        def fake_context(*_a, **_k):
            contexts_opened["n"] += 1
            yield MagicMock()

        def fake_repost_one(_page, _action, _store, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise TargetClosedError(
                    "Target page, context or browser has been closed"
                )
            kwargs["result"].reposts += 1

        with (
            patch("src.facebook.reposter.open_account_context", fake_context),
            patch("src.facebook.reposter.get_page", return_value=MagicMock()),
            patch("src.facebook.reposter.is_logged_in", return_value=True),
            patch("src.facebook.reposter.page_shows_login_form", return_value=False),
            patch("src.facebook.reposter._repost_one", side_effect=fake_repost_one),
            patch("src.facebook.reposter.random_delay"),
            patch("src.facebook.reposter.ensure_log_dir", return_value=root),
        ):
            result = execute_reposts(actions, store, config, root=root)

        self.assertEqual(result.reposts, 2)
        self.assertGreaterEqual(result.browser_reopens, 1)
        self.assertGreaterEqual(contexts_opened["n"], 2)
        self.assertEqual(call_count["n"], 3)  # fail once + 2 successes

    def test_proactive_restart_every_n(self):
        actions = [_repost_action(f"obj{i}") for i in range(4)]
        store = MagicMock()
        config = {
            "facebook": {"headless": True},
            "sync": {
                "repost": {"restart_browser_every": 2, "max_browser_reopens": 5},
            },
        }
        root = Path("/tmp")
        contexts_opened = {"n": 0}

        @contextmanager
        def fake_context(*_a, **_k):
            contexts_opened["n"] += 1
            yield MagicMock()

        def ok_repost(_page, _action, _store, **kwargs):
            kwargs["result"].reposts += 1

        with (
            patch("src.facebook.reposter.open_account_context", fake_context),
            patch("src.facebook.reposter.get_page", return_value=MagicMock()),
            patch("src.facebook.reposter.is_logged_in", return_value=True),
            patch("src.facebook.reposter.page_shows_login_form", return_value=False),
            patch("src.facebook.reposter._repost_one", side_effect=ok_repost),
            patch("src.facebook.reposter.random_delay"),
            patch("src.facebook.reposter.ensure_log_dir", return_value=root),
        ):
            result = execute_reposts(actions, store, config, root=root)

        self.assertEqual(result.reposts, 4)
        self.assertEqual(contexts_opened["n"], 2)  # 2 + 2
        self.assertEqual(result.browser_reopens, 0)


if __name__ == "__main__":
    unittest.main()
