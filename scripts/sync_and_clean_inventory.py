#!/usr/bin/env python3
"""Align Odoo vehicle inventory with live autosell.mx catalog.

1. Sync ``list_price`` to website prices
2. Archive products missing from the live catalog (``active=False``)
3. Tag ownership tipo from title asterisk:
   - ``*`` in title → Consignación (Auto de Cliente) + category VEHICULO CONSIGNACION
   - no ``*`` → Lote / Propio (Inventario Agencia) + category vehiculos
4. Bind ``warehouse_id`` / ``location_id`` to Periférico or San Felipe
   from catalog markers (``+`` San Felipe, ``*``/``-``/default Periférico)

Usage::

  PYTHONPATH=. python scripts/sync_and_clean_inventory.py --dry-run
  PYTHONPATH=. python scripts/sync_and_clean_inventory.py --from-snapshot data/catalog_latest.json
  PYTHONPATH=. python scripts/sync_and_clean_inventory.py
"""
from __future__ import annotations

import argparse
import re
import sys
import xmlrpc.client
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from src.config import BRANCH_TAG_MAP, PLACEHOLDER_BRANCH, PRIMARY_BRANCH  # noqa: E402
from src.facebook.util import parse_mxn_price  # noqa: E402
from src.inventory.autosell import AutosellCatalogError, fetch_catalog  # noqa: E402
from src.inventory.snapshot import load_catalog_snapshot, save_catalog_snapshot  # noqa: E402
from src.models import Vehicle  # noqa: E402
from src.odoo_sync.client import OdooCRMClient, OdooCRMError  # noqa: E402

MIN_CATALOG = 5
TAG_CONSIGNACION = "Consignación"
TAG_LOTE = "Lote / Propio"
CATEG_CONSIGNACION = "VEHICULO CONSIGNACION"
CATEG_LOTE = "vehiculos"
MARKER_RE = re.compile(r"([*+-])\s*$")


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
    parts = [vehicle.title, vehicle.brand, vehicle.year]
    return " ".join(p for p in parts if p).strip() or vehicle.slug


def parse_price(vehicle: Vehicle) -> float:
    return float(parse_mxn_price(vehicle.price))


def is_consignment(title: str) -> bool:
    """Trailing asterisk in title → customer consignment vehicle."""
    text = (title or "").rstrip()
    return bool(text) and (text.endswith("*") or re.search(r"\s\*\s*$", text) is not None)


def branch_from_title(title: str) -> str:
    """Map catalog marker to warehouse branch key."""
    text = (title or "").strip()
    if not text:
        return PRIMARY_BRANCH
    match = MARKER_RE.search(text)
    if match:
        marker = match.group(1)
        return BRANCH_TAG_MAP.get(marker, PRIMARY_BRANCH)
    # Leading marker (rare)
    if text[:1] in BRANCH_TAG_MAP:
        return BRANCH_TAG_MAP[text[:1]]
    lowered = text.lower()
    if "san felipe" in lowered or "san_felipe" in lowered:
        return PLACEHOLDER_BRANCH
    return PRIMARY_BRANCH


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


def ensure_product_tag(client: OdooCRMClient, name: str) -> int | None:
    label = (name or "").strip()
    if not label:
        return None
    rows = client.execute_kw(
        "product.tag",
        "search_read",
        [[("name", "=", label)]],
        {"fields": ["id"], "limit": 1},
    )
    if rows:
        return int(rows[0]["id"])
    try:
        return int(client.execute_kw("product.tag", "create", [{"name": label}]))
    except Exception as exc:
        print(f"WARN product.tag create {label!r}: {fault_message(exc)}")
        return None


def ensure_category(client: OdooCRMClient, name: str) -> int | None:
    rows = client.execute_kw(
        "product.category",
        "search_read",
        [[("name", "=ilike", name)]],
        {"fields": ["id", "name"], "limit": 5},
    )
    for row in rows or []:
        if str(row.get("name") or "").strip().lower() == name.strip().lower():
            return int(row["id"])
    if rows:
        return int(rows[0]["id"])
    try:
        return int(client.execute_kw("product.category", "create", [{"name": name}]))
    except Exception as exc:
        print(f"WARN product.category create {name!r}: {fault_message(exc)}")
        return None


def load_warehouses(client: OdooCRMClient) -> dict[str, dict[str, int]]:
    """Return branch_key → {warehouse_id, location_id}."""
    rows = client.execute_kw(
        "stock.warehouse",
        "search_read",
        [[]],
        {"fields": ["id", "name", "code", "lot_stock_id"], "limit": 20},
    )
    out: dict[str, dict[str, int]] = {}
    for row in rows or []:
        name = str(row.get("name") or "").lower()
        code = str(row.get("code") or "").lower()
        wh_id = int(row["id"])
        loc = row.get("lot_stock_id")
        loc_id = int(loc[0]) if isinstance(loc, (list, tuple)) and loc else None
        if loc_id is None:
            continue
        meta = {"warehouse_id": wh_id, "location_id": loc_id}
        if "felipe" in name or "sanf" in code:
            out[PLACEHOLDER_BRANCH] = meta
        elif "perifer" in name or "peri" in code:
            out[PRIMARY_BRANCH] = meta
    return out


