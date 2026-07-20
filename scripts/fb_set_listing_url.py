#!/usr/bin/env python3
"""Update sync.db fb_listing_url after an extension repost (new item ID)."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.store.db import SyncStore


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Set Facebook listing URL in sync.db (e.g. after Chrome extension repost)."
    )
    parser.add_argument("--account", required=True)
    parser.add_argument("--autosell-id", required=True)
    parser.add_argument("--url", required=True, help="New marketplace item URL")
    parser.add_argument(
        "--keep-posted-at",
        action="store_true",
        help="Do not reset posted_at (default: reset so age filters treat as fresh)",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    db_path = os.getenv("DB_PATH", "data/sync.db")
    store = SyncStore(ROOT / db_path if not Path(db_path).is_absolute() else db_path)

    row = store.get_fb_listing(args.autosell_id, args.account)
    if row is None:
        print(f"No row for {args.autosell_id} on {args.account}", file=sys.stderr)
        return 1

    old = row["fb_listing_url"]
    ok = store.update_fb_listing_url(
        args.autosell_id,
        args.account,
        fb_listing_url=args.url.strip(),
        reset_posted_at=not args.keep_posted_at,
    )
    if not ok:
        print("Update failed", file=sys.stderr)
        return 1
    print(f"Updated {args.autosell_id} on {args.account}")
    print(f"  old: {old}")
    print(f"  new: {args.url.strip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
