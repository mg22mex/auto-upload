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

from src.facebook.session import get_page, is_logged_in, open_account_context


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a saved Facebook Playwright session.")
    parser.add_argument("--account", required=True)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--headed", action="store_true", help="Open a visible browser")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    config = load_config(ROOT / args.config)
    headless = not args.headed and env_bool("FB_HEADLESS", config.get("facebook", {}).get("headless", True))

    with open_account_context(config, args.account, root=ROOT, headless=headless) as context:
        page = get_page(context)
        ok = is_logged_in(page)
        print(f"Logged in: {ok}")
        print(f"URL: {page.url}")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
