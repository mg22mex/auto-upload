#!/usr/bin/env python3
"""Local webhook smoke before systemd — Meta verify GET + finance POST.

Default: in-process FastAPI TestClient.
  - Real quote_engine + Odoo (from .env)
  - Graph API MessengerClient mocked (no live Page reply)

Against a running daemon:
  python scripts/test_webhook_local.py --http http://127.0.0.1:8080

Usage:
  python scripts/test_webhook_local.py
  python scripts/test_webhook_local.py --http http://127.0.0.1:8080
  python scripts/test_webhook_local.py --skip-odoo   # parse/verify only
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")


SAMPLE_POST = {
    "object": "page",
    "entry": [
        {
            "messaging": [
                {
                    "sender": {"id": "PSID-LOCAL-TEST"},
                    "message": {
                        "text": "Cotízame este vehículo a 36 meses",
                        "quick_reply": {
                            "payload": (
                                '{"vehicle_name":"Mazda CX-5 2020",'
                                '"vehicle_price":300000,'
                                '"customer_name":"Prueba Webhook Local"}'
                            )
                        },
                    },
                }
            ]
        }
    ],
}


def _check_env(*, need_odoo: bool, need_verify: bool) -> list[str]:
    missing: list[str] = []
    if need_verify and not os.getenv("FB_VERIFY_TOKEN", "").strip():
        missing.append("FB_VERIFY_TOKEN")
    if need_odoo:
        for key in ("ODOO_URL", "ODOO_DB"):
            if not os.getenv(key, "").strip():
                missing.append(key)
        if not (
            os.getenv("ODOO_USERNAME", "").strip()
            or os.getenv("ODOO_USER", "").strip()
        ):
            missing.append("ODOO_USERNAME|ODOO_USER")
        if not (
            os.getenv("ODOO_API_KEY", "").strip()
            or os.getenv("ODOO_PASSWORD", "").strip()
        ):
            missing.append("ODOO_API_KEY|ODOO_PASSWORD")
    return missing


def _run_http(base: str, verify_token: str) -> int:
    import requests

    base = base.rstrip("/")
    health = requests.get(f"{base}/health", timeout=10)
    print(f"GET /health → {health.status_code} {health.text}")
    if health.status_code != 200:
        return 1

    verify = requests.get(
        f"{base}/webhook/facebook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": verify_token,
            "hub.challenge": "challenge-local-test",
        },
        timeout=10,
    )
    print(f"GET /webhook/facebook → {verify.status_code} body={verify.text!r}")
    if verify.status_code != 200 or verify.text != "challenge-local-test":
        return 1

    post = requests.post(f"{base}/webhook/facebook", json=SAMPLE_POST, timeout=60)
    print(f"POST /webhook/facebook → {post.status_code}")
    print(post.text[:800])
    if post.status_code != 200:
        return 1
    body = post.json()
    results = body.get("results") or []
    if not results:
        print("WARN: no messaging events processed")
        return 1
    status = results[0].get("status")
    print(f"event status={status} lead_id={results[0].get('lead_id')}")
    return 0 if status in {"quoted", "needs_vehicle", "needs_price", "ignored"} else 1


def _run_inprocess(*, skip_odoo: bool) -> int:
    from fastapi.testclient import TestClient

from src.meta_gateway.gateway import MetaWebhookGateway
from src.odoo_sync.client import OdooCRMClient, QuoteLeadResult
from src.quote_engine.engine import CalibratedQuoteEngine
from src.voice_gateway.webhook import create_app

    verify_token = os.getenv("FB_VERIFY_TOKEN", "").strip() or "local-dev-verify"
    messenger = MagicMock()
    messenger.send_text_message.return_value = {"message_id": "mid.local"}

    if skip_odoo:
        odoo = MagicMock()
        odoo.authenticate.return_value = 1
        odoo.create_or_update_lead.return_value = QuoteLeadResult(lead_id=999)
        odoo.post_quote_to_chatter.return_value = 1001
        odoo.search_vehicle_inventory.return_value = []
    else:
        odoo = OdooCRMClient()

    gateway = MetaWebhookGateway(
        verify_token=verify_token,
        quote_engine=CalibratedQuoteEngine(),
        odoo=odoo,
        messenger=messenger,
    )
    client = TestClient(create_app(meta_gateway=gateway))

    health = client.get("/health")
    print(f"GET /health → {health.status_code} {health.json()}")
    if health.status_code != 200:
        return 1

    verify = client.get(
        "/webhook/facebook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": verify_token,
            "hub.challenge": "challenge-local-test",
        },
    )
    print(f"GET /webhook/facebook → {verify.status_code} body={verify.text!r}")
    if verify.status_code != 200 or verify.text != "challenge-local-test":
        return 1

    post = client.post("/webhook/facebook", json=SAMPLE_POST)
    print(f"POST /webhook/facebook → {post.status_code}")
    print(post.text[:800])
    if post.status_code != 200:
        return 1
    body = post.json()
    results = body.get("results") or []
    if not results:
        print("FAIL: empty results")
        return 1
    row = results[0]
    print(
        f"event status={row.get('status')} lead_id={row.get('lead_id')} "
        f"monthly={row.get('estimated_monthly_payment')}"
    )
    if row.get("status") == "error":
        print(f"FAIL: {row.get('error')}")
        return 1
    if not skip_odoo and row.get("status") == "quoted" and not row.get("lead_id"):
        print("FAIL: quoted without lead_id")
        return 1
    if messenger.send_text_message.called:
        print("Graph reply mocked OK (send_text_message called)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test Autosell webhook routes")
    parser.add_argument(
        "--http",
        metavar="BASE_URL",
        help="Hit a running server (e.g. http://127.0.0.1:8080)",
    )
    parser.add_argument(
        "--skip-odoo",
        action="store_true",
        help="Mock Odoo (in-process only); still runs quote + verify",
    )
    args = parser.parse_args()

    if args.http:
        missing = _check_env(need_odoo=False, need_verify=True)
        if missing:
            print(f"Missing env for HTTP verify: {', '.join(missing)}", file=sys.stderr)
            return 1
        token = os.getenv("FB_VERIFY_TOKEN", "").strip()
        return _run_http(args.http, token)

    missing = _check_env(need_odoo=not args.skip_odoo, need_verify=False)
    if missing:
        print(f"Missing env: {', '.join(missing)}", file=sys.stderr)
        print("Set them in .env or pass --skip-odoo", file=sys.stderr)
        return 1
    return _run_inprocess(skip_odoo=args.skip_odoo)


if __name__ == "__main__":
    raise SystemExit(main())
