"""Unit tests — browser-dead detection + resilient repost loop."""

from __future__ import annotations

import tempfile
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


def _accounts_config(*account_ids: str) -> list[dict]:
    return [
        {
            "id": account_id,
            "session_dir": f"sessions/{account_id}",
            "facebook_c_user": f"c_user_{account_id}",
        }
        for account_id in account_ids
    ]


def _healthy_session_report(path: str = "/tmp/fake-session") -> dict:
    return {
        "exists": True,
        "file_count": 5,
        "has_cookies_file": True,
        "looks_empty": False,
        "path": path,
        "bytes": 1024,
    }


def _seed_dummy_session(root: Path, account_id: str) -> Path:
    """Minimal Chromium-like profile so session_health_report is not empty."""
    session_dir = root / "sessions" / account_id
    default = session_dir / "Default"
    default.mkdir(parents=True, exist_ok=True)
    (default / "Cookies").write_bytes(b"dummy-cookies")
    (session_dir / "Local State").write_text("{}", encoding="utf-8")
    return session_dir


@contextmanager
def _repost_resilience_patches(
    *,
    fake_context,
    is_logged_in=True,
    page_shows_login_form=False,
    repost_one=None,
    assert_identity=None,
    log_dir: Path | None = None,
):
    """Common mocks: no live FB profile required on CI disks."""
    identity = assert_identity or (lambda *_a, **_k: "c_user_mock")
    if callable(is_logged_in) and not isinstance(is_logged_in, bool):
        logged_in_patch = patch(
            "src.facebook.reposter.is_logged_in", side_effect=is_logged_in
        )
    else:
        logged_in_patch = patch(
            "src.facebook.reposter.is_logged_in", return_value=bool(is_logged_in)
        )
    with (
        patch("src.facebook.reposter.open_account_context", fake_context),
        patch("src.facebook.reposter.get_page", return_value=MagicMock()),
        logged_in_patch,
        patch(
            "src.facebook.reposter.page_shows_login_form",
            return_value=page_shows_login_form,
        ),
        patch(
            "src.facebook.reposter._repost_one",
            side_effect=repost_one if repost_one is not None else MagicMock(),
        ),
        patch("src.facebook.reposter.assert_session_identity", side_effect=identity),
        patch(
            "src.facebook.reposter.session_health_report",
            return_value=_healthy_session_report(),
        ),
        patch("src.facebook.reposter.random_delay"),
        patch(
            "src.facebook.reposter.ensure_log_dir",
            return_value=log_dir or Path("/tmp"),
        ),
    ):
        yield


class ResilientRepostTests(unittest.TestCase):
    def test_reopens_browser_and_retries_after_target_closed(self):
        actions = [_repost_action("obj1"), _repost_action("obj2")]
        store = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_dummy_session(root, "account_1")
            config = {
                "facebook": {"headless": True, "max_photos_per_listing": 1},
                "accounts": _accounts_config("account_1"),
                "sync": {
                    "removal_action": "mark_sold",
                    "repost": {"restart_browser_every": 99, "max_browser_reopens": 5},
                },
            }
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

            with _repost_resilience_patches(
                fake_context=fake_context,
                repost_one=fake_repost_one,
                log_dir=root / "logs",
            ):
                result = execute_reposts(actions, store, config, root=root)

            self.assertEqual(result.reposts, 2)
            self.assertGreaterEqual(result.browser_reopens, 1)
            self.assertGreaterEqual(contexts_opened["n"], 2)
            self.assertEqual(call_count["n"], 3)  # fail once + 2 successes

    def test_proactive_restart_every_n(self):
        actions = [_repost_action(f"obj{i}") for i in range(4)]
        store = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_dummy_session(root, "account_1")
            config = {
                "facebook": {"headless": True},
                "accounts": _accounts_config("account_1"),
                "sync": {
                    "repost": {"restart_browser_every": 2, "max_browser_reopens": 5},
                },
            }
            contexts_opened = {"n": 0}

            @contextmanager
            def fake_context(*_a, **_k):
                contexts_opened["n"] += 1
                yield MagicMock()

            def ok_repost(_page, _action, _store, **kwargs):
                kwargs["result"].reposts += 1

            with _repost_resilience_patches(
                fake_context=fake_context,
                repost_one=ok_repost,
                log_dir=root / "logs",
            ):
                result = execute_reposts(actions, store, config, root=root)

            self.assertEqual(result.reposts, 4)
            self.assertEqual(contexts_opened["n"], 2)  # 2 + 2
            self.assertEqual(result.browser_reopens, 0)

    def test_session_expired_skips_account_continues_next(self):
        """account_2 not logged in must not abort account_1."""
        actions = [
            _repost_action("a2_car", account_id="account_2"),
            _repost_action("a1_car", account_id="account_1"),
        ]
        store = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_dummy_session(root, "account_1")
            _seed_dummy_session(root, "account_2")
            config = {
                "facebook": {"headless": True, "max_photos_per_listing": 1},
                "accounts": _accounts_config("account_1", "account_2"),
                "sync": {
                    "repost": {"restart_browser_every": 99, "max_browser_reopens": 5},
                },
            }
            posted: list[str] = []

            @contextmanager
            def fake_context(*_a, **_k):
                yield MagicMock()

            def fake_logged_in(_page):
                # First context is account_2 (ordered first).
                return (
                    fake_logged_in.calls.pop(0) if fake_logged_in.calls else True
                )

            fake_logged_in.calls = [False, True]

            def fake_repost_one(_page, action, _store, **kwargs):
                posted.append(action.account_id)
                kwargs["result"].reposts += 1

            def fake_identity(_context, _config, account_id):
                # Only reached when is_logged_in is True (account_1).
                return f"c_user_{account_id}"

            with _repost_resilience_patches(
                fake_context=fake_context,
                is_logged_in=fake_logged_in,
                repost_one=fake_repost_one,
                assert_identity=fake_identity,
                log_dir=root / "logs",
            ):
                result = execute_reposts(
                    actions,
                    store,
                    config,
                    root=root,
                    account_order=["account_2", "account_1"],
                )

            self.assertEqual(result.session_expired_accounts, ["account_2"])
            self.assertEqual(result.accounts_ok, ["account_1"])
            self.assertEqual(posted, ["account_1"])
            self.assertEqual(result.reposts, 1)
            self.assertTrue(
                any("FAILED_SESSION_EXPIRED" in e for e in result.errors)
            )
            self.assertTrue(
                any(
                    "[SKIP] account_2" in e or "account_2" in e
                    for e in result.errors
                )
            )


if __name__ == "__main__":
    unittest.main()
