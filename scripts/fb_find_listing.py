#!/usr/bin/env python3
"""Find a listing URL on Marketplace dashboard/selling by vehicle id or title."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.facebook.poster import (
    _find_top_matching_listing,
    _verify_listing_url,
)
from src.facebook.session import get_page, is_logged_in, open_account_context
from src.inventory.snapshot import load_catalog_snapshot


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True)
    parser.add_argument("--autosell-id", required=True)
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    config = load_config(ROOT / args.config)
    vehicle = next(
        v
        for v in load_catalog_snapshot(ROOT / "data/catalog_latest.json")
        if v.autosell_id == args.autosell_id
    )

    with open_account_context(
        config, args.account, root=ROOT, headless=config.get("facebook", {}).get("headless", True)
    ) as context:
        page = get_page(context)
        if not is_logged_in(page):
            print("Not logged in", file=sys.stderr)
            return 1

        for listing_page in (
            "https://www.facebook.com/marketplace/you/dashboard",
            "https://www.facebook.com/marketplace/you/selling",
        ):
            page.goto(listing_page, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(4_000)
            url = _find_top_matching_listing(page, vehicle)
            if url and _verify_listing_url(page, url, vehicle):
                print(url)
                return 0

    print("Listing URL not found", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
