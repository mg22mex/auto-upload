#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.store.db import SyncStore


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Manage repost holds (skip during run_repost / scheduled repost)."
    )
    parser.add_argument("command", choices=["add", "clear", "list"])
    parser.add_argument("autosell_id", nargs="?", help="e.g. obj1126 (add/clear)")
    parser.add_argument("--account", help="account_1, account_2, …")
    parser.add_argument("--until", dest="hold_until", help="Hold end date YYYY-MM-DD or ISO datetime")
    parser.add_argument("--reason", default="", help="e.g. fb_ads, manual_promo")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    config = load_config(ROOT / args.config)
    db_path = os.getenv("DB_PATH", "data/sync.db")
    store = SyncStore(ROOT / db_path if not Path(db_path).is_absolute() else db_path)

    if args.command == "list":
        rows = store.list_repost_holds(args.account)
        if not rows:
            print("No repost holds.")
            return 0
        for row in rows:
            until = row["hold_until"] or "indefinite"
            reason = row["reason"] or "-"
            print(f"{row['autosell_id']} @ {row['account_id']} until={until} reason={reason}")
        return 0

    if not args.autosell_id or not args.account:
        print("add/clear require autosell_id and --account", file=sys.stderr)
        return 1

    account_ids = [a["id"] for a in config.get("accounts", [])]
    if args.account not in account_ids:
        print(f"Unknown account: {args.account}", file=sys.stderr)
        return 1

    if args.command == "add":
        store.add_repost_hold(
            args.autosell_id,
            args.account,
            reason=args.reason,
            hold_until=args.hold_until,
        )
        until = args.hold_until or "indefinite"
        print(f"Hold added: {args.autosell_id} on {args.account} until={until}")
        return 0

    if store.clear_repost_hold(args.autosell_id, args.account):
        print(f"Hold cleared: {args.autosell_id} on {args.account}")
    else:
        print(f"No hold found: {args.autosell_id} on {args.account}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
