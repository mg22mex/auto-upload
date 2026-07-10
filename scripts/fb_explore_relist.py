#!/usr/bin/env python3
"""Discover Facebook Marketplace relist/repost UI for fast-relist implementation.

Run headed on an account with live listings:

  python scripts/fb_explore_relist.py --account account_1 --autosell-id obj969 --headed

Outputs screenshots + JSON under data/logs/facebook/relist_explore/
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.facebook.session import get_page, is_logged_in, open_account_context
from src.facebook.util import ensure_log_dir
from src.inventory.snapshot import load_catalog_snapshot
from src.store.db import SyncStore

RELIST_KEYWORDS = re.compile(
    r"repost|relist|re-list|duplicate|sell again|vender|publicar|"
    r"renovar|renew|copiar|duplicate|otra vez|again|bump|renovar",
    re.I,
)


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _collect_interactives(page) -> list[dict]:
    """Visible buttons, menuitems, links with text."""
    try:
        items = page.evaluate(
            """() => {
              const out = [];
              const sel = 'button, [role="button"], [role="menuitem"], [role="link"], a';
              for (const el of document.querySelectorAll(sel)) {
                const rect = el.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) continue;
                const text = (el.innerText || el.getAttribute('aria-label') || '').trim();
                if (!text || text.length > 200) continue;
                out.push({
                  tag: el.tagName,
                  role: el.getAttribute('role') || '',
                  ariaLabel: el.getAttribute('aria-label') || '',
                  text: text.slice(0, 120),
                  href: el.getAttribute('href') || '',
                });
              }
              return out;
            }"""
        )
        return items or []
    except Exception as exc:
        return [{"error": str(exc)}]


def _filter_relist_candidates(items: list[dict]) -> list[dict]:
    out = []
    for item in items:
        blob = " ".join(
            str(item.get(k, "")) for k in ("text", "ariaLabel", "href")
        )
        if RELIST_KEYWORDS.search(blob):
            out.append(item)
    return out


def _save(page, out_dir: Path, name: str) -> None:
    path = out_dir / f"{name}.png"
    try:
        page.screenshot(path=str(path), full_page=False)
        print(f"  screenshot: {path}")
    except Exception as exc:
        print(f"  screenshot failed ({name}): {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Explore FB relist UI for fast-relist design.")
    parser.add_argument("--account", required=True)
    parser.add_argument("--autosell-id", required=True)
    parser.add_argument("--catalog", default="data/catalog_latest.json")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    config = load_config(ROOT / args.config)
    fb_config = config.get("facebook", {})
    headless = not args.headed and env_bool("FB_HEADLESS", fb_config.get("headless", True))

    vehicles = load_catalog_snapshot(ROOT / args.catalog)
    vehicle = next((v for v in vehicles if v.autosell_id == args.autosell_id), None)
    if vehicle is None:
        print(f"Not in catalog: {args.autosell_id}", file=sys.stderr)
        return 1

    db_path = os.getenv("DB_PATH", "data/sync.db")
    store = SyncStore(ROOT / db_path if not Path(db_path).is_absolute() else db_path)
    row = store.get_fb_listing(args.autosell_id, args.account)
    if not row or not row["fb_listing_url"]:
        print(f"No live URL in sync.db for {args.autosell_id} on {args.account}", file=sys.stderr)
        return 1

    listing_url = row["fb_listing_url"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = ensure_log_dir(ROOT / "data" / "logs" / "facebook" / "relist_explore")
    run_dir = out_dir / f"{args.autosell_id}_{args.account}_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    report: dict = {
        "autosell_id": args.autosell_id,
        "account": args.account,
        "listing_url": listing_url,
        "vehicle": vehicle.marketplace_title,
        "pages": {},
    }

    print(f"Output: {run_dir}")

    with open_account_context(config, args.account, root=ROOT, headless=headless) as context:
        page = get_page(context)
        if not is_logged_in(page):
            print("Not logged in", file=sys.stderr)
            return 1

        # --- Listing detail page ---
        print("Opening listing...")
        page.goto(listing_url, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(3_000)
        _save(page, run_dir, "01_listing_page")
        items = _collect_interactives(page)
        report["pages"]["listing"] = {
            "url": page.url,
            "all_interactives_count": len(items),
            "relist_candidates": _filter_relist_candidates(items),
        }

        # Try opening ⋮ / more menu
        for pattern in (
            re.compile(r"more|más|opciones|options", re.I),
            re.compile(r"manage|administrar|gestionar", re.I),
        ):
            btn = page.get_by_role("button", name=pattern)
            if btn.count() and btn.first.is_visible():
                print(f"  clicking menu: {pattern.pattern}")
                btn.first.click()
                page.wait_for_timeout(2_000)
                _save(page, run_dir, "02_listing_menu_open")
                menu_items = _collect_interactives(page)
                report["pages"]["listing_menu"] = {
                    "trigger": pattern.pattern,
                    "relist_candidates": _filter_relist_candidates(menu_items),
                    "all_menu_items": menu_items[:40],
                }
                page.keyboard.press("Escape")
                page.wait_for_timeout(1_000)
                break

        # --- Dashboard / selling ---
        for label, url in (
            ("selling", "https://www.facebook.com/marketplace/you/selling"),
            ("dashboard", "https://www.facebook.com/marketplace/you/dashboard"),
        ):
            print(f"Opening {label}...")
            page.goto(url, wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_timeout(4_000)
            _save(page, run_dir, f"03_{label}")
            items = _collect_interactives(page)
            report["pages"][label] = {
                "url": page.url,
                "relist_candidates": _filter_relist_candidates(items),
                "sample_items": items[:30],
            }

        # Scroll dashboard for lazy-loaded listing actions
        page.mouse.wheel(0, 1500)
        page.wait_for_timeout(2_000)
        _save(page, run_dir, "04_dashboard_scrolled")
        items = _collect_interactives(page)
        report["pages"]["dashboard_scrolled"] = {
            "relist_candidates": _filter_relist_candidates(items),
        }

    report_path = run_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nReport: {report_path}")

    print("\n=== Relist keyword matches ===")
    for page_name, data in report["pages"].items():
        cands = data.get("relist_candidates", [])
        if cands:
            print(f"\n{page_name}:")
            for c in cands[:15]:
                print(f"  - [{c.get('role')}] {c.get('text') or c.get('ariaLabel')}")

    if not any(d.get("relist_candidates") for d in report["pages"].values()):
        print("\nNo relist keyword matches found.")
        print("Next: run with --headed, manually open the extension UI, compare labels.")
        print("Or capture DevTools Network during extension repost.")

    print("\nFill discovered labels in docs/FAST_RELIST_PLAN.md section 4.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
