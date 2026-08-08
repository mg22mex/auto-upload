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

from src.facebook.renewer import execute_renews
from src.facebook.util import env_int
from src.inventory.snapshot import load_catalog_snapshot
from src.store.db import SyncStore
from src.sync.repost import parse_older_than_days, plan_repost_actions


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


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
        description=(
            "Renew (Renovar) live Facebook listings — same item URL, bump placement. "
            "Uses FB native Renew, not delete+recreate."
        )
    )
    parser.add_argument("--account", nargs="+", help="Limit to these accounts")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ids", help="Comma-separated autosell ids, e.g. obj1137,obj969")
    group.add_argument(
        "--all-eligible",
        action="store_true",
        help="Renew live catalog listings (respects holds and age filter)",
    )
    parser.add_argument(
        "--older-than",
        default=None,
        help="Min days since last post/renew for --all-eligible (default: 3)",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=None,
        help="Max renews per account (default: RENEW_MAX or config)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore repost holds (admin)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Plan only")
    parser.add_argument("--catalog", default="data/catalog_latest.json")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    config = load_config(ROOT / args.config)
    renew_cfg = config.get("sync", {}).get("renew", {})
    repost_cfg = config.get("sync", {}).get("repost", {})

    account_ids = resolve_accounts(config, args.account)
    older_than = parse_older_than_days(
        args.older_than
        or os.getenv("RENEW_MIN_AGE_DAYS")
        or str(renew_cfg.get("min_age_days", repost_cfg.get("min_age_days", 3)))
    )
    max_per = args.max
    if max_per is None:
        max_per = env_int(
            "RENEW_MAX_PER_ACCOUNT_PER_RUN",
            int(
                renew_cfg.get(
                    "max_per_account_per_run",
                    repost_cfg.get("max_per_account_per_run", 25),
                )
            ),
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
        action_name="renew",
    )

    print("")
    print("=== Facebook renew (Renovar) ===")
    print(f"Accounts:    {', '.join(account_ids)}")
    print(f"Would renew: {len(actions)}")
    print(f"Skipped:     {len(skipped)}")
    print("")

    if actions:
        print("Renews:")
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
        return 0

    result = execute_renews(actions, store, config, root=ROOT, account_order=account_ids)
    print(f"Renew done: {result.renews} renewed, {len(result.errors)} error(s).")
    if result.errors:
        for err in result.errors:
            print(f"  FB error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
