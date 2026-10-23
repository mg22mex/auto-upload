from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.facebook.preflight_purge import (
    preflight_purge_config,
    run_preflight_shelf_purge,
)
from src.facebook.purge_old import PurgeStats


class TestPreflightPurgeConfig(unittest.TestCase):
    def test_defaults_from_config(self):
        config = {
            "sync": {
                "preflight_purge": {
                    "enabled": True,
                    "max_days": 0,
                    "keep_autosell_ids": ["obj1173"],
                    "keep_title_re": r"(?i)mustang\s*gt",
                }
            }
        }
        with patch.dict(os.environ, {}, clear=False):
            for key in list(os.environ):
                if "PREFLIGHT" in key or key.startswith("FB_PREFLIGHT"):
                    os.environ.pop(key, None)
            cfg = preflight_purge_config(config)
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.max_days, 0.0)
        self.assertEqual(cfg.keep_autosell_ids, {"obj1173"})
        self.assertIsNotNone(cfg.keep_title_re)
        self.assertTrue(cfg.keep_title_re.search("2025 Ford Mustang GT"))

    def test_env_disable(self):
        with patch.dict(os.environ, {"FB_PREFLIGHT_PURGE": "0"}):
            cfg = preflight_purge_config({"sync": {"preflight_purge": {"enabled": True}}})
        self.assertFalse(cfg.enabled)

    def test_env_keep_ids_merge(self):
        config = {"sync": {"preflight_purge": {"keep_autosell_ids": ["obj1173"]}}}
        with patch.dict(os.environ, {"FB_PREFLIGHT_KEEP_IDS": "obj999,obj1173"}):
            cfg = preflight_purge_config(config)
        self.assertEqual(cfg.keep_autosell_ids, {"obj1173", "obj999"})


class TestRunPreflightShelfPurge(unittest.TestCase):
    def test_skipped_when_disabled(self):
        store = MagicMock()
        with patch.dict(os.environ, {"FB_PREFLIGHT_PURGE": "0"}):
            result = run_preflight_shelf_purge(
                {},
                store,
                ["account_1"],
                root=Path("."),
                dry_run=False,
            )
        self.assertFalse(result.ran)
        self.assertIn("disabled", result.skipped_reason)

    def test_calls_purge_per_account(self):
        store = MagicMock()
        config = {
            "sync": {"preflight_purge": {"enabled": True, "max_days": 0}},
            "facebook": {"headless": True},
            "accounts": [{"id": "account_1", "session_dir": "sessions/account_1"}],
        }
        stats = PurgeStats(found=2, deleted=1)

        ctx = MagicMock()
        ctx.__enter__.return_value = MagicMock()
        ctx.__exit__.return_value = None

        with (
            patch.dict(os.environ, {"FB_PREFLIGHT_PURGE": "1"}),
            patch(
                "src.facebook.preflight_purge.open_account_context",
                return_value=ctx,
            ),
            patch("src.facebook.preflight_purge.get_page", return_value=MagicMock()),
            patch("src.facebook.preflight_purge.is_logged_in", return_value=True),
            patch(
                "src.facebook.preflight_purge.resolve_session_dir",
                return_value=Path("/tmp/sess"),
            ),
            patch(
                "src.facebook.preflight_purge.session_health_report",
                return_value={"exists": True},
            ),
            patch(
                "src.facebook.preflight_purge.purge_old_listings_on_page",
                return_value=stats,
            ) as purge,
        ):
            result = run_preflight_shelf_purge(
                config,
                store,
                ["account_1"],
                root=Path("."),
                dry_run=False,
            )

        self.assertTrue(result.ran)
        self.assertEqual(result.deleted, 1)
        self.assertEqual(result.found, 2)
        purge.assert_called_once()
        kwargs = purge.call_args.kwargs
        self.assertEqual(kwargs["max_days"], 0.0)
        self.assertFalse(kwargs["dry_run"])


if __name__ == "__main__":
    unittest.main()
