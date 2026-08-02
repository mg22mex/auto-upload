"""Unit tests — alternating weekly renew/repost mode."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.sync.weekly_bump import (
    resolve_weekly_bump_mode,
    weekly_bump_config,
)


class TestResolveWeeklyBumpMode(unittest.TestCase):
    def test_even_iso_week_is_renew(self):
        # 2026-08-02 is ISO week 31 (odd) — use a known even week Sunday
        # 2026-08-09 is Sunday of ISO week 32 (even)
        when = datetime(2026, 8, 9, 9, 0, tzinfo=ZoneInfo("America/Chihuahua"))
        self.assertEqual(when.isocalendar().week % 2, 0)
        self.assertEqual(resolve_weekly_bump_mode(when), "renew")

    def test_odd_iso_week_is_repost(self):
        # 2026-08-02 Sunday → ISO week 31 (odd)
        when = datetime(2026, 8, 2, 9, 0, tzinfo=ZoneInfo("America/Chihuahua"))
        self.assertEqual(when.isocalendar().week % 2, 1)
        self.assertEqual(resolve_weekly_bump_mode(when), "repost")

    def test_force_mode_overrides_week(self):
        when = datetime(2026, 8, 2, 9, 0, tzinfo=ZoneInfo("America/Chihuahua"))
        self.assertEqual(
            resolve_weekly_bump_mode(when, force_mode="renew"),
            "renew",
        )
        self.assertEqual(
            resolve_weekly_bump_mode(when, force_mode="repost"),
            "repost",
        )

    def test_force_auto_uses_calendar(self):
        when = datetime(2026, 8, 2, 9, 0, tzinfo=ZoneInfo("America/Chihuahua"))
        self.assertEqual(
            resolve_weekly_bump_mode(when, force_mode="auto"),
            "repost",
        )

    def test_swapped_config(self):
        when = datetime(2026, 8, 9, 9, 0, tzinfo=ZoneInfo("America/Chihuahua"))
        self.assertEqual(
            resolve_weekly_bump_mode(when, even_week="repost", odd_week="renew"),
            "repost",
        )

    def test_invalid_force(self):
        with self.assertRaises(ValueError):
            resolve_weekly_bump_mode(force_mode="bump")


class TestWeeklyBumpConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = weekly_bump_config({})
        self.assertEqual(cfg["even_week"], "renew")
        self.assertEqual(cfg["odd_week"], "repost")
        self.assertEqual(cfg["timezone"], "America/Chihuahua")

    def test_reads_nested(self):
        cfg = weekly_bump_config(
            {
                "sync": {
                    "weekly_bump": {
                        "even_week": "repost",
                        "odd_week": "renew",
                        "timezone": "UTC",
                    }
                }
            }
        )
        self.assertEqual(cfg["even_week"], "repost")
        self.assertEqual(cfg["timezone"], "UTC")


if __name__ == "__main__":
    unittest.main()
