"""Unit tests — session health pre-check (logged-out / checkpoint detection)."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

from src.facebook import health
from src.facebook.session import detect_session_state, page_shows_checkpoint


def _config(session_dir: str) -> dict:
    return {"accounts": [{"id": "account_1", "session_dir": session_dir}]}


def _populate(session_dir: Path) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "Cookies").write_bytes(b"cookie-db")
    (session_dir / "Preferences").write_text("{}")
    (session_dir / "Local State").write_text("{}")


class TestCheckpointDetection(unittest.TestCase):
    def _page(self, url: str, *, visible: bool = False):
        page = MagicMock()
        page.url = url
        locator = page.get_by_text.return_value
        locator.count.return_value = 1 if visible else 0
        locator.first.is_visible.return_value = visible
        role = page.get_by_role.return_value
        role.count.return_value = 0
        page.locator.return_value.count.return_value = 0
        return page

    def test_checkpoint_url(self):
        page = self._page("https://www.facebook.com/checkpoint/123")
        self.assertTrue(page_shows_checkpoint(page))
        self.assertEqual(detect_session_state(page), "checkpoint")

    def test_checkpoint_text(self):
        page = self._page("https://www.facebook.com/marketplace", visible=True)
        self.assertTrue(page_shows_checkpoint(page))

    def test_clean_marketplace_is_ok(self):
        page = self._page("https://www.facebook.com/marketplace")
        self.assertEqual(detect_session_state(page), "ok")

    def test_login_url_is_logged_out(self):
        page = self._page("https://www.facebook.com/login/")
        self.assertEqual(detect_session_state(page), "logged_out")


class TestProfilePrecheck(unittest.TestCase):
    def test_missing_profile_flags_no_session(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = health.check_profile(_config("sessions/account_1"), "account_1", root)
        self.assertEqual(result.state, health.STATE_NO_SESSION)
        self.assertFalse(result.healthy)
        self.assertIn("fb_login.py", result.detail)

    def test_populated_profile_is_ok(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _populate(root / "sessions" / "account_1")
            result = health.check_profile(_config("sessions/account_1"), "account_1", root)
        self.assertEqual(result.state, health.STATE_OK)
        self.assertTrue(result.healthy)

    def test_precheck_skips_live_probe_for_broken_profile(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            results = health.precheck_accounts(
                _config("sessions/account_1"), ["account_1"], root, live=True
            )
        self.assertEqual([r.state for r in results], [health.STATE_NO_SESSION])
        self.assertFalse(results[0].checked_live)
        self.assertEqual(len(health.unhealthy(results)), 1)
        self.assertIn("account_1", health.format_health_report(results))


if __name__ == "__main__":
    unittest.main()
