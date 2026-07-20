#!/usr/bin/env python3
"""Probe renew UI for one listing (debug)."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.facebook.session import get_page, is_logged_in, open_account_context
from src.facebook.util import ensure_log_dir
from src.inventory.snapshot import load_catalog_snapshot
from src.store.db import SyncStore


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True)
    parser.add_argument("--autosell-id", required=True)
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    config = load_config(ROOT / "config.yaml")
    vehicle = next(
        v for v in load_catalog_snapshot(ROOT / "data/catalog_latest.json") if v.autosell_id == args.autosell_id
    )
    store = SyncStore(ROOT / "data/sync.db")
    row = store.get_fb_listing(args.autosell_id, args.account)
    out = ensure_log_dir(ROOT / "data/logs/facebook/relist_explore")
    needle = vehicle.title or vehicle.brand

    with open_account_context(config, args.account, root=ROOT, headless=True) as context:
        page = get_page(context)
        if not is_logged_in(page):
            return 1

        # Selling list
        page.goto("https://www.facebook.com/marketplace/you/selling", wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        page.screenshot(path=str(out / f"{args.autosell_id}_probe_selling.png"))
        found = page.get_by_text(re.compile(re.escape(needle), re.I)).count()
        print(f"selling text matches for {needle!r}: {found}")

        if found:
            page.get_by_text(re.compile(re.escape(needle), re.I)).first.click()
            page.wait_for_timeout(2500)
            page.screenshot(path=str(out / f"{args.autosell_id}_probe_after_click.png"))
            items = page.evaluate(
                """() => [...document.querySelectorAll('button,[role="button"],[role="menuitem"]')]
                  .filter(el => el.getBoundingClientRect().width>0)
                  .map(el => ({t:(el.innerText||el.getAttribute('aria-label')||'').slice(0,80),
                               a:el.getAttribute('aria-label')||'',
                               d:el.getAttribute('aria-disabled')}))
                  .filter(x => /renew|renovar|more|más|sold|vendido/i.test(x.t+x.a))
                """
            )
            print("after click candidates:", json.dumps(items, indent=2, ensure_ascii=False))

        # Renew dialog
        page.goto(
            "https://www.facebook.com/marketplace/selling/renew_listings/?is_routable_dialog=true",
            wait_until="domcontentloaded",
        )
        page.wait_for_timeout(4000)
        page.screenshot(path=str(out / f"{args.autosell_id}_probe_renew_dialog.png"))
        body = page.locator("body").inner_text(timeout=5000)[:1500]
        print("renew dialog snippet:\n", body[:800])

    print(f"URL in db: {row['fb_listing_url'] if row else None}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
