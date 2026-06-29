#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.facebook.errors import FacebookSessionError
from src.facebook.session import login_interactive


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def main() -> int:
    parser = argparse.ArgumentParser(description="Log in to Facebook and save a Playwright session.")
    parser.add_argument("--account", required=True, help="Account id from config.yaml (e.g. account_1)")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    config = load_config(ROOT / args.config)
    try:
        login_interactive(config, args.account, root=ROOT)
    except FacebookSessionError as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
