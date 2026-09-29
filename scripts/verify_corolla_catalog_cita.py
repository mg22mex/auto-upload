#!/usr/bin/env python3
"""Reset WA/Vapi session and verify Corolla pending→confirm cita (no Autométrica).

Usage:
  PYTHONPATH=. python scripts/verify_corolla_catalog_cita.py
  PYTHONPATH=. python scripts/verify_corolla_catalog_cita.py --phone 5216141754852
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

MSG = (
    "Hola. Quiero agendar una cita para ver un Corolla que tienen, "
    "hoy a las 530 pm, por favor."
)
CONFIRM = "Confirma a esa hora, 5:30 pm"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phone", default="5216141754852")
    parser.add_argument("--name", default="Marco Gastelum")
    parser.add_argument("--branch", default="san_felipe")
    parser.add_argument("--instance", default="autosell_san_felipe")
    parser.add_argument(
        "--live-crm",
        action="store_true",
        help="Allow real Odoo create_vapi_lead (default: dry mock)",
    )
    args = parser.parse_args()

    from src.voice_gateway.vapi_chat import (
        chat_with_beatriz,
        clear_wa_session_context,
        get_chat_store,
    )

    phone = args.phone
    print(f"Resetting session phone={phone} …", flush=True)
    clear_wa_session_context(phone, instance=args.instance)

    # Seed sticky Autométrica that must NOT win.
    store = get_chat_store()
    store.set_chat_id(
        phone,
        "verify-prev",
        instance=args.instance,
        meta={
            "trade_in_label": "Toyota Corolla 2020 LE",
            "valor_compra": 195500,
            "tradein_summary": "Estimación Autométrica ~$195,500",
            "interested_vehicle": "Ford Mustang GT 2025",
        },
    )

    crm_patch = patch(
        "src.voice_gateway.vapi_bridge.create_vapi_lead",
        return_value={
            "status": "created",
            "lead_id": -1,
            "dry_run": True,
            "branch": "periferico",
        },
    )
    ctx = crm_patch if not args.live_crm else nullcontext()
    with ctx as crm:
        pending = chat_with_beatriz(
            text=MSG,
            phone=phone,
            customer_name=args.name,
            branch=args.branch,
            instance=args.instance,
            store=store,
        )
        meta_pending = store.get_meta(phone, args.instance)
        confirmed = chat_with_beatriz(
            text=CONFIRM,
            phone=phone,
            customer_name=args.name,
            branch=args.branch,
            instance=args.instance,
            store=store,
        )
        meta_done = store.get_meta(phone, args.instance)

    crm_branch = None
    if not args.live_crm and crm and crm.call_args:
        crm_branch = crm.call_args[0][0].branch

    report = {
        "ok": (
            "pending_appointment_confirmation" in (pending.tools_called or [])
            and "book_appointment" in (confirmed.tools_called or [])
            and "get_tradein_valuation" not in (pending.tools_called or [])
            and "Corolla" in (pending.interested_vehicle or "")
            and meta_pending.get("pending_appointment_confirmation") == "1"
            and meta_pending.get("pending_appointment_branch") == "periferico"
            and "confirmo esa visita" in (pending.reply_text or "").casefold()
            and "5:30 pm" in (pending.reply_text or "")
            and "53" not in (pending.reply_text or "").replace("5:30", "")
            and "195,500" not in (pending.reply_text or "")
            and "Cita confirmada" in (confirmed.reply_text or "")
            and "5:30 pm" in (confirmed.reply_text or "")
            and "53" not in (confirmed.reply_text or "").replace("5:30", "")
            and "Periférico" in (confirmed.reply_text or "")
            and not meta_done.get("pending_appointment_confirmation")
            and (args.live_crm or crm_branch == "periferico")
            and (args.live_crm or (crm and crm.call_count == 1))
        ),
        "pending_tools": pending.tools_called,
        "confirm_tools": confirmed.tools_called,
        "interested_vehicle": confirmed.interested_vehicle or pending.interested_vehicle,
        "vehicle_branch": meta_done.get("vehicle_branch")
        or meta_pending.get("vehicle_branch"),
        "pending_reply": pending.reply_text,
        "confirm_reply": confirmed.reply_text,
        "crm_mock_branch": crm_branch,
        "crm_calls": None if args.live_crm else (crm.call_count if crm else 0),
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["ok"] else 1


class nullcontext:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


if __name__ == "__main__":
    raise SystemExit(main())
