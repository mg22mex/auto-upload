#!/usr/bin/env python3
"""Clean CRM test leads and draft/test sales orders in Odoo.

By default only targets clearly test-named leads (ATTR TEST, Prueba, Marco Test,
etc.) and sale.order drafts with $0 / test markers. Does **not** wipe all
``MG Quote Lead`` production opportunities unless ``--purge-mg-quote-leads``.

Usage::

  PYTHONPATH=. python scripts/cleanup_odoo_test_data.py --dry-run
  PYTHONPATH=. python scripts/cleanup_odoo_test_data.py
  PYTHONPATH=. python scripts/cleanup_odoo_test_data.py --purge-mg-quote-leads
"""
from __future__ import annotations

import argparse
import sys
import xmlrpc.client
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from src.odoo_sync.client import OdooCRMClient, OdooCRMError  # noqa: E402

# Name / phone patterns for setup & pipeline smoke tests.
LEAD_NAME_PATTERNS = (
    "ATTR TEST",
    "Prueba Lead",
    "Prueba Pipeline",
    "Prueba Local",
    "Marco Test",
    "RR Fresh Test",
    "[TEST]",
    "test lead",
    "Prospecto Messenger",
)
LEAD_PHONE_PREFIXES = (
    "61490010",  # attribution validate batch
    "614000000",  # live Odoo smoke phones
)
MG_QUOTE_TAG = "MG Quote Lead"


def fault_message(exc: BaseException) -> str:
    if isinstance(exc, xmlrpc.client.Fault):
        lines = [
            line.strip()
            for line in (exc.faultString or "").splitlines()
            if line.strip()
        ]
        detail = lines[-1] if lines else "unknown XML-RPC fault"
        return f"XML-RPC Fault {exc.faultCode}: {detail[:300]}"
    if isinstance(exc, (OdooCRMError, ValueError)):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def _or_domain(clauses: list[list[Any]]) -> list[Any]:
    if not clauses:
        return []
    if len(clauses) == 1:
        return list(clauses[0])
    domain: list[Any] = ["|"] * (len(clauses) - 1)
    for clause in clauses:
        domain.extend(clause)
    return domain


def find_test_leads(
    client: OdooCRMClient,
    *,
    purge_mg_quote: bool,
) -> list[dict[str, Any]]:
    clauses: list[list[Any]] = [
        [("name", "ilike", pat)] for pat in LEAD_NAME_PATTERNS
    ]
    for prefix in LEAD_PHONE_PREFIXES:
        clauses.append([("phone", "=like", f"{prefix}%")])

    if purge_mg_quote:
        tag_ids = client.execute_kw(
            "crm.tag",
            "search",
            [[("name", "=", MG_QUOTE_TAG)]],
            {"limit": 5},
        )
        if tag_ids:
            clauses.append([("tag_ids", "in", list(tag_ids))])

    domain = _or_domain(clauses)
    if not domain:
        return []
    rows = client.execute_kw(
        "crm.lead",
        "search_read",
        [domain],
        {
            "fields": ["id", "name", "phone", "tag_ids", "type", "active"],
            "limit": 5000,
            "context": {"active_test": False},
        },
    )
    # Dedupe by id
    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    for row in rows or []:
        lid = int(row["id"])
        if lid in seen:
            continue
        seen.add(lid)
        out.append(row)
    return out


def find_test_sale_orders(client: OdooCRMClient) -> list[dict[str, Any]]:
    """Draft/sent quotes that look like tests ($0, test partner/name/origin)."""
    clauses = [
        [("amount_total", "=", 0)],
        [("name", "ilike", "test")],
        [("client_order_ref", "ilike", "test")],
        [("origin", "ilike", "test")],
        [("origin", "ilike", "prueba")],
        [("partner_id.name", "ilike", "prueba")],
        [("partner_id.name", "ilike", "ATTR TEST")],
        [("partner_id.name", "ilike", "test lead")],
    ]
    # state AND (clause1 OR clause2 …)
    or_part = _or_domain(clauses)
    domain: list[Any] = [("state", "in", ["draft", "sent", "cancel"]), *or_part]
    try:
        rows = client.execute_kw(
            "sale.order",
            "search_read",
            [domain],
            {
                "fields": [
                    "id",
                    "name",
                    "state",
                    "amount_total",
                    "partner_id",
                    "origin",
                    "client_order_ref",
                ],
                "limit": 500,
            },
        )
    except Exception:
        # sale module may be restricted — soft fail
        return []
    return list(rows or [])


