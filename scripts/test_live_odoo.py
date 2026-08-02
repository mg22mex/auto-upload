#!/usr/bin/env python3
"""Live Odoo CRM smoke test against autosellmx.odoo.com.

Reads ODOO_URL / ODOO_DB / ODOO_USERNAME / ODOO_API_KEY from .env.
Usage:
  python scripts/test_live_odoo.py
"""
from __future__ import annotations

import sys
import traceback
import xmlrpc.client
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from src.odoo_sync.client import OdooCRMClient, OdooCRMError  # noqa: E402

TEST_NAME = "Prueba Lead AI"
TEST_PHONE = "6140000000"
TEST_VEHICLE = "Mazda CX-5 2020"
TEST_BRANCH_ID = 1

SAMPLE_QUOTE = """\
Cotización Scotiabank (prueba AI)
Vehículo: Mazda CX-5 2020
Enganche: $30,000.00
Plazo: 36 meses
Mensualidad estimada: $10,726.90
— Autosell pipeline live test
"""


def _fault_message(exc: BaseException) -> str:
    if isinstance(exc, xmlrpc.client.Fault):
        text = (exc.faultString or "").strip()
        # Prefer last meaningful line over full Odoo server traceback
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        tail = lines[-1] if lines else text
        if len(tail) > 240:
            tail = tail[:237] + "..."
        return f"XML-RPC Fault {exc.faultCode}: {tail}"
    if isinstance(exc, OdooCRMError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def main() -> int:
    print("=== Live Odoo integration test ===")
    print(f"Root: {ROOT}")
    client = OdooCRMClient()
    print(f"URL: {client.url or '(missing)'}")
    print(f"DB:  {client.db or '(missing)'}")
    print(f"User: {client.username or '(missing)'}")

    # Step 1 — authenticate
    print("\n[1/3] authenticate()")
    try:
        uid = client.authenticate()
    except Exception as exc:
        print(f"FAIL auth: {_fault_message(exc)}")
        return 1
    print(f"OK  authenticated UID={uid}")

    # Step 2 — create/update lead
    print("\n[2/3] create_or_update_lead()")
    try:
        lead_result = client.create_or_update_lead(
            TEST_NAME,
            TEST_PHONE,
            TEST_VEHICLE,
            TEST_BRANCH_ID,
        )
    except Exception as exc:
        print(f"FAIL lead: {_fault_message(exc)}")
        # Common causes: missing crm rights, invalid team_id, unknown custom field
        print(
            "Hint: check CRM access, crm.team id=1 exists, "
            "and optional x_vehicle_name field."
        )
        if os_getenv_debug():
            traceback.print_exc()
        return 2
    lead_id = lead_result.lead_id
    print(f"OK  lead_id={lead_id}")
    if lead_result.activity_id is not None:
        print(f"OK  follow-up activity_id={lead_result.activity_id}")
    if lead_result.tag_ids:
        print(f"OK  tag_ids={list(lead_result.tag_ids)}")

    # Step 3 — chatter quote
    print("\n[3/3] post_quote_to_chatter()")
    try:
        msg_id = client.post_quote_to_chatter(lead_id, SAMPLE_QUOTE)
    except Exception as exc:
        print(f"FAIL chatter: {_fault_message(exc)}")
        print("Hint: need mail / Discuss rights on crm.lead.")
        return 3
    print(f"OK  message_id={msg_id}")

    print("\n=== PASS ===")
    print(f"UID={uid} lead_id={lead_id} message_id={msg_id}")
    return 0


def os_getenv_debug() -> bool:
    import os

    return os.getenv("ODOO_LIVE_DEBUG", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


if __name__ == "__main__":
    raise SystemExit(main())
