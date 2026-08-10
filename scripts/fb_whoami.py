#!/usr/bin/env python3
"""Print which Facebook identity the Playwright session is logged in as (no secrets)."""
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

from src.facebook.session import get_page, is_logged_in, open_account_context, resolve_session_dir
from src.facebook.util import env_bool


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _text(page, selector: str) -> str | None:
    try:
        loc = page.locator(selector).first
        if loc.count() == 0:
            return None
        raw = (loc.inner_text(timeout=3_000) or "").strip()
        return raw or None
    except Exception:
        return None


def _href(page, selector: str) -> str | None:
    try:
        loc = page.locator(selector).first
        if loc.count() == 0:
            return None
        return loc.get_attribute("href")
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    config = load_config(ROOT / args.config)
    headless = not args.headed and env_bool(
        "FB_HEADLESS", config.get("facebook", {}).get("headless", True)
    )
    session_dir = resolve_session_dir(config, args.account, ROOT)

    with open_account_context(config, args.account, root=ROOT, headless=headless) as context:
        page = get_page(context)
        logged_in = is_logged_in(page)
        # Profile menus / me page — labels only (no tokens)
        display_name = None
        profile_href = None
        try:
            page.goto("https://www.facebook.com/me", wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2_500)
            profile_href = page.url
            display_name = _text(page, "h1") or _text(page, '[data-pagelet="ProfileTilesFeed"] h1')
            if not display_name:
                display_name = page.title().replace(" | Facebook", "").strip() or None
            # Numeric id from profile URL when present
            m = re.search(r"profile\.php\?id=(\d+)", profile_href or "")
            profile_id = m.group(1) if m else None
            if not profile_id:
                m2 = re.search(r"facebook\.com/([^/?#]+)", profile_href or "")
                profile_id = m2.group(1) if m2 and m2.group(1) not in {"me", "profile.php"} else None
        except Exception as exc:
            profile_id = None
            print(f"WARN: profile probe failed: {exc}", file=sys.stderr)

        # Cookie c_user is the FB user id for this session
        c_user = None
        try:
            for cookie in context.cookies("https://www.facebook.com"):
                if cookie.get("name") == "c_user":
                    c_user = cookie.get("value")
                    break
        except Exception:
            pass

        out = {
            "account_id": args.account,
            "session_dir": str(session_dir),
            "logged_in_marketplace": logged_in,
            "display_name": display_name,
            "profile_url": profile_href,
            "profile_slug_or_id": profile_id,
            "c_user": c_user,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if logged_in else 1


if __name__ == "__main__":
    raise SystemExit(main())
