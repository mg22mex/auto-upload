#!/usr/bin/env python3
"""Headed Facebook login — saves Playwright profile under sessions/<account>/.

Flow (do not close the browser yourself):
  1. Chromium opens → log in, finish 2FA / checkpoint.
  2. Click **Trust this device** if Facebook shows it.
  3. Press ENTER in *this* terminal (twice) — only then is the session
     flushed into ``sessions/<account>/`` and the browser closed.

Example::

    python scripts/fb_login.py --account account_2
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.facebook.errors import FacebookSessionError
from src.facebook.session import login_interactive, resolve_session_dir


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Log in to Facebook and save a Playwright session. "
            "Browser stays open until you press ENTER after 2FA / Trust this device."
        )
    )
    parser.add_argument(
        "--account",
        required=True,
        help="Account id from config.yaml (e.g. account_1, account_2)",
    )
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    config = load_config(ROOT / args.config)
    session_dir = resolve_session_dir(config, args.account, ROOT)
    print(f"Will save session to: {session_dir}")
    print(
        "Keep the Chromium window open through 2FA / 'Trust this device'; "
        "then press ENTER in this terminal when prompted."
    )
    print("")
    try:
        login_interactive(config, args.account, root=ROOT)
    except FacebookSessionError as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1
    print(f"Done. Profile ready at {session_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
