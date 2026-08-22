"""Unit tests — relist-first listing bump + 2-day age eligibility."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.models import Vehicle
from src.sync.repost import plan_repost_actions
from src.sync.weekly_bump import (
    DEFAULT_EVEN_WEEK,
    DEFAULT_MAX_PER_ACCOUNT_PER_RUN,
    DEFAULT_MIN_AGE_DAYS,
    DEFAULT_ODD_WEEK,
    resolve_weekly_bump_mode,
    weekly_bump_config,
)


def _vehicle(autosell_id: str) -> Vehicle:
    return Vehicle(
        autosell_id=autosell_id,
        slug=autosell_id,
        title=f"Model {autosell_id}",
        brand="Audi",
        year="2020",
        price="100000",
        mileage="10000 km",
        version="",
        url=f"https://www.autosell.mx/{autosell_id}",
        image_urls=["https://example.com/a.jpg"],
    )


def _listing(
    autosell_id: str,
    account_id: str,
    *,
    posted_at: str,
    status: str = "live",
) -> dict:
    return {
        "autosell_id": autosell_id,
        "account_id": account_id,
        "fb_listing_url": f"https://www.facebook.com/marketplace/item/{autosell_id}/",
        "status": status,
        "content_hash": "abc",
        "posted_at": posted_at,
    }


class TestResolveWeeklyBumpMode(unittest.TestCase):
    def test_defaults_are_repost_both_weeks(self):
        self.assertEqual(DEFAULT_EVEN_WEEK, "repost")
        self.assertEqual(DEFAULT_ODD_WEEK, "repost")
        self.assertEqual(DEFAULT_MIN_AGE_DAYS, 2.0)
        self.assertEqual(DEFAULT_MAX_PER_ACCOUNT_PER_RUN, 25)

    def test_even_iso_week_is_repost(self):
        # 2026-08-09 is Sunday of ISO week 32 (even)
        when = datetime(2026, 8, 9, 9, 0, tzinfo=ZoneInfo("America/Chihuahua"))
        self.assertEqual(when.isocalendar().week % 2, 0)
        self.assertEqual(resolve_weekly_bump_mode(when), "repost")

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

    def test_force_auto_uses_calendar_defaults(self):
        when = datetime(2026, 8, 2, 9, 0, tzinfo=ZoneInfo("America/Chihuahua"))
        self.assertEqual(
            resolve_weekly_bump_mode(when, force_mode="auto"),
            "repost",
        )

    def test_legacy_alternate_config_still_works(self):
        when = datetime(2026, 8, 9, 9, 0, tzinfo=ZoneInfo("America/Chihuahua"))
        self.assertEqual(
            resolve_weekly_bump_mode(when, even_week="renew", odd_week="repost"),
            "renew",
        )
        when_odd = datetime(2026, 8, 2, 9, 0, tzinfo=ZoneInfo("America/Chihuahua"))
        self.assertEqual(
            resolve_weekly_bump_mode(when_odd, even_week="renew", odd_week="repost"),
            "repost",
        )

    def test_invalid_force(self):
        with self.assertRaises(ValueError):
            resolve_weekly_bump_mode(force_mode="bump")


class TestWeeklyBumpConfig(unittest.TestCase):
    def test_defaults_relist_first(self):
        cfg = weekly_bump_config({})
        self.assertEqual(cfg["even_week"], "repost")
        self.assertEqual(cfg["odd_week"], "repost")
        self.assertEqual(cfg["min_age_days"], 2.0)
        self.assertEqual(cfg["timezone"], "America/Chihuahua")
        self.assertEqual(cfg["max_per_account_per_run"], 25)

    def test_reads_nested(self):
        cfg = weekly_bump_config(
            {
                "sync": {
                    "weekly_bump": {
                        "even_week": "renew",
                        "odd_week": "repost",
                        "min_age_days": 1.5,
                        "timezone": "UTC",
                    }
                }
            }
        )
        self.assertEqual(cfg["even_week"], "renew")
        self.assertEqual(cfg["min_age_days"], 1.5)
        self.assertEqual(cfg["timezone"], "UTC")

    def test_min_age_falls_back_to_repost_section(self):
        cfg = weekly_bump_config({"sync": {"repost": {"min_age_days": 2}}})
        self.assertEqual(cfg["min_age_days"], 2.0)


class TestPlanRepostAgeFilter(unittest.TestCase):
    """Listings older than min age → repost action; fresher skipped."""

    def test_eligible_at_2_days_gets_repost(self):
        now = datetime.now(timezone.utc)
        live = [
            _listing(
                "obj_old",
                "account_1",
                posted_at=(now - timedelta(days=2, hours=1)).isoformat(),
            ),
            _listing(
                "obj_new",
                "account_1",
                posted_at=(now - timedelta(days=1)).isoformat(),
            ),
        ]
        vehicles = [_vehicle("obj_old"), _vehicle("obj_new")]
        actions, skipped = plan_repost_actions(
            vehicles,
            ["account_1"],
            live,
            all_eligible=True,
            older_than_days=2,
            max_per_account=10,
            is_on_hold=lambda *_: False,
            action_name="repost",
        )
        ids = {a.autosell_id for a in actions}
        self.assertEqual(ids, {"obj_old"})
        self.assertTrue(all(a.action == "repost" for a in actions))
        self.assertTrue(any("obj_new" in line and "min 2d" in line for line in skipped))

    def test_exactly_2_days_is_eligible(self):
        now = datetime.now(timezone.utc)
        live = [
            _listing(
                "obj_edge",
                "account_1",
                posted_at=(now - timedelta(days=2)).isoformat(),
            ),
        ]
        actions, skipped = plan_repost_actions(
            [_vehicle("obj_edge")],
            ["account_1"],
            live,
            all_eligible=True,
            older_than_days=DEFAULT_MIN_AGE_DAYS,
            max_per_account=5,
            is_on_hold=lambda *_: False,
            action_name="repost",
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action, "repost")
        self.assertEqual(skipped, [])

    def test_one_point_five_days_floor(self):
        now = datetime.now(timezone.utc)
        live = [
            _listing(
                "obj_ok",
                "account_1",
                posted_at=(now - timedelta(hours=40)).isoformat(),
            ),
            _listing(
                "obj_young",
                "account_1",
                posted_at=(now - timedelta(hours=24)).isoformat(),
            ),
        ]
        actions, skipped = plan_repost_actions(
            [_vehicle("obj_ok"), _vehicle("obj_young")],
            ["account_1"],
            live,
            all_eligible=True,
            older_than_days=1.5,
            max_per_account=10,
            is_on_hold=lambda *_: False,
        )
        self.assertEqual({a.autosell_id for a in actions}, {"obj_ok"})
        self.assertTrue(any("obj_young" in line and "min 1.5d" in line for line in skipped))

    def test_default_older_than_is_2_days(self):
        now = datetime.now(timezone.utc)
        live = [
            _listing(
                "obj_a",
                "account_1",
                posted_at=(now - timedelta(days=2, minutes=5)).isoformat(),
            ),
        ]
        actions, _ = plan_repost_actions(
            [_vehicle("obj_a")],
            ["account_1"],
            live,
            all_eligible=True,
            max_per_account=5,
            is_on_hold=lambda *_: False,
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action, "repost")

    def test_hold_skips_repost(self):
        now = datetime.now(timezone.utc)
        live = [
            _listing(
                "obj_hold",
                "account_1",
                posted_at=(now - timedelta(days=10)).isoformat(),
            ),
        ]
        actions, skipped = plan_repost_actions(
            [_vehicle("obj_hold")],
            ["account_1"],
            live,
            all_eligible=True,
            older_than_days=2,
            max_per_account=5,
            is_on_hold=lambda aid, acct: aid == "obj_hold",
            action_name="repost",
        )
        self.assertEqual(actions, [])
        self.assertTrue(any("hold" in line for line in skipped))

    def test_min_age_zero_includes_recent_posts(self):
        now = datetime.now(timezone.utc)
        live = [
            _listing(
                "obj_fresh",
                "account_1",
                posted_at=(now - timedelta(days=1)).isoformat(),
            ),
            _listing(
                "obj_week",
                "account_1",
                posted_at=(now - timedelta(days=6)).isoformat(),
            ),
        ]
        actions, skipped = plan_repost_actions(
            [_vehicle("obj_fresh"), _vehicle("obj_week")],
            ["account_1"],
            live,
            all_eligible=True,
            older_than_days=0,
            max_per_account=10,
            is_on_hold=lambda *_: False,
        )
        self.assertEqual({a.autosell_id for a in actions}, {"obj_fresh", "obj_week"})
        self.assertEqual(skipped, [])

    def test_force_skips_age_and_holds(self):
        now = datetime.now(timezone.utc)
        live = [
            _listing(
                "obj_hold",
                "account_1",
                posted_at=(now - timedelta(days=1)).isoformat(),
            ),
        ]
        actions, skipped = plan_repost_actions(
            [_vehicle("obj_hold")],
            ["account_1"],
            live,
            all_eligible=True,
            older_than_days=7,
            max_per_account=10,
            is_on_hold=lambda *_: True,
            force=True,
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].autosell_id, "obj_hold")
        self.assertEqual(skipped, [])

    def test_fifo_oldest_posted_at_first_within_cap(self):
        now = datetime.now(timezone.utc)
        live = [
            _listing("obj_mid", "account_1", posted_at=(now - timedelta(days=8)).isoformat()),
            _listing("obj_old", "account_1", posted_at=(now - timedelta(days=20)).isoformat()),
            _listing("obj_newer", "account_1", posted_at=(now - timedelta(days=4)).isoformat()),
        ]
        vehicles = [_vehicle("obj_mid"), _vehicle("obj_old"), _vehicle("obj_newer")]
        actions, skipped = plan_repost_actions(
            vehicles,
            ["account_1"],
            live,
            all_eligible=True,
            older_than_days=2,
            max_per_account=2,
            is_on_hold=lambda *_: False,
        )
        self.assertEqual([a.autosell_id for a in actions], ["obj_old", "obj_mid"])

    def test_force_still_respects_max_cap(self):
        now = datetime.now(timezone.utc)
        live = [
            _listing(f"obj_{i}", "account_1", posted_at=(now - timedelta(days=1)).isoformat())
            for i in range(5)
        ]
        vehicles = [_vehicle(f"obj_{i}") for i in range(5)]
        actions, skipped = plan_repost_actions(
            vehicles,
            ["account_1"],
            live,
            all_eligible=True,
            older_than_days=7,
            max_per_account=2,
            is_on_hold=lambda *_: False,
            force=True,
        )
        self.assertEqual(len(actions), 2)


class TestResolveMaxPerAccount(unittest.TestCase):
    def test_force_does_not_uncap(self):
        from src.sync.repost import resolve_max_per_account

        self.assertEqual(
            resolve_max_per_account(
                None,
                older_than_days=0,
                force=True,
                env_name="REPOST_MAX_PER_ACCOUNT_PER_RUN_UNSET_XYZ",
                config_default=25,
            ),
            25,
        )

    def test_unlimited_flag(self):
        from src.sync.repost import UNLIMITED_PER_ACCOUNT, resolve_max_per_account

        self.assertEqual(
            resolve_max_per_account(
                None,
                older_than_days=2,
                force=False,
                env_name="REPOST_MAX_PER_ACCOUNT_PER_RUN_UNSET_XYZ",
                config_default=25,
                unlimited=True,
            ),
            UNLIMITED_PER_ACCOUNT,
        )


class TestResolveMinAgeDays(unittest.TestCase):
    def test_cli_zero_wins_over_config(self):
        from src.sync.repost import resolve_min_age_days

        self.assertEqual(
            resolve_min_age_days("0", config_default=2, force=False),
            0.0,
        )

    def test_force_zeros_age(self):
        from src.sync.repost import resolve_min_age_days

        self.assertEqual(
            resolve_min_age_days("7", config_default=2, force=True),
            0.0,
        )

    def test_parses_fractional_days(self):
        from src.sync.repost import parse_older_than_days, resolve_min_age_days

        self.assertEqual(parse_older_than_days("1.5"), 1.5)
        self.assertEqual(parse_older_than_days("1.5d"), 1.5)
        self.assertEqual(
            resolve_min_age_days("1.5", config_default=2, force=False),
            1.5,
        )


class TestBuildBumpCommand(unittest.TestCase):
    def test_older_than_zero_forwards_min_age_days_0(self):
        import importlib.util

        path = ROOT / "scripts" / "run_weekly_bump.py"
        spec = importlib.util.spec_from_file_location("run_weekly_bump_cmd", path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cmd = mod.build_bump_command(
            script=Path("/tmp/run_repost.py"),
            older_than="0",
            max_per=25,
            python="python",
        )
        self.assertIn("--min-age-days", cmd)
        self.assertEqual(cmd[cmd.index("--min-age-days") + 1], "0")
        self.assertNotIn("--force", cmd)

    def test_force_forwards_force_flag(self):
        import importlib.util

        path = ROOT / "scripts" / "run_weekly_bump.py"
        spec = importlib.util.spec_from_file_location("run_weekly_bump_cmd", path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cmd = mod.build_bump_command(
            script=Path("/tmp/run_repost.py"),
            older_than="2",
            max_per=25,
            force=True,
            python="python",
        )
        self.assertIn("--force", cmd)
        self.assertEqual(cmd[cmd.index("--min-age-days") + 1], "2")


if __name__ == "__main__":
    unittest.main()
