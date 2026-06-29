#!/usr/bin/env python3
"""Upload one photo to FB create flow and save debug screenshots (no publish)."""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.facebook.photos import BROWSER_HEADERS, vehicle_image_urls
from src.facebook.session import get_page, is_logged_in, open_account_context
from src.facebook.ui import dismiss_overlays, log_page_state
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True)
    parser.add_argument("--autosell-id", default="obj969")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    config = load_config(ROOT / args.config)
    fb_config = config.get("facebook", {})
    headless = not args.headed and env_bool("FB_HEADLESS", fb_config.get("headless", True))
    log_dir = ROOT / "data" / "logs" / "facebook"
    log_dir.mkdir(parents=True, exist_ok=True)

    vehicle = next(
        v
        for v in load_catalog_snapshot(ROOT / "data/catalog_latest.json")
        if v.autosell_id == args.autosell_id
    )
    url = vehicle_image_urls(vehicle)[0]
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)
    response = session.get(url, headers={"Referer": vehicle.url}, timeout=60)
    response.raise_for_status()
    tmp = Path(tempfile.mkdtemp()) / "test.jpg"
    tmp.write_bytes(response.content)

    with open_account_context(config, args.account, root=ROOT, headless=headless) as context:
        page = get_page(context)
        if not is_logged_in(page):
            print("Not logged in", file=sys.stderr)
            return 1
        page.goto(fb_config.get("create_url"), wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(3_000)
        dismiss_overlays(page)
        log_page_state(page, "opened")
        page.screenshot(path=str(log_dir / f"{args.autosell_id}_debug_01_open.png"), full_page=True)

        file_input = page.locator('input[type="file"]').first
        if file_input.count() == 0:
            print("No file input found", file=sys.stderr)
            page.screenshot(path=str(log_dir / f"{args.autosell_id}_debug_no_input.png"), full_page=True)
            return 1

        file_input.set_input_files([str(tmp)])
        elapsed = 0
        for extra in (5, 10, 15, 30, 60):
            page.wait_for_timeout(extra * 1000)
            elapsed += extra
            shot = log_dir / f"{args.autosell_id}_debug_after_{elapsed}s.png"
            page.screenshot(path=str(shot), full_page=True)
            print(f"Saved {shot}")
            labels = page.evaluate(
                """() => [...document.querySelectorAll('[aria-label]')]
                  .map(n => n.getAttribute('aria-label'))
                  .filter(Boolean)"""
            )
            print(f"aria-labels @ {elapsed}s:", labels[:30])

    tmp.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
