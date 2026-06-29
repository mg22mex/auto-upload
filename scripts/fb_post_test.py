#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.facebook.errors import FacebookPostingError, FacebookSessionError
from src.facebook.poster import create_vehicle_listing
from src.facebook.session import get_page, is_logged_in, open_account_context
from src.facebook.util import ensure_log_dir
from src.inventory.snapshot import load_catalog_snapshot


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Post one catalog vehicle to Facebook (manual test).")
    parser.add_argument("--account", required=True)
    parser.add_argument("--autosell-id", required=True, help="e.g. obj969")
    parser.add_argument("--catalog", default="data/catalog_latest.json")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument(
        "--max-photos",
        type=int,
        default=None,
        help="Limit photos for this test (default: MAX_PHOTOS_PER_LISTING or config)",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    config = load_config(ROOT / args.config)
    fb_config = config.get("facebook", {})
    headless = not args.headed and env_bool("FB_HEADLESS", fb_config.get("headless", True))
    max_photos = args.max_photos if args.max_photos is not None else int(
        os.getenv("MAX_PHOTOS_PER_LISTING", fb_config.get("max_photos_per_listing", 20))
    )

    vehicles = load_catalog_snapshot(ROOT / args.catalog)
    vehicle = next((v for v in vehicles if v.autosell_id == args.autosell_id), None)
    if vehicle is None:
        print(f"Vehicle not found in catalog: {args.autosell_id}", file=sys.stderr)
        return 1

    log_dir = ensure_log_dir(ROOT / "data" / "logs" / "facebook")

    try:
        with open_account_context(config, args.account, root=ROOT, headless=headless) as context:
            page = get_page(context)
            if not is_logged_in(page):
                raise FacebookSessionError(
                    f"Not logged in for {args.account}. Run: python scripts/fb_login.py --account {args.account}"
                )
            url = create_vehicle_listing(
                page,
                vehicle,
                fb_config=fb_config,
                max_photos=max_photos,
                log_dir=log_dir,
            )
            print(f"Posted: {url}")
    except (FacebookSessionError, FacebookPostingError) as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
