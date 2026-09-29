#!/usr/bin/env python3
"""Poll marketing@ IMAP for Autosell.mx webform leads → Odoo + Beatriz WA.

Usage::

  PYTHONPATH=. .venv/bin/python scripts/parse_web_leads.py
  PYTHONPATH=. .venv/bin/python scripts/parse_web_leads.py --dry-run
  PYTHONPATH=. .venv/bin/python scripts/parse_web_leads.py --file sample.eml
  PYTHONPATH=. .venv/bin/python scripts/parse_web_leads.py --stdin-json < payload.json

Env (see ``.env.example``)::

  WEB_LEADS_IMAP_HOST / PORT / USER / PASSWORD / FOLDER
  WEB_LEAD_STAGE_NAME=Nuevo / Web Lead
  WEB_LEADS_DRY_RUN=false
"""
from __future__ import annotations

import argparse
import email
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from src.web_leads.imap_poll import imap_configured  # noqa: E402
from src.web_leads.parser import parse_email_message, parse_webhook_payload  # noqa: E402
from src.web_leads.pipeline import (  # noqa: E402
    ingest_web_lead,
    ingest_webhook_payload,
    process_web_lead_batch,
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument(
        "--file",
        type=Path,
        help="Parse a single .eml / RFC822 file instead of IMAP",
    )
    p.add_argument(
        "--stdin-json",
        action="store_true",
        help="Read a Mailgun/SendGrid-style JSON payload from stdin",
    )
    p.add_argument(
        "--check",
        action="store_true",
        help="IMAP login/select health check only (prints status: ok|error)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    dry = bool(args.dry_run)

    if args.check:
        from src.web_leads.imap_poll import check_imap_status

        report = check_imap_status()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report.get("status") == "ok" else 1

    if args.stdin_json:
        payload = json.load(sys.stdin)
        result = ingest_webhook_payload(payload, dry_run=dry)
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2, default=str))
        return 0 if result.status in {"ok", "dry_run", "skipped"} else 1

    if args.file:
        raw = args.file.read_bytes()
        msg = email.message_from_bytes(raw)
        lead = parse_email_message(msg)
        result = ingest_web_lead(lead, dry_run=dry)
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2, default=str))
        return 0 if result.status in {"ok", "dry_run", "skipped"} else 1

    if not imap_configured():
        print(
            json.dumps(
                {
                    "status": "skipped",
                    "reason": "imap_not_configured",
                    "hint": "Set WEB_LEADS_IMAP_HOST/USER/PASSWORD in .env",
                },
                ensure_ascii=False,
            )
        )
        return 0

    batch = process_web_lead_batch(dry_run=dry, limit=int(args.limit))
    print(json.dumps(batch, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
