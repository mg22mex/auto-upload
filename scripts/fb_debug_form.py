#!/usr/bin/env python3
"""Dump visible inputs on the Marketplace vehicle form (debug)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.facebook.session import get_page, is_logged_in, open_account_context
from src.facebook.ui import dismiss_overlays, log_page_state


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True)
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    config = load_config(ROOT / args.config)
    fb_config = config.get("facebook", {})

    with open_account_context(
        config, args.account, root=ROOT, headless=fb_config.get("headless", True)
    ) as context:
        page = get_page(context)
        if not is_logged_in(page):
            print("Not logged in", file=sys.stderr)
            return 1
        page.goto(fb_config.get("create_url"), wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(4_000)
        dismiss_overlays(page)
        log_page_state(page, "form_debug")
        fields = page.evaluate(
            """() => {
                const out = [];
                for (const el of document.querySelectorAll('input, textarea, [contenteditable="true"]')) {
                  const rect = el.getBoundingClientRect();
                  if (rect.width <= 0 || rect.height <= 0) continue;
                  out.push({
                    tag: el.tagName,
                    type: el.getAttribute('type'),
                    ariaLabel: el.getAttribute('aria-label'),
                    placeholder: el.getAttribute('placeholder'),
                    name: el.getAttribute('name'),
                    id: el.id,
                  });
                }
                return out;
            }"""
        )
        print(json.dumps(fields, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
