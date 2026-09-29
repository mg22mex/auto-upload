#!/usr/bin/env python3
"""Sync autosell.mx catalog → Odoo product.template inventory.

Diff-first: one bulk ``search_read``, then write **only** SKUs whose
``name`` / ``list_price`` changed (or missing / need reactivation).
Unchanged catalogs finish in seconds.

Usage:
  python scripts/sync_odoo_inventory.py
  python scripts/sync_odoo_inventory.py --from-snapshot data/catalog_latest.json
  python scripts/sync_odoo_inventory.py --scrape --limit 20
"""
from __future__ import annotations

import argparse
import sys
import xmlrpc.client
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from src.facebook.util import parse_mxn_price  # noqa: E402
from src.inventory.autosell import AutosellCatalogError, fetch_catalog  # noqa: E402
from src.inventory.snapshot import load_catalog_snapshot, save_catalog_snapshot  # noqa: E402
from src.models import Vehicle  # noqa: E402
from src.odoo_sync.client import OdooCRMClient, OdooCRMError  # noqa: E402

PRICE_EPS = 0.01


def fault_message(exc: BaseException) -> str:
    if isinstance(exc, xmlrpc.client.Fault):
        lines = [
            line.strip()
            for line in (exc.faultString or "").splitlines()
            if line.strip()
        ]
        detail = lines[-1] if lines else "unknown XML-RPC fault"
        return f"XML-RPC Fault {exc.faultCode}: {detail[:300]}"
    if isinstance(exc, (OdooCRMError, AutosellCatalogError, ValueError)):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def product_name(vehicle: Vehicle) -> str:
    """Odoo-style title: TITLE BRAND YEAR."""
    parts = [vehicle.title, vehicle.brand, vehicle.year]
    return " ".join(p for p in parts if p).strip() or vehicle.slug


def parse_price(vehicle: Vehicle) -> float:
    return float(parse_mxn_price(vehicle.price))


def load_vehicles(args: argparse.Namespace, config: dict) -> list[Vehicle]:
    if args.from_snapshot:
        path = Path(args.from_snapshot)
        print(f"Loading snapshot: {path}")
        vehicles = load_catalog_snapshot(path)
        print(f"Loaded {len(vehicles)} vehicles")
        return vehicles

    print("Scraping live catalog from autosell.mx ...")
    vehicles = fetch_catalog(config)
    out = Path(args.save_snapshot)
    save_catalog_snapshot(vehicles, out)
    print(f"Scraped {len(vehicles)} vehicles → {out}")
    return vehicles


def bulk_odoo_by_sku(
    client: OdooCRMClient,
    *,
    categ_id: int | None,
) -> dict[str, dict[str, Any]]:
    """Map default_code → product.template row (includes archived).

    Loads all SKU'd templates (no category filter) so consignación + lote
    share one in-memory diff map. ``categ_id`` is accepted for API compat.
    """
    del categ_id  # reserved; category filter would miss VEHICULO CONSIGNACION
    domain: list[Any] = [("default_code", "!=", False)]
    rows = client.execute_kw(
        "product.template",
        "search_read",
        [domain],
        {
            "fields": [
                "id",
                "name",
                "default_code",
                "list_price",
                "active",
                "sale_ok",
            ],
            "limit": 5000,
            "context": {"active_test": False},
        },
    )
    out: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        code = str(row.get("default_code") or "").strip()
        if not code:
            continue
        out[code] = {
            "id": int(row["id"]),
            "name": str(row.get("name") or ""),
            "default_code": code,
            "list_price": float(row.get("list_price") or 0),
            "active": bool(row.get("active")),
            "sale_ok": bool(row.get("sale_ok", True)),
        }
    return out


