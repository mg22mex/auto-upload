#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.facebook.reposter import execute_reposts
from src.facebook.session import format_session_login_error, resolve_session_dir, session_health_report
from src.facebook.util import env_int
from src.inventory.snapshot import load_catalog_snapshot
from src.store.db import SyncStore
from src.sync.repost import parse_older_than_days, plan_repost_actions


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def resolve_accounts(config: dict, requested: list[str] | None) -> list[str]:
    all_ids = [a["id"] for a in config.get("accounts", [])]
    if requested:
        unknown = set(requested) - set(all_ids)
        if unknown:
            raise SystemExit(f"Unknown account(s): {', '.join(sorted(unknown))}")
        return requested
    active = config.get("sync", {}).get("active_accounts")
    return list(active) if active else all_ids


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Repost live Facebook listings (mark sold → create → update sync.db URL)."
    )
    parser.add_argument("--account", nargs="+", help="Limit to these accounts")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--ids",
        help="Comma-separated autosell ids, e.g. obj1126,obj969",
    )
    group.add_argument(
        "--all-eligible",
        action="store_true",
        help="Repost live catalog listings (respects holds and age filter)",
    )
    parser.add_argument(
        "--older-than",
        default=None,
        help="Min days since last post for --all-eligible (default: 3 or config)",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=None,
        help="Max reposts per account this run (default: REPOST_MAX_PER_ACCOUNT or 5)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore repost holds (admin; use with --ids)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Plan only; do not touch Facebook")
    parser.add_argument("--catalog", default="data/catalog_latest.json")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    config = load_config(ROOT / args.config)
    repost_cfg = config.get("sync", {}).get("repost", {})

    account_ids = resolve_accounts(config, args.account)
    older_than = parse_older_than_days(
        args.older_than
        or os.getenv("REPOST_MIN_AGE_DAYS")
        or str(repost_cfg.get("min_age_days", 3))
    )
    max_per = args.max
    if max_per is None:
        max_per = env_int(
            "REPOST_MAX_PER_ACCOUNT_PER_RUN",
            int(repost_cfg.get("max_per_account_per_run", 5)),
        )

    catalog_path = ROOT / args.catalog
    if not catalog_path.is_file():
        print(f"Catalog not found: {catalog_path}", file=sys.stderr)
        return 1

    vehicles = load_catalog_snapshot(catalog_path)
    db_path = os.getenv("DB_PATH", "data/sync.db")
    store = SyncStore(ROOT / db_path if not Path(db_path).is_absolute() else db_path)

    explicit_ids = None
    if args.ids:
        explicit_ids = {part.strip() for part in args.ids.split(",") if part.strip()}

    actions, skipped = plan_repost_actions(
        vehicles,
        account_ids,
        store.get_live_listings(),
        explicit_ids=explicit_ids,
        all_eligible=args.all_eligible,
        older_than_days=older_than,
        max_per_account=max_per,
        is_on_hold=store.is_on_repost_hold,
        force=args.force,
    )

    print("")
    print("=== Facebook repost ===")
    print(f"Accounts:     {', '.join(account_ids)}")
    print(f"Would repost: {len(actions)}")
    print(f"Skipped:      {len(skipped)}")
    print("")

    accounts_by_id = {
        a["id"]: a for a in config.get("accounts", []) if isinstance(a, dict) and a.get("id")
    }
    for account_id in account_ids:
        acc = accounts_by_id.get(account_id, {})
        label = (
            acc.get("facebook_profile")
            or acc.get("facebook_c_user")
            or acc.get("label")
            or "unknown"
        )
        print(f"Processing reposts for {account_id} (Facebook Profile: {label})")
    print("")

    # Surface session problems early (before long Playwright loop)
    print("Session health (Chromium profiles under sessions/):")
    session_warns: list[str] = []
    for account_id in account_ids:
        sdir = resolve_session_dir(config, account_id, ROOT)
        health = session_health_report(sdir)
        flag = "OK" if not health["looks_empty"] else "PROBLEM"
        print(
            f"  [{flag}] {account_id}: exists={health['exists']} "
            f"files={health['file_count']} cookies_file={health['has_cookies_file']} "
            f"path={health['path']}"
        )
        if health["looks_empty"]:
            session_warns.append(format_session_login_error(account_id, sdir))
    if session_warns:
        print("")
        print("WARNING: one or more accounts need a headed login before repost:", file=sys.stderr)
        for msg in session_warns:
            print(f"  {msg}", file=sys.stderr)
        print("", file=sys.stderr)
    print("")

    if actions:
        print("Reposts:")
        for action in actions:
            title = action.vehicle.marketplace_title if action.vehicle else action.slug
            print(f"  - [{action.account_id}] {title} ({action.autosell_id})")
        print("")

    if skipped:
        print("Skipped:")
        for line in skipped[:25]:
            print(f"  - {line}")
        if len(skipped) > 25:
            print(f"  ... and {len(skipped) - 25} more")
        print("")

    if args.dry_run or not actions:
        if session_warns and not args.dry_run and not actions:
            return 0
        return 0

    result = execute_reposts(actions, store, config, root=ROOT, account_order=account_ids)
    print(f"Repost done: {result.reposts} reposted, {len(result.errors)} error(s).")
    if result.errors:
        for err in result.errors:
            print(f"  FB error: {err}", file=sys.stderr)
            if "Not logged in" in err or "session expired" in err.lower():
                print(
                    "  → Fix: on fb-worker run "
                    "`python scripts/fb_login.py --account <id>` (headed) "
                    "and confirm sessions/ lives under the persistent data bind.",
                    file=sys.stderr,
                )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