def write_tipo_and_warehouse(
    client: OdooCRMClient,
    product_id: int,
    *,
    consignacion: bool,
    branch: str,
    tag_consign_id: int | None,
    tag_lote_id: int | None,
    categ_consign_id: int | None,
    categ_lote_id: int | None,
    warehouses: dict[str, dict[str, int]],
) -> dict[str, Any]:
    """Best-effort write of tags, category, warehouse_id, location_id."""
    vals: dict[str, Any] = {}
    tipo_tag = tag_consign_id if consignacion else tag_lote_id
    other_tag = tag_lote_id if consignacion else tag_consign_id
    if tipo_tag:
        # Replace opposing tipo tag; keep other product tags.
        try:
            current = client.execute_kw(
                "product.template",
                "read",
                [[product_id]],
                {"fields": ["product_tag_ids"]},
            )
            existing = list((current[0].get("product_tag_ids") or []) if current else [])
            keep = [t for t in existing if t not in {other_tag, tipo_tag}]
            keep.append(int(tipo_tag))
            vals["product_tag_ids"] = [(6, 0, keep)]
        except Exception:
            vals["product_tag_ids"] = [(4, int(tipo_tag))]

    categ_id = categ_consign_id if consignacion else categ_lote_id
    if categ_id:
        vals["categ_id"] = int(categ_id)

    wh = warehouses.get(branch) or warehouses.get(PRIMARY_BRANCH)
    if wh:
        vals["warehouse_id"] = int(wh["warehouse_id"])
        vals["location_id"] = int(wh["location_id"])

    if not vals:
        return {"wrote": False}
    # Progressive field drop — warehouse_id / location_id may be Studio-only.
    attempt = dict(vals)
    last_exc: BaseException | None = None
    for drop in (
        (),
        ("location_id",),
        ("location_id", "warehouse_id"),
        ("location_id", "warehouse_id", "product_tag_ids"),
        ("location_id", "warehouse_id", "product_tag_ids", "categ_id"),
    ):
        for key in drop:
            attempt.pop(key, None)
        if not attempt:
            break
        try:
            client.execute_kw(
                "product.template", "write", [[int(product_id)], attempt]
            )
            return {
                "wrote": True,
                "tipo": TAG_CONSIGNACION if consignacion else TAG_LOTE,
                "branch": branch,
                "fields": sorted(attempt.keys()),
            }
        except Exception as exc:
            last_exc = exc
            attempt = dict(vals)
            continue
    return {"wrote": False, "error": str(last_exc) if last_exc else "no fields"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-snapshot", help="Catalog JSON instead of live scrape")
    parser.add_argument(
        "--save-snapshot",
        default="data/catalog_latest.json",
        help="Where to save a live scrape",
    )
    parser.add_argument("--limit", type=int, default=0, help="Max vehicles (0=all)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--no-archive",
        action="store_true",
        help="Skip archiving Odoo SKUs missing from catalog",
    )
    parser.add_argument(
        "--prices-only",
        action="store_true",
        help="Only sync list_price (skip tipo/warehouse writes)",
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--min-catalog",
        type=int,
        default=MIN_CATALOG,
        help=f"Abort archive if catalog smaller than this (default {MIN_CATALOG})",
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

    if len(vehicles) < int(args.min_catalog):
        print(f"ABORT: catalog too small ({len(vehicles)} < {args.min_catalog})")
        return 3

    client = OdooCRMClient()
    try:
        uid = client.authenticate()
        print(f"Odoo auth UID={uid} @ {client.url}")
    except Exception as exc:
        print(f"FAIL auth: {fault_message(exc)}")
        return 2

    tag_consign_id = ensure_product_tag(client, TAG_CONSIGNACION)
    tag_lote_id = ensure_product_tag(client, TAG_LOTE)
    categ_consign_id = ensure_category(client, CATEG_CONSIGNACION)
    categ_lote_id = ensure_category(client, CATEG_LOTE) or client.find_vehicle_category_id()
    warehouses = load_warehouses(client)
    print(
        f"Tags: consign={tag_consign_id} lote={tag_lote_id} | "
        f"categ consign={categ_consign_id} lote={categ_lote_id} | "
        f"warehouses={warehouses}"
    )

    created: list[dict] = []
    updated: list[dict] = []
    price_fixed: list[dict] = []
    typed: list[dict] = []
    skipped: list[str] = []
    errors: list[str] = []
    archived: list[dict] = []
    catalog_codes: set[str] = set()

    for index, vehicle in enumerate(vehicles, start=1):
        code = str(vehicle.autosell_id or "").strip()
        if not code:
            skipped.append(f"(no sku) {product_name(vehicle)}")
            continue
        catalog_codes.add(code)
        name = product_name(vehicle)
        label = f"{code} {name}"
        consign = is_consignment(vehicle.title)
        branch = branch_from_title(vehicle.title)
        try:
            price = parse_price(vehicle)
        except ValueError as exc:
            skipped.append(f"{label}: {exc}")
            print(f"[{index}/{len(vehicles)}] SKIP {label} ({exc})")
            continue

        if args.dry_run:
            existing = client.find_product_template(default_code=code, name=name)
            action = "would_update" if existing else "would_create"
            old_price = float(existing["list_price"]) if existing else None
            price_delta = (
                None if old_price is None else round(price - old_price, 2)
            )
            print(
                f"[{index}/{len(vehicles)}] DRY {action} {label} "
                f"price={price} delta={price_delta} "
                f"tipo={'Consignación' if consign else 'Lote'} branch={branch}"
            )
            if existing and price_delta not in (None, 0.0):
                price_fixed.append(
                    {
                        "id": int(existing["id"]),
                        "default_code": code,
                        "old": old_price,
                        "new": price,
                    }
                )
            continue

        try:
            result = client.upsert_vehicle_product(
                name=name,
                list_price=price,
                default_code=code,
                categ_id=categ_consign_id if consign else categ_lote_id,
                description=f"{vehicle.url}\n{vehicle.mileage} | {vehicle.version}".strip(),
                qty_available=1.0,
            )
        except Exception as exc:
            msg = f"{label}: {fault_message(exc)}"
            errors.append(msg)
            print(f"[{index}/{len(vehicles)}] ERR {msg}")
            continue

        bucket = created if result["action"] == "created" else updated
        bucket.append(result)
        if result["action"] == "updated":
            price_fixed.append(
                {
                    "id": result["id"],
                    "default_code": code,
                    "new": price,
                }
            )

        tipo_meta: dict[str, Any] = {"wrote": False}
        if not args.prices_only:
            tipo_meta = write_tipo_and_warehouse(
                client,
                int(result["id"]),
                consignacion=consign,
                branch=branch,
                tag_consign_id=tag_consign_id,
                tag_lote_id=tag_lote_id,
                categ_consign_id=categ_consign_id,
                categ_lote_id=categ_lote_id,
                warehouses=warehouses,
            )
            if tipo_meta.get("wrote"):
                typed.append({"id": result["id"], "code": code, **tipo_meta})

        print(
            f"[{index}/{len(vehicles)}] {result['action'].upper()} "
            f"id={result['id']} {name!r} list_price={price} "
            f"tipo={'Consignación' if consign else 'Lote'} "
            f"branch={branch} meta={tipo_meta.get('fields') or tipo_meta}"
        )

    do_archive = (
        not args.no_archive
        and not (args.limit and args.limit > 0)
        and len(catalog_codes) >= int(args.min_catalog)
    )
    if do_archive:
        try:
            active = client.list_active_vehicle_products(
                categ_id=None  # both consignación + vehiculos
            )
            # Also list by either category to avoid missing orphans
            orphans = [p for p in active if p["default_code"] not in catalog_codes]
            if args.dry_run:
                archived = orphans
                print(f"\nDRY archive orphans: {len(orphans)}")
                for product in orphans[:25]:
                    print(
                        f"  would_archive SKU {product['default_code']} "
                        f"id={product['id']} {product['name']!r}"
                    )
                if len(orphans) > 25:
                    print(f"  … +{len(orphans) - 25} more")
            else:
                archived = client.archive_orphan_vehicles(
                    catalog_codes, categ_id=None
                )
                print(f"\nArchived orphans: {len(archived)}")
                for product in archived[:25]:
                    print(
                        f"  archived SKU {product['default_code']} "
                        f"id={product['id']} {product['name']!r}"
                    )
        except Exception as exc:
            msg = f"orphan archive failed: {fault_message(exc)}"
            errors.append(msg)
            print(f"ERR {msg}")
    elif args.limit:
        print("\nSkip orphan archive (--limit set)")
    else:
        print("\nSkip orphan archive (--no-archive)")

    print("\n=== SUMMARY ===")
    print(f"catalog:       {len(vehicles)} (skus={len(catalog_codes)})")
    print(f"created:       {len(created)}")
    print(f"updated:       {len(updated)}")
    print(f"price touches: {len(price_fixed)}")
    print(f"tipo/wh writes:{len(typed)}")
    print(f"archived:      {len(archived)}")
    print(f"skipped:       {len(skipped)}")
    print(f"errors:        {len(errors)}")
    consign_n = sum(1 for v in vehicles if is_consignment(v.title))
    print(f"catalog tipo:  Consignación={consign_n} Lote={len(vehicles) - consign_n}")
    if errors[:5]:
        print("sample errors:")
        for err in errors[:5]:
            print(f"  - {err}")

    return 1 if errors and not (created or updated or archived) else 0


if __name__ == "__main__":
    raise SystemExit(main())
