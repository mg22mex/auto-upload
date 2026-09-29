#!/usr/bin/env python3
"""Simulate a booked appointment → partner + lead + calendar; print Odoo IDs.

Usage:
  PYTHONPATH=. python scripts/test_odoo_crm_sync.py
  PYTHONPATH=. python scripts/test_odoo_crm_sync.py --dry-run
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

from src.odoo_sync.appointment_sync import sync_booked_appointment  # noqa: E402
from src.odoo_sync.structure import setup_odoo_structure  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="Marco Gastelum")
    parser.add_argument("--phone", default="5216141754852")
    parser.add_argument("--vehicle", default="2022 Toyota Corolla XLE *")
    parser.add_argument("--branch", default="periferico")
    parser.add_argument("--when", default="hoy a las 5:30 pm")
    parser.add_argument("--price", type=float, default=365000.0)
    parser.add_argument(
        "--skip-setup",
        action="store_true",
        help="Do not refresh data/odoo_mapping.json first",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan only (no Odoo writes)",
    )
    args = parser.parse_args()

    if not args.skip_setup:
        print("=== refresh Odoo structure mapping ===", flush=True)
        setup = setup_odoo_structure(create_missing=True)
        print(
            json.dumps(
                {
                    "teams": setup.teams,
                    "setter_user_id": setup.setter.odoo_id if setup.setter else None,
                    "mapping": setup.mapping_path,
                },
                indent=2,
                ensure_ascii=False,
            )
        )

    print("\n=== sync booked appointment ===", flush=True)
    result = sync_booked_appointment(
        customer_name=args.name,
        phone=args.phone,
        vehicle=args.vehicle,
        branch=args.branch,
        when_text=args.when,
        vehicle_price=args.price,
        dry_run=args.dry_run,
    )
    print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
    if result.error:
        print(f"FAIL {result.error}", flush=True)
        return 1
    print(
        f"\nOK partner_id={result.partner_id} lead_id={result.lead_id} "
        f"event_id={result.event_id} closer={result.closer_user_id} "
        f"setter={result.setter_user_id}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
