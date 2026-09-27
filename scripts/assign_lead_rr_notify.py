#!/usr/bin/env python3
"""Retroactively RR-assign a CRM lead and WhatsApp the assigned rep."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from src.config import PRIMARY_BRANCH, normalize_rep_phone, parse_reps  # noqa: E402
from src.notifications.whatsapp_rep import notify_appointment_rep  # noqa: E402
from src.odoo_sync.client import OdooCRMClient  # noqa: E402
from src.odoo_sync.crm import (  # noqa: E402
    _ASSIGNER,
    assign_lead_owner,
    clear_odoo_team_roster_cache,
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--lead-id", type=int, default=1937)
    p.add_argument("--branch", default=PRIMARY_BRANCH)
    p.add_argument(
        "--message",
        default=(
            "Nuevo Lead Asignado: Marco Gastelum | Cita: Mañana 4:00 PM | "
            "Sucursal: Periférico | Trade-in: Corolla 2020 LE ($201,200 MXN)"
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Pick RR + print; do not write Odoo or send WhatsApp",
    )
    return p


def main() -> int:
    args = _build_parser().parse_args()
    clear_odoo_team_roster_cache()

    roster = parse_reps(os.getenv("REPS_PERIFERICO") or "[]")
    print(f"env REPS_PERIFERICO count={len(roster)}")
    for i, r in enumerate(roster):
        print(f"  [{i}] {r.name} odoo_id={r.odoo_id} phone={r.phone or '(empty)'}")

    pick = assign_lead_owner(args.branch)
    print(
        f"RR pick: name={pick.rep_name!r} odoo_id={pick.odoo_id} "
        f"phone={pick.phone} index={pick.rotation_index} "
        f"cursor_next={_ASSIGNER._cursor.get(args.branch)}"
    )

    if args.dry_run:
        print("DRY-RUN: skip Odoo write + WhatsApp")
        return 0

    if not pick.odoo_id and not pick.phone:
        print("ERROR: no rep assigned (empty roster / no DEFAULT_REP_PHONE)", file=sys.stderr)
        return 2

    client = OdooCRMClient()
    client.authenticate()
    if pick.odoo_id:
        ok = client.assign_lead_advisor(int(args.lead_id), int(pick.odoo_id))
        print(f"Odoo lead {args.lead_id} user_id={pick.odoo_id} write_ok={ok}")
    else:
        print("WARN: pick has phone but no odoo_id; CRM user_id left unchanged")

    # Prefer the explicit one-liner the operator asked for.
    from src.whatsapp_worker.client import WhatsAppWorkerClient

    wa = WhatsAppWorkerClient()
    rep_phone = normalize_rep_phone(pick.phone)
    notice = None
    if rep_phone:
        wa.send_text_message(rep_phone, args.message, branch=args.branch)
        notice = {"sent": True, "phone": rep_phone, "via": "custom"}
        print(f"WhatsApp sent to {rep_phone}")
    else:
        notice = notify_appointment_rep(
            customer_name="Marco Gastelum",
            client_phone="5216141754852",
            branch=args.branch,
            interested_vehicle="Corolla 2020 LE (trade-in $201,200)",
            appointment_date="Mañana 4:00 PM",
            financing_summary="Trade-in appraisal — Corolla 2020 LE $201,200 MXN",
            stage_name="Beatriz Cita",
            lead_id=int(args.lead_id),
            assignment=pick,
            whatsapp_client=wa,
        )
        print("WhatsApp via notify_appointment_rep:", notice.as_dict())

    lead = client.execute_kw(
        "crm.lead",
        "read",
        [[int(args.lead_id)]],
        {"fields": ["id", "name", "contact_name", "user_id", "team_id", "stage_id"]},
    )
    print("LEAD", json.dumps(lead, ensure_ascii=False, default=str))
    print(
        "RESULT",
        json.dumps(
            {
                "lead_id": args.lead_id,
                "assigned": pick.as_dict(),
                "notification": notice if isinstance(notice, dict) else notice.as_dict(),
                "cursor": dict(_ASSIGNER._cursor),
            },
            ensure_ascii=False,
            default=str,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
