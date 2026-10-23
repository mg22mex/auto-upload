"""Unit tests — fast post-publish outcome + duplicate abort."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from src.facebook.errors import FacebookPostingError
from src.facebook.poster import (
    _body_has_any,
    _DUPLICATE_LISTING_PHRASES,
    _publish_outcome,
    _wait_for_publish_outcome,
    raise_if_duplicate_listing,
)


class TestDuplicatePhrases(unittest.TestCase):
    def test_english_and_spanish_hits(self):
        self.assertEqual(
            _body_has_any(
                "this looks like a duplicate of another listing",
                _DUPLICATE_LISTING_PHRASES,
            ),
            "this looks like a duplicate",
        )
        self.assertEqual(
            _body_has_any(
                "error: publicación duplicada detectada",
                _DUPLICATE_LISTING_PHRASES,
            ),
            "publicación duplicada",
        )

    def test_raise_if_duplicate(self):
        page = MagicMock()
        page.locator.return_value.inner_text.return_value = (
            "This looks like a duplicate listing."
        )
        with self.assertRaises(FacebookPostingError) as cm:
            raise_if_duplicate_listing(page, autosell_id="obj1")
        self.assertIn("SKIP_CREATE", str(cm.exception))
        self.assertIn("duplicate", str(cm.exception).lower())


class TestPublishOutcomeRace(unittest.TestCase):
    def test_success_outcome(self):
        page = MagicMock()
        page.locator.return_value.inner_text.return_value = "Your listing is live"
        self.assertEqual(_publish_outcome(page), "success")

    def test_duplicate_outcome(self):
        page = MagicMock()
        page.locator.return_value.inner_text.return_value = (
            "This looks like a duplicate"
        )
        self.assertEqual(_publish_outcome(page), "duplicate")

    def test_wait_raises_on_duplicate_without_full_ladder(self):
        page = MagicMock()
        page.locator.return_value.inner_text.return_value = (
            "parece un duplicado de otra publicación"
        )
        with patch("src.facebook.poster._dismiss_vehicle_category_prompts"):
            with self.assertRaises(FacebookPostingError) as cm:
                _wait_for_publish_outcome(
                    page, autosell_id="obj9", timeout_ms=2_000, poll_ms=50
                )
        self.assertIn("duplicate", str(cm.exception).lower())
        # Must not have slept the old 5+10+15 ladder.
        self.assertLess(page.wait_for_timeout.call_count, 5)

    def test_wait_returns_success_quickly(self):
        page = MagicMock()
        page.locator.return_value.inner_text.return_value = "listing is live now"
        with patch("src.facebook.poster._dismiss_vehicle_category_prompts"):
            outcome = _wait_for_publish_outcome(
                page, autosell_id="obj9", timeout_ms=2_000, poll_ms=50
            )
        self.assertEqual(outcome, "success")
        self.assertEqual(page.wait_for_timeout.call_count, 0)


if __name__ == "__main__":
    unittest.main()