def archive_or_unlink_leads(
    client: OdooCRMClient,
    leads: list[dict[str, Any]],
    *,
    dry_run: bool,
    hard_delete: bool,
) -> tuple[list[int], list[int], list[str]]:
    archived: list[int] = []
    deleted: list[int] = []
    errors: list[str] = []
    for lead in leads:
        lid = int(lead["id"])
        label = f"lead {lid} {lead.get('name')!r}"
        if dry_run:
            action = "would_unlink" if hard_delete else "would_archive"
            print(f"  {action} {label}")
            (deleted if hard_delete else archived).append(lid)
            continue
        try:
            if hard_delete:
                client.execute_kw("crm.lead", "unlink", [[lid]])
                deleted.append(lid)
                print(f"  deleted {label}")
            else:
                client.execute_kw(
                    "crm.lead",
                    "write",
                    [[lid], {"active": False}],
                )
                archived.append(lid)
                print(f"  archived {label}")
        except Exception as exc:
            # Fallback: archive if unlink blocked (won opportunities, etc.)
            if hard_delete:
                try:
                    client.execute_kw(
                        "crm.lead",
                        "write",
                        [[lid], {"active": False}],
                    )
                    archived.append(lid)
                    print(f"  archived (unlink blocked) {label}")
                    continue
                except Exception as exc2:
                    msg = f"{label}: {fault_message(exc2)}"
                    errors.append(msg)
                    print(f"  ERR {msg}")
                    continue
            msg = f"{label}: {fault_message(exc)}"
            errors.append(msg)
            print(f"  ERR {msg}")
    return archived, deleted, errors


def cancel_or_unlink_orders(
    client: OdooCRMClient,
    orders: list[dict[str, Any]],
    *,
    dry_run: bool,
    hard_delete: bool,
) -> tuple[list[int], list[int], list[str]]:
    cancelled: list[int] = []
    deleted: list[int] = []
    errors: list[str] = []
    for order in orders:
        oid = int(order["id"])
        label = (
            f"SO {order.get('name')} id={oid} "
            f"state={order.get('state')} total={order.get('amount_total')}"
        )
        if dry_run:
            action = "would_unlink" if hard_delete else "would_cancel"
            print(f"  {action} {label}")
            (deleted if hard_delete else cancelled).append(oid)
            continue
        try:
            state = str(order.get("state") or "")
            if hard_delete and state in ("draft", "cancel"):
                client.execute_kw("sale.order", "unlink", [[oid]])
                deleted.append(oid)
                print(f"  deleted {label}")
                continue
            if state in ("draft", "sent"):
                # Prefer action_cancel when available
                try:
                    client.execute_kw("sale.order", "action_cancel", [[oid]])
                except Exception:
                    client.execute_kw(
                        "sale.order", "write", [[oid], {"state": "cancel"}]
                    )
                cancelled.append(oid)
                print(f"  cancelled {label}")
            else:
                print(f"  skip {label}")
        except Exception as exc:
            msg = f"{label}: {fault_message(exc)}"
            errors.append(msg)
            print(f"  ERR {msg}")
    return cancelled, deleted, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List targets only — no Odoo writes",
    )
    parser.add_argument(
        "--hard-delete",
        action="store_true",
        help="Unlink records instead of archive/cancel (when allowed)",
    )
    parser.add_argument(
        "--purge-mg-quote-leads",
        action="store_true",
        help=(
            "Also archive/delete ALL leads tagged 'MG Quote Lead' "
            "(destructive — production Beatriz leads)"
        ),
    )
    parser.add_argument(
        "--skip-sales",
        action="store_true",
        help="Only clean crm.lead",
    )
    args = parser.parse_args()

    client = OdooCRMClient()
    try:
        uid = client.authenticate()
        print(f"Odoo auth UID={uid} @ {client.url}")
    except Exception as exc:
        print(f"FAIL auth: {fault_message(exc)}")
        return 2

    if args.purge_mg_quote_leads:
        print(
            "WARN: --purge-mg-quote-leads will include ALL MG Quote Lead tagged CRM"
        )

    print("\n=== CRM leads ===")
    leads = find_test_leads(client, purge_mg_quote=bool(args.purge_mg_quote_leads))
    print(f"Matched leads: {len(leads)}")
    archived_leads, deleted_leads, lead_errors = archive_or_unlink_leads(
        client,
        leads,
        dry_run=bool(args.dry_run),
        hard_delete=bool(args.hard_delete),
    )

    cancelled_sos: list[int] = []
    deleted_sos: list[int] = []
    so_errors: list[str] = []
    if not args.skip_sales:
        print("\n=== Sales orders (draft/test) ===")
        orders = find_test_sale_orders(client)
        print(f"Matched sale.order: {len(orders)}")
        cancelled_sos, deleted_sos, so_errors = cancel_or_unlink_orders(
            client,
            orders,
            dry_run=bool(args.dry_run),
            hard_delete=bool(args.hard_delete),
        )

    print("\n=== SUMMARY ===")
    mode = "DRY-RUN" if args.dry_run else "LIVE"
    print(f"mode:              {mode}")
    print(f"leads archived:    {len(archived_leads)}")
    print(f"leads deleted:     {len(deleted_leads)}")
    print(f"SOs cancelled:     {len(cancelled_sos)}")
    print(f"SOs deleted:       {len(deleted_sos)}")
    print(f"errors:            {len(lead_errors) + len(so_errors)}")
    if archived_leads[:10]:
        print("sample archived leads:", ", ".join(str(i) for i in archived_leads[:10]))
    if deleted_leads[:10]:
        print("sample deleted leads:", ", ".join(str(i) for i in deleted_leads[:10]))
    if cancelled_sos or deleted_sos:
        print(
            "sample SOs:",
            ", ".join(str(i) for i in (cancelled_sos + deleted_sos)[:10]),
        )
    for err in (lead_errors + so_errors)[:8]:
        print(f"  - {err}")

    return 1 if (lead_errors or so_errors) else 0


if __name__ == "__main__":
    raise SystemExit(main())
