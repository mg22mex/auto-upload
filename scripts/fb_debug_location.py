#!/usr/bin/env python3
"""Debug Ubicación combobox on Marketplace vehicle form."""
from __future__ import annotations

import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.facebook.photos import download_vehicle_photos
from src.facebook.session import get_page, is_logged_in, open_account_context
from src.facebook.ui import advance_past_photo_step, dismiss_overlays, wait_for_photo_previews
from src.inventory.snapshot import load_catalog_snapshot


def main() -> int:
    load_dotenv(ROOT / ".env")
    config = yaml.safe_load((ROOT / "config.yaml").read_text())
    vehicle = next(
        v for v in load_catalog_snapshot(ROOT / "data/catalog_latest.json")
        if v.autosell_id == "obj1126"
    )
    photos = download_vehicle_photos(vehicle, max_photos=1)
    with open_account_context(config, "account_3", root=ROOT, headless=True) as ctx:
        page = get_page(ctx)
        if not is_logged_in(page):
            print("Not logged in")
            return 1
        page.goto(config["facebook"]["create_url"], wait_until="domcontentloaded")
        page.wait_for_timeout(3_000)
        dismiss_overlays(page)
        page.locator('input[type="file"]').first.set_input_files([str(photos[0])])
        wait_for_photo_previews(page, min_count=1, timeout_ms=120_000)
        advance_past_photo_step(page, timeout_ms=90_000)
        page.wait_for_timeout(3_000)

        for selector in (
            '[role="combobox"][aria-label="Ubicación"]',
            '[role="combobox"][aria-label*="Ubicación" i]',
            '[role="combobox"][aria-label*="Location" i]',
        ):
            box = page.locator(selector)
            print(f"selector={selector!r} count={box.count()}")
            if box.count() == 0:
                continue
            target = box.first
            print(f"  visible={target.is_visible()} text={target.inner_text()!r}")
            html = target.evaluate("el => el.outerHTML.slice(0, 1200)")
            print(f"  html={html}")
            target.scroll_into_view_if_needed()
            target.click()
            page.wait_for_timeout(800)
            page.keyboard.press("Control+a")
            page.keyboard.type("Chihuahua", delay=40)
            page.wait_for_timeout(2_500)
            opts = page.locator('[role="option"]')
            texts: list[str] = []
            for i in range(min(opts.count(), 20)):
                try:
                    t = opts.nth(i).inner_text(timeout=500).strip()
                    if t:
                        texts.append(t)
                except Exception:
                    pass
            print(f"  options={texts}")
            print(f"  box after type={target.inner_text()!r}")
            if opts.count():
                opts.first.click()
                page.wait_for_timeout(1500)
                print(f"  box after pick={target.inner_text()!r}")
                state = page.evaluate(
                    """() => {
                      const out = [];
                      for (const el of document.querySelectorAll('[role="combobox"]')) {
                        out.push({
                          label: el.getAttribute('aria-label') || '',
                          value: (el.innerText || '').trim(),
                        });
                      }
                      return out;
                    }"""
                )
                print(f"  all comboboxes after pick={state}")
            # Try Enter key approach
            target.click()
            page.keyboard.press("Control+a")
            page.keyboard.type("Chihuahua", delay=40)
            page.wait_for_timeout(2000)
            page.keyboard.press("ArrowDown")
            page.wait_for_timeout(300)
            page.keyboard.press("Enter")
            page.wait_for_timeout(1500)
            state2 = page.evaluate(
                """() => {
                  const out = [];
                  for (const el of document.querySelectorAll('[role="combobox"]')) {
                    out.push({
                      label: el.getAttribute('aria-label') || '',
                      value: (el.innerText || '').trim(),
                    });
                  }
                  return out;
                }"""
            )
            print(f"  all comboboxes after Enter={state2}")
            break
    for p in photos:
        p.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
