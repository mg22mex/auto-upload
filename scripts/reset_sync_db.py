#!/usr/bin/env python3
"""Reset Marketplace sync.db mapping state for a fresh sync.

Clears ``fb_listings``, ``repost_holds``, and optionally ``sync_runs``.
Does **not** delete ``catalog_snapshots`` (needed for planning).

Examples:
  # Dry-run (show counts)
  python scripts/reset_sync_db.py --dry-run

  # Wipe all FB mappings / holds (keep catalog)
  python scripts/reset_sync_db.py --yes

  # Wipe everything except one preserved autosell mapping (if present)
  python scripts/reset_sync_db.py --yes --keep-ids obj1173
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=None,
        help="Path to sync.db (default: DB_PATH or data/sync.db)",
    )
    parser.add_argument(
        "--keep-ids",
        default="",
        help="Comma-separated autosell ids to preserve in fb_listings",
    )
    parser.add_argument(
        "--clear-runs",
        action="store_true",
        help="Also DELETE FROM sync_runs",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print row counts only; do not modify",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required to apply destructive changes",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    db_path = Path(args.db or os.getenv("DB_PATH", "data/sync.db"))
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    if not db_path.is_file():
        print(f"ERROR: database not found: {db_path}", file=sys.stderr)
        return 1

    keep = {part.strip() for part in args.keep_ids.split(",") if part.strip()}

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    counts = {
        "fb_listings": con.execute("SELECT COUNT(*) FROM fb_listings").fetchone()[0],
        "repost_holds": con.execute("SELECT COUNT(*) FROM repost_holds").fetchone()[0],
        "sync_runs": con.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0],
        "catalog_snapshots": con.execute(
            "SELECT COUNT(*) FROM catalog_snapshots"
        ).fetchone()[0],
    }
    print(f"DB: {db_path}")
    print(f"Counts: {counts}")
    if keep:
        print(f"Keep autosell ids: {', '.join(sorted(keep))}")
        for autosell_id in sorted(keep):
            rows = con.execute(
                "SELECT autosell_id, account_id, status, fb_listing_url "
                "FROM fb_listings WHERE autosell_id = ?",
                (autosell_id,),
            ).fetchall()
            if not rows:
                print(f"  (no fb_listings row for {autosell_id})")
            for row in rows:
                print(f"  keep {dict(row)}")

    if args.dry_run:
        print("Dry-run only — no changes.")
        con.close()
        return 0

    if not args.yes:
        print("Refusing to modify without --yes (or use --dry-run).", file=sys.stderr)
        con.close()
        return 2

    if keep:
        placeholders = ",".join("?" for _ in keep)
        con.execute(
            f"DELETE FROM fb_listings WHERE autosell_id NOT IN ({placeholders})",
            tuple(keep),
        )
        con.execute(
            f"DELETE FROM repost_holds WHERE autosell_id NOT IN ({placeholders})",
            tuple(keep),
        )
        # Clear holds on kept ids too (fresh sync), but keep listing row.
        con.execute(
            f"DELETE FROM repost_holds WHERE autosell_id IN ({placeholders})",
            tuple(keep),
        )
    else:
        con.execute("DELETE FROM fb_listings")
        con.execute("DELETE FROM repost_holds")

    if args.clear_runs:
        con.execute("DELETE FROM sync_runs")

    con.commit()
    after = {
        "fb_listings": con.execute("SELECT COUNT(*) FROM fb_listings").fetchone()[0],
        "repost_holds": con.execute("SELECT COUNT(*) FROM repost_holds").fetchone()[0],
        "sync_runs": con.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0],
        "catalog_snapshots": con.execute(
            "SELECT COUNT(*) FROM catalog_snapshots"
        ).fetchone()[0],
    }
    con.close()
    print(f"After: {after}")
    print("Reset complete (catalog_snapshots preserved).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
