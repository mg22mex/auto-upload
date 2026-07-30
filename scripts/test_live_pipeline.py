#!/usr/bin/env python3
"""Live Phase 2 pipeline: Odoo inventory → Scotiabank quote → CRM chatter."""
from __future__ import annotations

import sys
import xmlrpc.client
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from src.odoo_sync.client import OdooCRMClient, OdooCRMError  # noqa: E402
from src.pipeline import AutosellPipeline  # noqa: E402

QUERY_CANDIDATES = ("CX3", "Mazda")
TEST_PHONE = "6140000002"


def fault_message(exc: BaseException) -> str:
    if isinstance(exc, xmlrpc.client.Fault):
        lines = [
            line.strip()
            for line in (exc.faultString or "").splitlines()
            if line.strip()
        ]
        detail = lines[-1] if lines else "unknown XML-RPC fault"
        return f"XML-RPC Fault {exc.faultCode}: {detail[:300]}"
    if isinstance(exc, OdooCRMError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def main() -> int:
    odoo = OdooCRMClient()
    try:
        uid = odoo.authenticate()
        print(f"OK auth UID={uid}")
    except Exception as exc:
        print(f"FAIL auth: {fault_message(exc)}")
        return 1

    selected_query = ""
    selected = None
    try:
        for query in QUERY_CANDIDATES:
            matches = odoo.search_vehicle_inventory(query)
            print(f"Inventory query={query!r}: {len(matches)} match(es)")
            if matches:
                selected_query = query
                selected = matches[0]
                break
    except Exception as exc:
        print(f"FAIL inventory: {fault_message(exc)}")
        return 2

    if selected is None:
        print("FAIL inventory: no CX3/Mazda vehicle found")
        return 2

    print(
        "Selected "
        f"ID={selected['id']} name={selected['name']!r} "
        f"price={selected['list_price']} qty={selected['qty_available']}"
    )
    if selected["list_price"] <= 1:
        print("WARN Odoo list_price is a placeholder (<= 1); quote will reflect it.")

    pipeline = AutosellPipeline(
        odoo=odoo,
        assign_advisor=False,
        dispatch_whatsapp=False,
    )
    result = pipeline.process_lead(
        {
            "name": "Prueba Pipeline Inventario AI",
            "phone": TEST_PHONE,
            "vehicle_name": selected_query,
            # Intentionally omitted: pipeline must resolve from Odoo.
            "term_months": 36,
            "branch_id": 1,
            "annual_auto_insurance": 0,
        }
    )

    for step in result.steps:
        print(f"{step['step']}: {step['status']} {step}")

    if not result.ok:
        print(f"FAIL pipeline: {result.error}")
        return 3

    print("=== PASS ===")
    print(
        f"lead_id={result.lead_id} "
        f"monthly={result.estimated_monthly_payment} "
        f"chatter=posted"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
