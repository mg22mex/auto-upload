#!/usr/bin/env python3
"""Extract c_user / xs / fb_dtsg from a stored Playwright profile (no UI actions)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.facebook.graphql.session_ctx import (
    as_public_dict,
    extract_session_context_from_profile,
)
from src.facebook.util import env_bool


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    with (ROOT / args.config).open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    headless = not args.headed and env_bool(
        "FB_HEADLESS", bool(config.get("facebook", {}).get("headless", True))
    )
    session = extract_session_context_from_profile(
        config, args.account, root=ROOT, headless=headless
    )
    print(json.dumps(as_public_dict(session), indent=2))
    print(session.redacted_summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
