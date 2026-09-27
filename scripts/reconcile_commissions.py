#!/usr/bin/env python3
"""Reconcile Odoo MG Quote Lead → local commissions ledger.

Usage:
  PYTHONPATH=. python scripts/reconcile_commissions.py
  PYTHONPATH=. python scripts/reconcile_commissions.py --limit 100 --month 2026-09
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument(
        "--month",
        default="",
        help="YYYY-MM for summary (default: current UTC month)",
    )
    parser.add_argument(
        "--db",
        default="",
        help="Override COMMISSIONS_DB_PATH",
    )
    args = parser.parse_args()

    if args.db:
        import os

        os.environ["COMMISSIONS_DB_PATH"] = args.db

    from src.attribution import (
        commissions_db_path,
        monthly_summary,
        reconcile_commissions_from_odoo,
    )

    print(f"commissions db: {commissions_db_path()}", flush=True)
    result = reconcile_commissions_from_odoo(limit=args.limit)
    if args.month:
        result["summary"] = monthly_summary(args.month).to_dict()
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
