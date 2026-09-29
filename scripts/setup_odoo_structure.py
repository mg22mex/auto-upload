#!/usr/bin/env python3
"""Validate/create Odoo CRM teams + users; write data/odoo_mapping.json.

Usage:
  PYTHONPATH=. python scripts/setup_odoo_structure.py
  PYTHONPATH=. python scripts/setup_odoo_structure.py --no-create
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from src.odoo_sync.structure import (  # noqa: E402
    DEFAULT_MAPPING_PATH,
    setup_odoo_structure,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-create",
        action="store_true",
        help="Only validate / map existing records (do not create teams/users)",
    )
    parser.add_argument(
        "--mapping",
        type=Path,
        default=ROOT / DEFAULT_MAPPING_PATH,
        help="Output mapping JSON path",
    )
    args = parser.parse_args()

    print("=== Odoo CRM structure setup ===", flush=True)
    result = setup_odoo_structure(
        create_missing=not args.no_create,
        mapping_path=args.mapping,
    )
    print(json.dumps(result.as_mapping(), indent=2, ensure_ascii=False))
    print(f"\nWrote {result.mapping_path}", flush=True)
    if result.warnings:
        print("Warnings:", flush=True)
        for w in result.warnings:
            print(f"  - {w}", flush=True)
    missing_closers = [
        r
        for entries in result.reps.values()
        for r in entries
        if r.role == "closer" and not r.odoo_id
    ]
    if missing_closers:
        print(
            f"WARN: {len(missing_closers)} closer(s) without odoo_id "
            f"(create may require Odoo seat): "
            + ", ".join(f"{r.branch}/{r.name}" for r in missing_closers),
            flush=True,
        )
    for branch, entries in result.reps.items():
        if not any(r.role == "closer" and r.odoo_id for r in entries):
            print(f"FAIL: no closer with odoo_id for {branch}", flush=True)
            return 2
    if not result.teams.get("periferico") or not result.teams.get("san_felipe"):
        print("FAIL: branch teams incomplete", flush=True)
        return 3
    print("OK structure ready", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
