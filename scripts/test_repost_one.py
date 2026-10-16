#!/usr/bin/env python3
"""Single-SKU Facebook repost smoke test (hardened delete→verify→create pipeline).

Runs production ``scripts/run_repost.py`` for exactly one autosell id. Phase
markers to watch in the log:

  PROFILE_OK <account>: c_user=… matches config
  CACHE_BUILD / DELETE_START … photos_ready=N
  DELETE_OK → SHELF_CHECK_OK → HARD_DELETE_VERIFY_OK
  CREATE_START … skip_shelf_gate=1 delete_verified=1 → CREATE_OK

Examples (operator / fb-worker — not from the IDE agent browser):

  # Plan only
  python scripts/test_repost_one.py --id obj1126 --account account_1 --dry-run

  # Live one-vehicle test (headed Chromium)
  PYTHONUNBUFFERED=1 python scripts/test_repost_one.py \\
    --id obj1126 --account account_1 --force --headed

Equivalent without this wrapper:

  PYTHONUNBUFFERED=1 FB_HEADLESS=0 python scripts/run_repost.py \\
    --ids obj1126 --account account_1 --force --max 1
"""
from __future__ import annotations

import argparse
import os
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PHASE_MARKERS = """
Watch for these log lines (in order):
  1. PROFILE_OK <account>: c_user=… matches config
     — or wrong Facebook profile / FAILED_SESSION_EXPIRED (abort before CACHE)
  2. CACHE_BUILD <id> … / DELETE_START … photos_ready=N  (prefetch before delete)
  3. DELETE_OK  (DELETE_FAIL → SKIP_CREATE, no create)
  4. SHELF_CHECK_OK
  5. HARD_DELETE_VERIFY_OK  (HARD_DELETE_VERIFY_FAIL → SKIP_CREATE)
  6. CREATE_START … delete_verified=1 → CREATE_OK
"""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Repost exactly one vehicle through the hardened pipeline."
    )
    parser.add_argument(
        "--id",
        dest="id",
        required=True,
        help="Autosell id / SKU (e.g. obj1126)",
    )
    parser.add_argument(
        "--account",
        required=True,
        help="Account id from config.yaml (e.g. account_1)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore holds / min-age / already-bumped skip (recommended for tests)",
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--catalog", default="data/catalog_latest.json")
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Visible Chromium (sets FB_HEADLESS=0 for this process)",
    )
    args = parser.parse_args()

    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    if args.headed:
        os.environ["FB_HEADLESS"] = "0"
    if args.force:
        os.environ.setdefault("REPOST_SKIP_IF_BUMPED_HOURS", "0")

    sku = args.id.strip()
    if not sku or "," in sku:
        print(
            "ERROR: pass a single --id (use scripts/run_repost.py for batches)",
            file=sys.stderr,
        )
        return 2

    print("=== test_repost_one ===", flush=True)
    print(f"SKU:      {sku}", flush=True)
    print(f"Account:  {args.account}", flush=True)
    print(f"Force:    {args.force}", flush=True)
    print(f"Dry-run:  {args.dry_run}", flush=True)
    print(f"Headless: {os.getenv('FB_HEADLESS', '(config default)')}", flush=True)
    print(PHASE_MARKERS, flush=True)

    argv = [
        str(ROOT / "scripts" / "run_repost.py"),
        "--ids",
        sku,
        "--account",
        args.account,
        "--max",
        "1",
        "--config",
        args.config,
        "--catalog",
        args.catalog,
    ]
    if args.dry_run:
        argv.append("--dry-run")
    if args.force:
        argv.append("--force")

    print(f"$ python {' '.join(argv)}", flush=True)
    print("", flush=True)

    sys.argv = argv
    runpy.run_path(str(ROOT / "scripts" / "run_repost.py"), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
