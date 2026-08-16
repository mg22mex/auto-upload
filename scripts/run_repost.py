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

from src.facebook.executor import execute_actions
from src.facebook.reposter import execute_reposts
from src.facebook.session import format_session_login_error, resolve_session_dir, session_health_report
from src.facebook.util import ensure_unbuffered_stdio, env_int
from src.inventory.snapshot import load_catalog_snapshot
from src.store.db import SyncStore
from src.sync.engine import plan_sync_actions, split_executable_actions
from src.sync.allocator import allocate_from_config, slot_allocator_config
from src.sync.repost import (
    resolve_max_per_account,
    resolve_min_age_days,
    plan_repost_actions,
)


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
    ensure_unbuffered_stdio()
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
        "--min-age-days",
        dest="older_than",
        default=None,
        help=(
            "Min days since last post for --all-eligible "
            "(default: 3 or config). Pass 0 to include listings posted in the last 1–7 days."
        ),
    )
    parser.add_argument(
        "--max",
        "--max-per-account",
        dest="max",
        type=int,
        default=None,
        help="Max reposts per account this run (default: 15). --force does not lift this cap.",
    )
    parser.add_argument(
        "--unlimited",
        action="store_true",
        help="Ignore per-account cap (full shelf). Prefer daily --max-per-account 15.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore holds and min-age; still respects --max-per-account unless --unlimited",
    )
    parser.add_argument("--dry-run", action="store_true", help="Plan only; do not touch Facebook")
    parser.add_argument("--catalog", default="data/catalog_latest.json")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    config = load_config(ROOT / args.config)
    repost_cfg = config.get("sync", {}).get("repost", {})

    account_ids = resolve_accounts(config, args.account)
    older_than = resolve_min_age_days(
        args.older_than,
        env_names=("REPOST_MIN_AGE_DAYS",),
        config_default=int(repost_cfg.get("min_age_days", 3)),
        force=args.force,
    )
    max_per = resolve_max_per_account(
        args.max,
        older_than_days=older_than,
        force=args.force,
        env_name="REPOST_MAX_PER_ACCOUNT_PER_RUN",
        config_default=int(repost_cfg.get("max_per_account_per_run", 15)),
        unlimited=args.unlimited,
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

    live_listings = store.get_live_listings()
    allocation = allocate_from_config(config, vehicles, account_ids, live_listings)
    alloc_cfg = slot_allocator_config(config)
    assigned_by_account = None
    if allocation:
        assigned_by_account = {
            acct: allocation.assigned_ids(acct) for acct in account_ids
        }
        print("=== Slot allocation ===", flush=True)
        print(allocation.format_table(), flush=True)
        print("", flush=True)

    create_actions = []
    deferred_creates = []
    if args.all_eligible:
        max_creates = env_int(
            "MAX_POSTS_PER_ACCOUNT_PER_RUN",
            int(config.get("sync", {}).get("max_posts_per_account_per_run", 10)),
        )
        sync_planned = plan_sync_actions(
            vehicles,
            account_ids,
            live_listings,
            max_creates_per_account=max_creates,
            allocation=allocation,
            enforce_overflow_removals=alloc_cfg["enforce_overflow_removals"],
        )
        executable_sync, deferred_creates = split_executable_actions(sync_planned)
        create_actions = [a for a in executable_sync if a.action == "create"]

    actions, skipped = plan_repost_actions(
        vehicles,
        account_ids,
        live_listings,
        explicit_ids=explicit_ids,
        all_eligible=args.all_eligible,
        older_than_days=older_than,
        max_per_account=max_per,
        is_on_hold=store.is_on_repost_hold,
        force=args.force,
        assigned_by_account=assigned_by_account,
    )

    print("")
    print("=== Facebook repost ===")
    print(f"Accounts:     {', '.join(account_ids)}")
    print(f"Force:        {args.force}")
    print(f"Min age days: {older_than}  (--force or 0 = all live dates)")
    print(f"Max / account: {max_per}")
    print(f"New creates:  {len(create_actions)} (catalog missing from sync.db)")
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

    if create_actions:
        print("New listings (catalog → Facebook create):")
        for action in create_actions[:20]:
            title = action.vehicle.marketplace_title if action.vehicle else action.slug
            print(f"  - [{action.account_id}] {title} ({action.autosell_id})")
        if len(create_actions) > 20:
            print(f"  ... and {len(create_actions) - 20} more")
        print("")
    if deferred_creates:
        print(f"Deferred creates (daily cap): {len(deferred_creates)}")
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

    if args.dry_run or (not actions and not create_actions):
        if session_warns and not args.dry_run and not actions and not create_actions:
            return 0
        return 0

    rc = 0
    if create_actions:
        print("Creating new catalog vehicles missing from sync.db ...")
        create_result = execute_actions(
            create_actions, store, config, root=ROOT, account_order=account_ids
        )
        print(
            f"Create done: {create_result.creates} posted, "
            f"{len(create_result.errors)} error(s)."
        )
        if create_result.errors:
            rc = 1
            for err in create_result.errors:
                print(f"  FB create error: {err}", file=sys.stderr)

    if actions:
        result = execute_reposts(actions, store, config, root=ROOT, account_order=account_ids)
        print(f"Repost done: {result.reposts} reposted, {len(result.errors)} error(s).")
        if result.errors:
            hard = [e for e in result.errors if "DEFERRED_FAILED" not in e]
            for err in result.errors:
                print(f"  FB error: {err}", file=sys.stderr)
                if "Not logged in" in err or "session expired" in err.lower():
                    print(
                        "  → Fix: on fb-worker run "
                        "`python scripts/fb_login.py --account <id>` (headed) "
                        "and confirm sessions/ lives under the persistent data bind.",
                        file=sys.stderr,
                    )
            if hard:
                rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
