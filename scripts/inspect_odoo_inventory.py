#!/usr/bin/env python3
"""Read-only Odoo inventory inspection.

Reads ODOO_URL / ODOO_DB / ODOO_USERNAME / ODOO_API_KEY from .env.
"""
from __future__ import annotations

import sys
import xmlrpc.client
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from src.odoo_sync.client import OdooCRMClient, OdooCRMError  # noqa: E402

MODELS = ("product.template", "product.product")
FALLBACK_MODEL = "fleet.vehicle"
BRANCH_FIELD_TERMS = (
    "branch",
    "warehouse",
    "location",
    "tag",
    "categ",
    "company",
)
DISPLAY_FIELDS = (
    "name",
    "display_name",
    "list_price",
    "lst_price",
    "is_published",
    "website_published",
    "website_url",
    "qty_available",
)


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


def available_fields(client: OdooCRMClient, model: str) -> dict[str, dict[str, Any]]:
    return client.execute_kw(
        model,
        "fields_get",
        [],
        {"attributes": ["string", "type", "relation"]},
    )


def branch_fields(fields: dict[str, dict[str, Any]]) -> list[str]:
    matches = []
    for field_name, metadata in fields.items():
        label = str(metadata.get("string") or "")
        haystack = f"{field_name} {label}".lower()
        if any(term in haystack for term in BRANCH_FIELD_TERMS):
            matches.append(field_name)
    return sorted(matches)


def printable_value(value: Any) -> str:
    if value in (False, None, "", []):
        return "-"
    if isinstance(value, list) and len(value) == 2 and isinstance(value[0], int):
        return f"{value[1]} (id={value[0]})"
    return str(value)


def inspect_model(client: OdooCRMClient, model: str) -> int:
    print(f"\n=== {model} ===")
    try:
        fields = available_fields(client, model)
    except Exception as exc:
        print(f"SKIP fields: {fault_message(exc)}")
        return 0

    related = branch_fields(fields)
    requested = [
        field_name
        for field_name in (*DISPLAY_FIELDS, *related)
        if field_name in fields
    ]
    requested = list(dict.fromkeys(requested))

    print("Branch/location/tag fields:")
    if related:
        for field_name in related:
            meta = fields[field_name]
            relation = (
                f" -> {meta['relation']}" if meta.get("relation") else ""
            )
            print(
                f"  {field_name}: {meta.get('string', '-')} "
                f"[{meta.get('type', '-')}{relation}]"
            )
    else:
        print("  (none available)")

    try:
        records = client.execute_kw(
            model,
            "search_read",
            [[]],
            {
                "fields": requested,
                "limit": 10,
                "order": "id desc",
            },
        )
    except Exception as exc:
        print(f"FAIL records: {fault_message(exc)}")
        return 0

    print(f"Records: {len(records)}")
    for record in records:
        title = record.get("name") or record.get("display_name") or "-"
        price = record.get("list_price", record.get("lst_price", "-"))
        published = record.get(
            "is_published", record.get("website_published", "-")
        )
        website_url = record.get("website_url", "-")
        quantity = record.get("qty_available", "-")
        print(
            f"\nID={record['id']} | Name={title} | "
            f"List Price={price} | Qty={quantity}"
        )
        print(f"  Published={published} | Website={website_url}")
        if related:
            values = ", ".join(
                f"{field_name}={printable_value(record.get(field_name))}"
                for field_name in related
            )
            print(f"  Branch/location/tags: {values}")
    return len(records)


def main() -> int:
    client = OdooCRMClient()
    try:
        uid = client.authenticate()
    except Exception as exc:
        print(f"FAIL auth: {fault_message(exc)}")
        return 1

    print(f"Authenticated UID={uid}")
    counts = {model: inspect_model(client, model) for model in MODELS}
    if counts["product.template"] == 0:
        print("\nproduct.template empty/unavailable; inspecting fleet.vehicle.")
        inspect_model(client, FALLBACK_MODEL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
