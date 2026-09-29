#!/usr/bin/env python3
"""Validate/create Odoo CRM teams + users; write data/odoo_mapping.json.

Usage:
  PYTHONPATH=. python scripts/setup_odoo_structure.py
  PYTHONPATH=. python scripts/setup_odoo_structure.py --no-create
  PYTHONPATH=. python scripts/setup_odoo_structure.py --sync-employees
  PYTHONPATH=. python scripts/setup_odoo_structure.py --employees-only
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
    sync_hr_employees,
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
    parser.add_argument(
        "--sync-employees",
        action="store_true",
        help="Also ensure hr.employee for each closer in the mapping",
    )
    parser.add_argument(
        "--employees-only",
        action="store_true",
        help="Skip team/user setup; only sync hr.employee from existing mapping",
    )
    parser.add_argument(
        "--include-setter",
        action="store_true",
        help="Also create hr.employee for Marco (Appointment Setter)",
    )
    args = parser.parse_args()

    if not args.employees_only:
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

    if args.sync_employees or args.employees_only:
        print("\n=== hr.employee sync ===", flush=True)
        emp = sync_hr_employees(
            mapping_path=args.mapping,
            create_missing=not args.no_create,
            include_setter=args.include_setter,
        )
        print(json.dumps(emp.as_dict(), indent=2, ensure_ascii=False))
        print(
            f"\nEmployees: created={emp.created} existing={emp.existing} "
            f"errors={emp.errors}",
            flush=True,
        )
        for row in emp.employees:
            status = (
                "CREATED"
                if row.created
                else ("EXISTS" if row.employee_id else "ERROR")
            )
            print(
                f"  [{status}] user_id={row.user_id} employee_id={row.employee_id} "
                f"name={row.name!r} email={row.work_email!r}"
                + (f" err={row.error}" if row.error else ""),
                flush=True,
            )
        if emp.errors and not emp.created and not emp.existing:
            return 4

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
