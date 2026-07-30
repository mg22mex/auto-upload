#!/usr/bin/env python3
"""Sync autosell.mx catalog → Odoo product.template inventory.

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

    created: list[dict] = []
    updated: list[dict] = []
    skipped: list[str] = []
    errors: list[str] = []

    for index, vehicle in enumerate(vehicles, start=1):
        label = f"{vehicle.autosell_id} {product_name(vehicle)}"
        try:
            price = parse_price(vehicle)
        except ValueError as exc:
            skipped.append(f"{label}: {exc}")
            print(f"[{index}/{len(vehicles)}] SKIP {label} ({exc})")
            continue

        if args.dry_run:
            existing = client.find_product_template(
                default_code=vehicle.autosell_id,
                name=product_name(vehicle),
            )
            action = "would_update" if existing else "would_create"
            print(
                f"[{index}/{len(vehicles)}] DRY {action} "
                f"{label} price={price}"
            )
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
        print(
            f"[{index}/{len(vehicles)}] {result['action'].upper()} "
            f"id={result['id']} {result['name']!r} "
            f"list_price={result['list_price']}"
        )

    print("\n=== SUMMARY ===")
    print(f"catalog:  {len(vehicles)}")
    print(f"created:  {len(created)}")
    print(f"updated:  {len(updated)}")
    print(f"skipped:  {len(skipped)}")
    print(f"errors:   {len(errors)}")
    if created[:5]:
        print("sample created:", ", ".join(str(r["id"]) for r in created[:5]))
    if updated[:5]:
        print("sample updated:", ", ".join(str(r["id"]) for r in updated[:5]))
    if errors[:5]:
        print("sample errors:")
        for err in errors[:5]:
            print(f"  - {err}")

    return 1 if errors and not (created or updated) else 0


if __name__ == "__main__":
    raise SystemExit(main())