def needs_write(
    existing: dict[str, Any] | None,
    *,
    name: str,
    price: float,
) -> str | None:
    """Return action label if a write is required, else None."""
    if existing is None:
        return "create"
    if not existing.get("active") or not existing.get("sale_ok"):
        return "reactivate"
    if (existing.get("name") or "").strip() != name.strip():
        return "rename"
    old = float(existing.get("list_price") or 0)
    if abs(old - price) > PRICE_EPS:
        return "price"
    return None


def sync_one(client: OdooCRMClient, vehicle: Vehicle, categ_id: int | None) -> dict:
    name = product_name(vehicle)
    price = parse_price(vehicle)
    return client.upsert_vehicle_product(
        name=name,
        list_price=price,
        default_code=vehicle.autosell_id,
        categ_id=categ_id,
        description=f"{vehicle.url}\n{vehicle.mileage} | {vehicle.version}".strip(),
        qty_available=1.0,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from-snapshot",
        help="Load vehicles from catalog JSON instead of live scrape",
    )
    parser.add_argument(
        "--save-snapshot",
        default="data/catalog_latest.json",
        help="Where to save live scrape (default: data/catalog_latest.json)",
    )
    parser.add_argument("--limit", type=int, default=0, help="Max vehicles (0=all)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse/match only; no Odoo writes",
    )
    parser.add_argument(
        "--no-archive",
        action="store_true",
        help="Skip soft-delete of Odoo vehicles missing from catalog",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Write every catalog SKU even when unchanged",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml",
    )
    args = parser.parse_args()

    config_path = ROOT / args.config
    with config_path.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}

    try:
        vehicles = load_vehicles(args, config)
    except Exception as exc:
        print(f"FAIL catalog: {fault_message(exc)}")
        return 1

    if args.limit and args.limit > 0:
        vehicles = vehicles[: args.limit]
        print(f"Limited to {len(vehicles)} vehicles")

    client = OdooCRMClient()
    try:
        uid = client.authenticate()
        print(f"Odoo auth UID={uid} @ {client.url}")
    except Exception as exc:
        print(f"FAIL auth: {fault_message(exc)}")
        return 2

    try:
        categ_id = client.find_vehicle_category_id()
        print(f"Vehicle category id={categ_id}")
    except Exception as exc:
        print(f"WARN category: {fault_message(exc)}")
        categ_id = None

    try:
        by_sku = bulk_odoo_by_sku(client, categ_id=categ_id)
        print(f"Odoo bulk load: {len(by_sku)} SKUs (incl. archived)")
    except Exception as exc:
        print(f"WARN bulk load failed ({fault_message(exc)}); falling back per-SKU")
        by_sku = {}

    created: list[dict] = []
    updated: list[dict] = []
    unchanged: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    archived: list[dict] = []
    catalog_codes: set[str] = set()

    for index, vehicle in enumerate(vehicles, start=1):
        label = f"{vehicle.autosell_id} {product_name(vehicle)}"
        catalog_codes.add(vehicle.autosell_id)
        try:
            price = parse_price(vehicle)
        except ValueError as exc:
            skipped.append(f"{label}: {exc}")
            print(f"[{index}/{len(vehicles)}] SKIP {label} ({exc})")
            continue

        name = product_name(vehicle)
        existing = by_sku.get(str(vehicle.autosell_id).strip())
        reason = needs_write(existing, name=name, price=price)
        if reason is None and not args.force:
            unchanged.append(str(vehicle.autosell_id))
            if index <= 3 or index == len(vehicles) or index % 50 == 0:
                print(f"[{index}/{len(vehicles)}] SKIP unchanged {label}")
            continue

        if args.dry_run:
            action = f"would_{reason or 'update'}"
            print(
                f"[{index}/{len(vehicles)}] DRY {action} "
                f"{label} price={price}"
            )
            if reason == "create":
                created.append({"id": -1, "default_code": vehicle.autosell_id})
            else:
                updated.append({"id": existing["id"] if existing else -1})
            continue

        try:
            result = sync_one(client, vehicle, categ_id)
        except Exception as exc:
            msg = f"{label}: {fault_message(exc)}"
            errors.append(msg)
            print(f"[{index}/{len(vehicles)}] ERR {msg}")
            continue

        bucket = created if result["action"] == "created" else updated
        bucket.append(result)
        # Refresh local cache so orphan pass stays accurate
        by_sku[str(vehicle.autosell_id).strip()] = {
            "id": int(result["id"]),
            "name": name,
            "default_code": str(vehicle.autosell_id),
            "list_price": float(price),
            "active": True,
            "sale_ok": True,
        }
        print(
            f"[{index}/{len(vehicles)}] {result['action'].upper()} "
            f"({reason}) id={result['id']} {result['name']!r} "
            f"list_price={result['list_price']}"
        )

    # Soft-delete orphans only on full-catalog runs (never with --limit).
    do_archive = not args.no_archive and not (args.limit and args.limit > 0)
    if do_archive:
        try:
            orphans = [
                p
                for code, p in by_sku.items()
                if code not in catalog_codes and p.get("active")
            ]
            if args.dry_run:
                archived = orphans
                print(
                    f"\nDRY archive orphans: {len(orphans)} "
                    f"(catalog keep={len(catalog_codes)})"
                )
                for product in orphans[:20]:
                    print(
                        f"  would_archive SKU {product['default_code']} as SOLD "
                        f"id={product['id']} {product['name']!r}"
                    )
                if len(orphans) > 20:
                    print(f"  … +{len(orphans) - 20} more")
            elif orphans:
                archived = []
                for product in orphans:
                    status_meta = client._write_product_inventory_status(
                        int(product["id"]),
                        inventory_status="sold",
                        active=False,
                    )
                    try:
                        client.execute_kw(
                            "product.template",
                            "write",
                            [[int(product["id"])], {"sale_ok": False}],
                        )
                    except Exception:
                        pass
                    archived.append(
                        {
                            **product,
                            "inventory_status": "sold",
                            "state_field": status_meta.get("state_field"),
                            "state_value": status_meta.get("state_value"),
                        }
                    )
                print(
                    f"\nArchived orphans as SOLD (active=False): {len(archived)} "
                    f"(catalog keep={len(catalog_codes)})"
                )
                for product in archived:
                    print(
                        f"  Archived orphan SKU {product['default_code']} as SOLD "
                        f"id={product['id']} {product['name']!r}"
                        + (
                            f" field={product.get('state_field')}="
                            f"{product.get('state_value')}"
                            if product.get("state_field")
                            else ""
                        )
                    )
            else:
                print("\nNo orphans to archive")
        except Exception as exc:
            msg = f"orphan archive failed: {fault_message(exc)}"
            errors.append(msg)
            print(f"ERR {msg}")
    elif args.limit and args.limit > 0:
        print("\nSkip orphan archive (--limit set; partial catalog unsafe)")
    else:
        print("\nSkip orphan archive (--no-archive)")

    print("\n=== SUMMARY ===")
    print(f"catalog:   {len(vehicles)}")
    print(f"unchanged: {len(unchanged)}")
    print(f"created:   {len(created)}")
    print(f"updated:   {len(updated)}")
    print(f"archived:  {len(archived)}")
    print(f"skipped:   {len(skipped)}")
    print(f"errors:    {len(errors)}")
    if created[:5]:
        print("sample created:", ", ".join(str(r["id"]) for r in created[:5]))
    if updated[:5]:
        print("sample updated:", ", ".join(str(r["id"]) for r in updated[:5]))
    if archived[:5]:
        print(
            "sample archived:",
            ", ".join(
                f"{r['default_code']}(id={r['id']})" for r in archived[:5]
            ),
        )
    if errors[:5]:
        print("sample errors:")
        for err in errors[:5]:
            print(f"  - {err}")

    return 1 if errors and not (created or updated or archived or unchanged) else 0


if __name__ == "__main__":
    raise SystemExit(main())
