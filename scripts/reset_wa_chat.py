#!/usr/bin/env python3
"""Force WhatsApp qualification state back to AI_ACTIVE for a phone.

Clears sticky HANDOFF_TO_HUMAN so Beatriz / Vapi text-first resumes.

Examples:
  PYTHONPATH=. python scripts/reset_wa_chat.py --phone 5216143231198
  PYTHONPATH=. python scripts/reset_wa_chat.py --phone +526143231198 --instance autosell_san_felipe
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from src.whatsapp_worker.inbound import QualificationStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phone", required=True, help="WhatsApp E.164 / digits")
    parser.add_argument(
        "--instance",
        default=None,
        help="Limit to one Evolution instance (default: all matches)",
    )
    parser.add_argument(
        "--keep-appointment",
        action="store_true",
        help="Do not clear appointment_time",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Override WA_QUALIFICATION_DB_PATH",
    )
    args = parser.parse_args()

    store = QualificationStore(args.db) if args.db else QualificationStore()
    try:
        updated = store.reset_to_ai_active(
            args.phone,
            instance=args.instance,
            clear_appointment=not args.keep_appointment,
            create_if_missing=True,
        )
    finally:
        store.close()

    print(json.dumps({"ok": True, "phone": args.phone, "updated": updated}, indent=2))
    return 0 if updated else 1


if __name__ == "__main__":
    raise SystemExit(main())
