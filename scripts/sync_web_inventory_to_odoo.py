#!/usr/bin/env python3
"""Compare autosell.mx live catalog vs Odoo saleable stock; deprecate missing SKUs.

Non-destructive for accounting: only ``product.template`` writes
(``sale_ok=False`` + deprecation note). Never creates ``account.move``.

Usage::

  python scripts/sync_web_inventory_to_odoo.py --dry-run
  python scripts/sync_web_inventory_to_odoo.py
  python scripts/sync_web_inventory_to_odoo.py --from-snapshot data/catalog_latest.json
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

from src.inventory.autosell import AutosellCatalogError, fetch_catalog  # noqa: E402
from src.inventory.snapshot import load_catalog_snapshot, save_catalog_snapshot  # noqa: E402
from src.odoo_sync.client import OdooCRMClient, OdooCRMError  # noqa: E402

# Abort deprecate if scrape looks truncated (protects against site outages).
MIN_CATALOG_FOR_DEPRECATE = 5


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


def load_web_skus(args: argparse.Namespace, config: dict) -> tuple[set[str], int]:
    """Return (sku set, vehicle count) from live scrape or snapshot."""
    if args.from_snapshot:
        path = Path(args.from_snapshot)
        print(f"Loading snapshot: {path}")
        vehicles = load_catalog_snapshot(path)
    else:
        print("Scraping live catalog from autosell.mx ...")
        vehicles = fetch_catalog(config)
        out = Path(args.save_snapshot)
        save_catalog_snapshot(vehicles, out)
        print(f"Scraped {len(vehicles)} vehicles → {out}")

    skus = {
        str(v.autosell_id).strip()
        for v in vehicles
        if str(getattr(v, "autosell_id", "") or "").strip()
    }
    return skus, len(vehicles)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from-snapshot",
        help="Use catalog JSON instead of live scrape",
    )
    parser.add_argument(
        "--save-snapshot",
        default="data/catalog_latest.json",
        help="Where to save a live scrape (default: data/catalog_latest.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Diff only — no Odoo writes",
    )
    parser.add_argument(
        "--min-catalog",
        type=int,
        default=MIN_CATALOG_FOR_DEPRECATE,
        help=f"Abort deprecate if web catalog smaller than this (default {MIN_CATALOG_FOR_DEPRECATE})",
    )
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    config_path = ROOT / args.config
    with config_path.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}

    try:
        web_skus, web_count = load_web_skus(args, config)
    except Exception as exc:
        print(f"FAIL catalog: {fault_message(exc)}")
        return 1

    print(f"Web catalog: {web_count} vehicles, {len(web_skus)} unique SKUs")
    if web_count < int(args.min_catalog) or len(web_skus) < int(args.min_catalog):
        print(
            f"ABORT deprecate: catalog too small "
            f"(vehicles={web_count} skus={len(web_skus)} < min={args.min_catalog})"
        )
        return 3

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
        odoo_rows = client.list_saleable_web_products(categ_id=categ_id)
    except Exception as exc:
        # Category filter may fail on some DBs — retry without categ.
        if categ_id is not None:
            print(f"WARN list with categ failed ({fault_message(exc)}); retry all sale_ok")
            odoo_rows = client.list_saleable_web_products(categ_id=None)
        else:
            print(f"FAIL list Odoo saleable: {fault_message(exc)}")
            return 4

    print(f"Odoo sale_ok+active with SKU: {len(odoo_rows)}")

    missing = [p for p in odoo_rows if p["default_code"] not in web_skus]
    still_live = len(odoo_rows) - len(missing)
    print(f"Still on web: {still_live}")
    print(f"Missing from web (to deprecate): {len(missing)}")

    deprecated: list[dict] = []
    errors: list[str] = []

    for product in missing:
        label = (
            f"id={product['id']} SKU={product['default_code']} "
            f"{product['name']!r} ${product['list_price']:.0f}"
        )
        if args.dry_run:
            print(f"  DRY would_deprecate sale_ok=False {label}")
            deprecated.append(product)
            continue
        try:
            result = client.deprecate_web_missing_product(product)
            deprecated.append(result)
            print(
                f"  DEPRECATE {label} "
                f"note_field={result.get('note_field')}"
            )
        except Exception as exc:
            msg = f"{label}: {fault_message(exc)}"
            errors.append(msg)
            print(f"  ERR {msg}")

    print("\n=== SUMMARY ===")
    print(f"web_skus:     {len(web_skus)}")
    print(f"odoo_saleable:{len(odoo_rows)}")
    print(f"deprecated:   {len(deprecated)}" + (" (dry-run)" if args.dry_run else ""))
    print(f"errors:       {len(errors)}")
    if deprecated[:10]:
        print(
            "sample:",
            ", ".join(
                str(p.get("default_code") or p.get("id")) for p in deprecated[:10]
            ),
        )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
