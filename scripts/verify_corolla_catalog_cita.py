#!/usr/bin/env python3
"""Reset WA/Vapi session and verify Corolla catalog cita (no Autométrica).

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
    "por favor. Se puede en media hora?"
)


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
        result = chat_with_beatriz(
            text=MSG,
            phone=phone,
            customer_name=args.name,
            branch=args.branch,
            instance=args.instance,
            store=store,
        )

    meta = store.get_meta(phone, args.instance)
    report = {
        "ok": (
            "book_appointment" in (result.tools_called or [])
            and "get_tradein_valuation" not in (result.tools_called or [])
            and "Corolla" in (result.interested_vehicle or "")
            and "XLE" in (result.interested_vehicle or meta.get("interested_vehicle") or "")
            and float(meta.get("vehicle_price") or 0) == 365000.0
            and "195,500" not in (result.reply_text or "")
            and "Autométrica" not in (result.reply_text or "")
            and "Periférico" in (result.reply_text or "")
        ),
        "tools_called": result.tools_called,
        "interested_vehicle": result.interested_vehicle,
        "vehicle_price": meta.get("vehicle_price"),
        "vehicle_branch": meta.get("vehicle_branch"),
        "tradein_summary": result.tradein_summary,
        "reply_text": result.reply_text,
        "crm_mock_branch": (
            None
            if args.live_crm
            else (crm.call_args[0][0].branch if crm and crm.call_args else None)
        ),
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
