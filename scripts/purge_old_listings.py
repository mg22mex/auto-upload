#!/usr/bin/env python3
"""Delete Facebook Marketplace listings older than a day threshold.

Example:
  python scripts/purge_old_listings.py --max-days 3 --accounts account_1 account_2
  python scripts/purge_old_listings.py --max-days 3 --accounts account_1 --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.facebook.purge_old import purge_old_listings_on_page
from src.facebook.session import (
    format_session_login_error,
    get_page,
    is_logged_in,
    open_account_context,
    resolve_session_dir,
    session_health_report,
)
from src.facebook.util import ensure_unbuffered_stdio, env_bool
from src.store.db import SyncStore


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


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
        description=(
            "Scan Marketplace /you/selling and delete listings older than "
            "--max-days (default 3). Updates sync.db for deleted mappings."
        )
    )
    parser.add_argument(
        "--accounts",
        "--account",
        dest="accounts",
        nargs="+",
        help="Account ids (default: sync.active_accounts)",
    )
    parser.add_argument(
        "--max-days",
        type=float,
        default=3.0,
        help="Delete listings with age strictly greater than this many days (default: 3)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and report candidates only; do not delete",
    )
    parser.add_argument(
        "--max-deletes",
        type=int,
        default=None,
        help="Stop after N successful deletes per account (safety cap)",
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--db",
        default=None,
        help="Path to sync.db (default: DB_PATH or data/sync.db)",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    config = load_config(ROOT / args.config)
    account_ids = resolve_accounts(config, args.accounts)
    dry_run = bool(args.dry_run) or env_bool("DRY_RUN", False)
    headless = env_bool("FB_HEADLESS", bool(config.get("facebook", {}).get("headless", True)))

    db_path = args.db or os.getenv("DB_PATH", "data/sync.db")
    store = SyncStore(ROOT / db_path if not Path(db_path).is_absolute() else db_path)

    print("=== Purge old Marketplace listings ===", flush=True)
    print(f"Accounts:  {', '.join(account_ids)}", flush=True)
    print(f"Max days:  {args.max_days}  (delete when age > {args.max_days})", flush=True)
    print(f"Dry run:   {dry_run}", flush=True)
    print(f"DB:        {db_path}", flush=True)
    print("", flush=True)

    total_found = 0
    total_skipped = 0
    total_deleted = 0
    total_failed = 0
    total_dry = 0

    for account_id in account_ids:
        session_dir = resolve_session_dir(config, account_id, ROOT)
        health = session_health_report(session_dir)
        print(
            f"--- {account_id} session exists={health.get('exists')} "
            f"path={session_dir} ---",
            flush=True,
        )
        try:
            with open_account_context(
                config, account_id, root=ROOT, headless=headless
            ) as context:
                page = get_page(context)
                if not is_logged_in(page):
                    print(
                        format_session_login_error(account_id, session_dir),
                        file=sys.stderr,
                        flush=True,
                    )
                    total_failed += 1
                    continue
                stats = purge_old_listings_on_page(
                    page,
                    store,
                    account_id=account_id,
                    max_days=args.max_days,
                    dry_run=dry_run,
                    max_deletes=args.max_deletes,
                    delay_min_sec=2.0,
                    delay_max_sec=4.0,
                )
        except Exception as exc:
            print(f"ERROR {account_id}: {exc}", file=sys.stderr, flush=True)
            total_failed += 1
            continue

        total_found += stats.found
        total_skipped += stats.skipped_young + stats.skipped_unknown_age
        total_deleted += stats.deleted
        total_failed += stats.failed
        total_dry += stats.dry_run_candidates
        print(
            f"  [{account_id}] summary: found={stats.found} "
            f"skipped={stats.skipped_young + stats.skipped_unknown_age} "
            f"deleted={stats.deleted} failed={stats.failed}"
            + (f" dry_run_candidates={stats.dry_run_candidates}" if dry_run else ""),
            flush=True,
        )
        print("", flush=True)

    print("=== Done ===", flush=True)
    print(
        f"Total found={total_found} skipped={total_skipped} "
        f"deleted={total_deleted} failed={total_failed}"
        + (f" dry_run_candidates={total_dry}" if dry_run else ""),
        flush=True,
    )
    return 1 if total_failed and not total_deleted else 0


if __name__ == "__main__":
    raise SystemExit(main())
