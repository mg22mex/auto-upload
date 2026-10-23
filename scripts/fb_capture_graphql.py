#!/usr/bin/env python3
"""Capture Facebook Marketplace GraphQL doc_id + variables from a live session.

Opens the account Chromium profile, navigates Marketplace, and records POSTs to
``/api/graphql/``. Save templates for GraphQL create/delete:

  python scripts/fb_capture_graphql.py --account account_1 --headed \\
      --save-as delete_listing --match-var listing_id

  python scripts/fb_capture_graphql.py --account account_1 --headed \\
      --save-as create_vehicle --match-name create

  # Optional: active selling list (better than HTML scrape for purge)
  python scripts/fb_capture_graphql.py --account account_1 --headed \\
      --save-as selling_active --match-name SellingFastActive

Then enable:

  FB_USE_GRAPHQL=1

Example shapes: ``src/facebook/graphql/examples/*.example.json``.
Never commit real cookies or full captured variable blobs with secrets.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.facebook.graphql.session_ctx import extract_session_context
from src.facebook.graphql.templates import save_captured_template
from src.facebook.session import get_page, is_logged_in, open_account_context
from src.facebook.util import env_bool


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument(
        "--seconds",
        type=int,
        default=180,
        help="How long to listen for GraphQL traffic (default 180)",
    )
    parser.add_argument(
        "--save-as",
        default="",
        help="Template basename under data/graphql/ (e.g. delete_listing)",
    )
    parser.add_argument(
        "--match-var",
        default="",
        help="Keep requests whose variables JSON contains this substring",
    )
    parser.add_argument(
        "--match-name",
        default="",
        help="Keep requests whose friendly name / body contains this (case-insensitive)",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Print captured doc_ids; do not write a template",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    config = load_config(ROOT / args.config)
    fb_config = config.get("facebook", {})
    headless = not args.headed and env_bool(
        "FB_HEADLESS", bool(fb_config.get("headless", True))
    )

    captured: list[dict] = []

    def on_request(request) -> None:  # type: ignore[no-untyped-def]
        try:
            url = request.url or ""
            if "/api/graphql" not in url:
                return
            if request.method.upper() != "POST":
                return
            post = request.post_data or ""
            params = parse_qs(post, keep_blank_values=True)
            doc_id = (params.get("doc_id") or [""])[0]
            variables = (params.get("variables") or ["{}"])[0]
            friendly = (params.get("fb_api_req_friendly_name") or [""])[0]
            if not doc_id:
                return
            if args.match_var and args.match_var not in variables:
                return
            if args.match_name:
                blob = f"{friendly} {post}".lower()
                if args.match_name.lower() not in blob:
                    return
            entry = {
                "doc_id": doc_id,
                "friendly_name": friendly,
                "variables_preview": variables[:500],
                "variables": variables,
                "ts": time.time(),
            }
            captured.append(entry)
            print(
                f"📡 doc_id={doc_id} name={friendly or '?'} "
                f"vars={variables[:120]!r}",
                flush=True,
            )
        except Exception as exc:
            print(f"WARN: capture listener: {exc}", flush=True)

    print(
        f"Listening {args.seconds}s for /api/graphql/ on {args.account}. "
        "Perform delete/create in the browser window…",
        flush=True,
    )
    with open_account_context(
        config, args.account, root=ROOT, headless=headless
    ) as context:
        page = get_page(context)
        if not is_logged_in(page):
            print("Not logged in — run scripts/fb_login.py", file=sys.stderr)
            return 1
        session = extract_session_context(
            context, page=page, account_id=args.account, fetch_tokens=True
        )
        print(f"Session OK: {session.redacted_summary()}", flush=True)
        page.on("request", on_request)
        page.goto(
            "https://www.facebook.com/marketplace/you/selling",
            wait_until="domcontentloaded",
            timeout=90_000,
        )
        deadline = time.time() + max(5, args.seconds)
        while time.time() < deadline:
            page.wait_for_timeout(1000)

    print(f"Captured {len(captured)} GraphQL POST(s)", flush=True)
    if args.list_only or not args.save_as:
        for entry in captured:
            print(
                json.dumps(
                    {
                        "doc_id": entry["doc_id"],
                        "friendly_name": entry["friendly_name"],
                        "variables_preview": entry["variables_preview"],
                    },
                    ensure_ascii=False,
                )
            )
        if not args.save_as:
            print(
                "Tip: re-run with --save-as delete_listing --match-var <item_id>",
                flush=True,
            )
        return 0

    if not captured:
        print("Nothing matched — widen --match-* or perform the UI action", file=sys.stderr)
        return 2

    last = captured[-1]
    path = save_captured_template(
        args.save_as,
        doc_id=last["doc_id"],
        variables=last["variables"],
        root=ROOT,
        friendly_name=last.get("friendly_name") or "",
        meta={"account": args.account, "note": "review placeholders before commit"},
    )
    print(f"Wrote {path}", flush=True)
    print(
        "Edit placeholders (item_id → {{item_id}}, photo ids → {{photo_ids}}, …) "
        "then set FB_USE_GRAPHQL=1",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
