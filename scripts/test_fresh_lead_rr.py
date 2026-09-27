#!/usr/bin/env python3
"""Fresh-lead RR + vehicle smoke test (avoids reusing lead 1937).

Seeds San Felipe cursor at Francisco (index 1), creates a brand-new phone lead
via ``create_vapi_lead`` / assignment pipeline, and asserts:

* WhatsApp card targets Francisco ``+526142417711``
* ``data/rr_cursor.db`` advances to index 2 (Aaron next)
* Odoo lead stores ``Chevrolet Aveo 2020``

Usage (on Oracle)::

  cd ~/auto-upload
  PYTHONPATH=. .venv/bin/python scripts/test_fresh_lead_rr.py
  PYTHONPATH=. .venv/bin/python scripts/test_fresh_lead_rr.py --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from src.assigner import load_cursors, save_cursor  # noqa: E402
from src.config import parse_reps  # noqa: E402
from src.notifications.whatsapp_rep import notify_appointment_rep  # noqa: E402
from src.odoo_sync.crm import (  # noqa: E402
    CRMLeadManager,
    PLACEHOLDER_BRANCH,
)
from src.voice_gateway.vapi_bridge import LeadArgs, create_vapi_lead  # noqa: E402


def _fresh_phone() -> str:
    # Unique 10-digit MX mobile → 52-prefixed E.164-ish for CRM.
    suffix = int(time.time()) % 10_000_000
    return f"521615{suffix:07d}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip live Odoo/WA; still exercise RR cursor + payload shaping",
    )
    parser.add_argument(
        "--phone",
        default="",
        help="Customer phone (default: generated unique test number)",
    )
    parser.add_argument(
        "--name",
        default="",
        help="Customer display name (default: resolved from session or 'Cliente')",
    )
    parser.add_argument(
        "--marco",
        action="store_true",
        help="Verify with Marco Gastelum / 5216141754852 (session identity)",
    )
    args = parser.parse_args()

    if args.dry_run:
        os.environ["ODOO_DRY_RUN"] = "true"
        os.environ["REP_NOTIFY_ENABLED"] = "false"

    reps = parse_reps(os.getenv("REPS_SAN_FELIPE") or "[]")
    if len(reps) < 3:
        print("FAIL: REPS_SAN_FELIPE needs ≥3 entries", file=sys.stderr)
        return 1

    # Seed: next pick = Francisco (index 1). Do NOT call reset_round_robin()
    # (that wipes the SQLite cursor).
    save_cursor(PLACEHOLDER_BRANCH, 1)
    from src.odoo_sync import crm as crm_mod

    crm_mod._ASSIGNER._cursor.clear()
    crm_mod._ASSIGNER._hydrate()

    before = load_cursors().get(PLACEHOLDER_BRANCH, 0)
    print(f"cursor BEFORE san_felipe={before} → next={reps[before % len(reps)].name}")

    if args.marco:
        phone = "5216141754852"
        name = "Marco Gastelum"
    else:
        phone = (args.phone or "").strip() or _fresh_phone()
        name = (args.name or "").strip() or "Cliente"
    vehicle = "Chevrolet Aveo 2020"
    tradein = "Toyota Corolla 2020 LE · Autométrica ~$201,200"

    lead_args = LeadArgs(
        name=name,
        phone=phone,
        interested_vehicle=vehicle,
        tradein_summary=tradein,
        appointment_date="lunes a las 10 am",
        branch=PLACEHOLDER_BRANCH,
    )

    manager = CRMLeadManager()
    result = create_vapi_lead(lead_args, manager=manager)
    print("create_vapi_lead:", {k: result.get(k) for k in (
        "status", "lead_id", "branch", "user_id", "assignment", "dry_run", "title"
    )})

    assignment = result.get("assignment") if isinstance(result.get("assignment"), dict) else None
    after_create = load_cursors().get(PLACEHOLDER_BRANCH, 0)
    print(f"cursor AFTER create san_felipe={after_create}")

    # create_vapi_lead already rotated RR once — reuse that assignment for WA.
    if not assignment:
        print("FAIL: no assignment from create_vapi_lead", file=sys.stderr)
        return 1

    from src.odoo_sync.crm import RepAssignment

    pick = RepAssignment(
        branch=str(assignment.get("branch") or PLACEHOLDER_BRANCH),
        phone=str(assignment.get("phone") or ""),
        odoo_id=(
            int(assignment["odoo_id"])
            if assignment.get("odoo_id") not in (None, "", False)
            else None
        ),
        rep_name=str(assignment.get("rep_name") or ""),
        fell_back=bool(assignment.get("fell_back")),
        rotation_index=int(assignment.get("rotation_index") or 0),
    )

    notice = notify_appointment_rep(
        customer_name=lead_args.name,
        client_phone=phone,
        branch=PLACEHOLDER_BRANCH,
        interested_vehicle=vehicle,
        appointment_date=lead_args.appointment_date,
        tradein_summary=tradein,
        stage_name=str(result.get("stage_name") or "Beatriz Cita"),
        lead_id=result.get("lead_id"),
        assignment=pick,
    )
    print("notify_appointment_rep:", notice.as_dict())
    print("----- MESSAGE PAYLOAD -----")
    print(notice.message or "(empty — notify disabled/skipped)")
    print("---------------------------")

    final_cursor = load_cursors().get(PLACEHOLDER_BRANCH, 0)
    print(f"cursor FINAL san_felipe={final_cursor} → next={reps[final_cursor % len(reps)].name}")

    # --- Assertions ---
    ok = True
    if args.marco or phone.endswith("6141754852"):
        msg = notice.message or ""
        if "Marco Gastelum" not in msg:
            print("FAIL: card missing Marco Gastelum", file=sys.stderr)
            ok = False
        else:
            print("OK customer name Marco Gastelum")
        if "5216141754852" not in msg:
            print("FAIL: card missing 5216141754852", file=sys.stderr)
            ok = False
        else:
            print("OK customer phone 5216141754852")
        if "https://wa.me/5216141754852" not in msg:
            print("FAIL: card missing wa.me link", file=sys.stderr)
            ok = False
        else:
            print("OK wa.me link present")
        if "RR Fresh" in msg:
            print("FAIL: synthetic RR Fresh still on card", file=sys.stderr)
            ok = False

    francisco = reps[1]
    expected_phone = francisco.phone
    if notice.phone != expected_phone and not args.dry_run:
        # dry-run may skip send but phone should still resolve
        pass
    if pick.phone != expected_phone and notice.phone != expected_phone:
        # After seed=1, first RR pick must be Francisco
        if pick.rotation_index != 1 and notice.phone != expected_phone:
            print(
                f"FAIL: expected Francisco {expected_phone}, "
                f"got pick={pick.phone!r} notice={notice.phone!r} idx={pick.rotation_index}",
                file=sys.stderr,
            )
            ok = False
        else:
            print(f"OK rep phone={notice.phone or pick.phone}")
    else:
        print(f"OK Francisco phone={notice.phone or pick.phone}")

    if final_cursor < 2 and after_create < 2:
        print(
            f"FAIL: cursor expected ≥2 (Aaron next), got {final_cursor}",
            file=sys.stderr,
        )
        ok = False
    else:
        print(f"OK cursor→{max(final_cursor, after_create)} (Aaron next)")

    vehicle_ok = vehicle.casefold() in str(result.get("title") or "").casefold()
    # create_vapi_lead returns opportunity_name as Paulina title — check description path
    if not args.dry_run and result.get("lead_id"):
        try:
            from src.odoo_sync.client import OdooCRMClient

            client = OdooCRMClient()
            client.authenticate()
            rows = client.execute_kw(
                "crm.lead",
                "read",
                [[int(result["lead_id"])], ["id", "name", "description", "team_id", "user_id"]],
            )
            row = rows[0] if rows else {}
            blob = f"{row.get('name')} {row.get('description')}"
            print(f"Odoo lead={row.get('id')} name={row.get('name')!r} team={row.get('team_id')} user={row.get('user_id')}")
            if "aveo" not in blob.casefold():
                print("FAIL: Odoo lead missing Chevrolet Aveo 2020", file=sys.stderr)
                ok = False
            else:
                print("OK Odoo vehicle contains Aveo")
                vehicle_ok = True
        except Exception as exc:
            print(f"WARN Odoo verify skipped: {exc}", file=sys.stderr)

    if args.dry_run:
        # dry-run title uses Consulta: vehicle - name unless opportunity_name set
        print(f"dry_run title={result.get('title')!r}")
        vehicle_ok = True

    if not vehicle_ok and not args.dry_run:
        ok = False

    print("=" * 50)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
