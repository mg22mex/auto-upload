#!/usr/bin/env python3
"""Simulate AI auto-handling + trade-in quote + appointment handoff (lead 1937).

Default mode is dry (no live Odoo / WhatsApp). Use ``--live`` to write stage /
advisor on the real lead and optionally notify the round-robin San Felipe rep.

Examples:
  PYTHONPATH=. python scripts/test_ai_lead_handoff.py
  PYTHONPATH=. python scripts/test_ai_lead_handoff.py --live --notify
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from src.lead_routing import (  # noqa: E402
    MG_QUOTE_LEAD_TAG,
    STAGE_CITA,
    STAGE_PRIMER_CONTACTO,
    detect_appointment_intent,
    format_ai_reply,
    handoff_appointment_to_rep,
    route_inbound_lead,
)
from src.whatsapp_worker.client import QUOTE_DISCLAIMER  # noqa: E402
from src.whatsapp_worker.inbound import (  # noqa: E402
    STATE_AI_ACTIVE,
    STATE_HANDOFF_TO_HUMAN,
    WhatsAppInboundEvent,
    process_qualification_turn,
)

TARGET_LEAD_ID = 1937
TARGET_NAME = "Marco Gastelum"
TARGET_BRANCH = "san_felipe"
TARGET_BRANCH_LABEL = "San Felipe"
TARGET_INTEREST = (
    "Hola, me interesa información sobre una camioneta. "
    "Forma de pago: financiamiento y a cambio. "
    "Auto a cambio: Toyota Corolla 2020."
)
TRADE_IN_DETAILS = "Versión LE, Kilometraje 85000 km"


def _event(text: str) -> WhatsAppInboundEvent:
    return WhatsAppInboundEvent(
        phone="5216140001937",
        name=TARGET_NAME,
        text=text,
        instance="autosell_san_felipe",
        message_id="test-1937",
    )


def _simulate_conversation() -> list[dict[str, Any]]:
    """AI reply → missing versión/km → Autométrica quote → appointment handoff."""
    turns: list[dict[str, Any]] = []

    t1 = process_qualification_turn(
        _event(TARGET_INTEREST),
        None,
        branch=TARGET_BRANCH,
        physical_location=TARGET_BRANCH_LABEL,
        tags=[MG_QUOTE_LEAD_TAG],
    )
    t1.session.lead_id = TARGET_LEAD_ID
    turns.append(
        {
            "step": "ai_trade_in_prompt",
            "state": t1.session.state,
            "agent": t1.session.handling_agent,
            "payment_method": t1.session.payment_method,
            "trade_in_vehicle": t1.session.trade_in_vehicle,
            "odoo_stage": t1.odoo_stage,
            "odoo_create": t1.odoo_create,
            "odoo_handoff": t1.odoo_handoff,
            "reply": t1.reply_text,
            "routing": t1.routing,
        }
    )

    t2 = process_qualification_turn(
        _event(TRADE_IN_DETAILS),
        t1.session,
        branch=TARGET_BRANCH,
        physical_location=TARGET_BRANCH_LABEL,
        tags=[MG_QUOTE_LEAD_TAG],
    )
    turns.append(
        {
            "step": "trade_in_quote",
            "state": t2.session.state,
            "agent": t2.session.handling_agent,
            "payment_method": t2.session.payment_method,
            "trade_in_vehicle": t2.session.trade_in_vehicle,
            "down_payment": t2.session.down_payment,
            "odoo_stage": t2.odoo_stage,
            "odoo_handoff": t2.odoo_handoff,
            "reply": t2.reply_text,
            "routing": t2.routing,
        }
    )

    appointment_text = "Quiero agendar una prueba de manejo mañana a las 11 en San Felipe"
    t3 = process_qualification_turn(
        _event(appointment_text),
        t2.session,
        branch=TARGET_BRANCH,
        physical_location=TARGET_BRANCH_LABEL,
        tags=[MG_QUOTE_LEAD_TAG],
    )
    turns.append(
        {
            "step": "appointment_request",
            "state": t3.session.state,
            "agent": t3.session.handling_agent,
            "odoo_stage": t3.odoo_stage,
            "odoo_handoff": t3.odoo_handoff,
            "appointment_handoff": t3.appointment_handoff,
            "appointment_time": t3.session.appointment_time,
            "reply": t3.reply_text,
            "routing": t3.routing,
        }
    )
    return turns


def _fake_odoo() -> MagicMock:
    odoo = MagicMock()
    odoo.authenticate.return_value = 1
    odoo._resolve_crm_stage_id.side_effect = lambda name: {
        STAGE_PRIMER_CONTACTO: 10,
        STAGE_CITA: 20,
        "Cita/Prueba de manejo": 20,
    }.get(name, 20)
    odoo.assign_lead_advisor.return_value = True
    odoo.post_quote_to_chatter.return_value = 1
    odoo.execute_kw.return_value = True
    return odoo


def _run_handoff(*, live: bool, notify: bool) -> dict[str, Any]:
    intent = detect_appointment_intent(
        "Quiero agendar una prueba de manejo mañana a las 11"
    )
    if live:
        from src.odoo_sync.client import OdooCRMClient

        odoo: Any = OdooCRMClient()
        odoo.authenticate()
        print(f"Live Odoo authenticated for lead {TARGET_LEAD_ID}", flush=True)
    else:
        odoo = _fake_odoo()

    wa: Any
    if notify and live:
        from src.whatsapp_worker.client import WhatsAppWorkerClient

        wa = WhatsAppWorkerClient()
    else:
        wa = MagicMock()
        wa.send_text_message.return_value = {"ok": True, "dry_run": True}

    result = handoff_appointment_to_rep(
        lead_id=TARGET_LEAD_ID,
        client_phone="5216140001937",
        branch=TARGET_BRANCH,
        vehicle_interest=TARGET_INTEREST,
        payment_method="financing_trade_in",
        appointment=intent,
        client_name=TARGET_NAME,
        odoo=odoo,
        whatsapp_client=wa,
    )
    return result.as_dict()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Write stage/advisor on real Odoo lead 1937",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help="With --live, also WhatsApp the round-robin San Felipe rep",
    )
    args = parser.parse_args()

    print("=== Lead 1937 — MG Quote Lead AI policy ===", flush=True)
    decision = route_inbound_lead({"tags": [MG_QUOTE_LEAD_TAG], "lead_id": TARGET_LEAD_ID})
    print(json.dumps(decision.as_dict(), ensure_ascii=False, indent=2), flush=True)

    print("\n=== Simulated conversation ===", flush=True)
    turns = _simulate_conversation()
    print(json.dumps(turns, ensure_ascii=False, indent=2), flush=True)

    quote_buf = str(turns[1].get("reply") or "")
    quote_meta = (turns[1].get("routing") or {}).get("trade_in_quote") or {}

    ok_prompt = (
        turns[0]["state"] == STATE_AI_ACTIVE
        and turns[0]["odoo_stage"] == STAGE_PRIMER_CONTACTO
        and not turns[0]["odoo_handoff"]
        and turns[0]["payment_method"] == "financing_trade_in"
        and ("Versión" in turns[0]["reply"] or "versión" in turns[0]["reply"].casefold())
        and ("Kilometraje" in turns[0]["reply"] or "auto a cambio" in turns[0]["reply"].casefold())
    )
    ok_quote = (
        turns[1]["state"] == STATE_AI_ACTIVE
        and "Trade-in (equity)" in quote_buf
        and "amortización" in quote_buf.casefold()
        and QUOTE_DISCLAIMER in quote_buf
        and bool(quote_meta.get("valor_compra"))
        and bool(quote_meta.get("disclaimer_present"))
        and quote_meta.get("financing") is True
        and quote_meta.get("payment_method") == "financing_trade_in"
    )
    ok_handoff = (
        turns[2]["state"] == STATE_HANDOFF_TO_HUMAN
        and turns[2]["appointment_handoff"]
        and turns[2]["odoo_stage"] == STAGE_CITA
    )

    print("\n=== Quote buffer (French + trade-in + disclaimer) ===", flush=True)
    print(quote_buf, flush=True)
    print("\n=== Quote meta ===", flush=True)
    print(json.dumps(quote_meta, ensure_ascii=False, indent=2), flush=True)

    print(f"\nTrade-in prompt OK: {ok_prompt}", flush=True)
    print(f"Trade-in quote OK: {ok_quote}", flush=True)
    print(f"Appointment handoff OK: {ok_handoff}", flush=True)

    print("\n=== Appointment handoff dispatch ===", flush=True)
    handoff = _run_handoff(live=args.live, notify=args.notify)
    print(json.dumps(handoff, ensure_ascii=False, indent=2), flush=True)

    sample = format_ai_reply(
        name=TARGET_NAME,
        text=TARGET_INTEREST,
        vehicle_interest=TARGET_INTEREST,
        branch_name=TARGET_BRANCH_LABEL,
    )
    print("\n=== Sample AI reply ===", flush=True)
    print(sample, flush=True)

    if not (ok_prompt and ok_quote and ok_handoff):
        return 1
    if handoff.get("error"):
        print(f"Handoff error: {handoff['error']}", file=sys.stderr, flush=True)
        return 1 if args.live else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
