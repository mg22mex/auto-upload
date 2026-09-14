#!/usr/bin/env python3
"""Live (or dry-run) Vapi outbound call smoke test.

Uses ``VAPI_API_KEY`` + ``VAPI_PHONE_NUMBER_ID`` (caller +52 614 227 4381).

Examples:
  # Offline-safe dry run (default when VOICE_OUTBOUND_DRY_RUN unset/true)
  PYTHONPATH=. python scripts/test_vapi_outbound.py --to +52614XXXXXXX

  # Real dial
  VOICE_OUTBOUND_DRY_RUN=false PYTHONPATH=. python scripts/test_vapi_outbound.py \\
      --to +52614XXXXXXX --live
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from src.lead_routing import build_voice_agent_script, QuoteVoiceContext  # noqa: E402
from src.voice_gateway.config import (  # noqa: E402
    DOCUMENTED_CALLER_E164,
    load_vapi_config,
    voice_outbound_dry_run,
)
from src.voice_gateway.dialer import place_vapi_outbound_call  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--to",
        required=True,
        help="Customer E.164 / MX number to dial (e.g. +526141234567)",
    )
    parser.add_argument("--lead-id", type=int, default=1937)
    parser.add_argument("--vehicle", default="Camioneta")
    parser.add_argument("--valuation", default="194500.00")
    parser.add_argument("--monthly", default="9212.13")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Force dry_run=false (also set VOICE_OUTBOUND_DRY_RUN=false)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Force dry_run=true even if env says otherwise",
    )
    args = parser.parse_args()

    cfg = load_vapi_config()
    dry = True if args.dry_run else (False if args.live else voice_outbound_dry_run())

    print("=== Vapi outbound smoke test ===", flush=True)
    print(f"caller_e164 (documented): {DOCUMENTED_CALLER_E164}", flush=True)
    print(f"phoneNumberId set: {bool(cfg.phone_number_id)}", flush=True)
    print(f"api_key set: {bool(cfg.api_key)}", flush=True)
    print(f"assistant_id set: {bool(cfg.assistant_id)}", flush=True)
    print(f"dry_run: {dry}", flush=True)
    print(f"to: {args.to}", flush=True)

    if not dry and not cfg.configured:
        print(
            "ERROR: VAPI_API_KEY and VAPI_PHONE_NUMBER_ID required for --live",
            file=sys.stderr,
            flush=True,
        )
        return 2

    script = build_voice_agent_script(
        QuoteVoiceContext(
            lead_id=args.lead_id,
            phone=args.to,
            vehicle_of_interest=args.vehicle,
            valuation_amount=args.valuation,
            monthly_payment=args.monthly,
        )
    )
    print("\n=== agent script ===", flush=True)
    print(script, flush=True)

    if args.live:
        os.environ["VOICE_OUTBOUND_DRY_RUN"] = "false"

    result = place_vapi_outbound_call(
        customer_phone=args.to,
        lead_id=args.lead_id,
        vehicle_of_interest=args.vehicle,
        valuation_amount=args.valuation,
        monthly_payment=args.monthly,
        agent_script=script,
        dry_run=dry,
    )
    print("\n=== result ===", flush=True)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
