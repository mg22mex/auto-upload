#!/usr/bin/env python3
"""Delete debug screenshots and temp run logs older than the retention window.

Usage:
  python scripts/cleanup_logs.py
  python scripts/cleanup_logs.py --max-age-days 3 --dry-run
  python scripts/cleanup_logs.py --target data/logs/facebook:*.png
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.facebook.util import env_float  # noqa: E402
from src.log_cleanup import (  # noqa: E402
    DEFAULT_MAX_AGE_DAYS,
    DEFAULT_TARGETS,
    cleanup_default_targets,
)


def parse_targets(raw: list[str] | None) -> tuple[tuple[str, str], ...]:
    if not raw:
        return DEFAULT_TARGETS
    targets: list[tuple[str, str]] = []
    for item in raw:
        directory, _, pattern = item.partition(":")
        if not directory or not pattern:
            raise SystemExit(f"Invalid --target (expected DIR:GLOB): {item}")
        targets.append((directory, pattern))
    return tuple(targets)


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-age-days",
        type=float,
        default=None,
        help=f"Retention window (default: LOG_RETENTION_DAYS or {DEFAULT_MAX_AGE_DAYS})",
    )
    parser.add_argument(
        "--target",
        action="append",
        help="Extra DIR:GLOB to sweep (repeatable); replaces the defaults",
    )
    parser.add_argument("--dry-run", action="store_true", help="List files, delete nothing")
    parser.add_argument("--verbose", action="store_true", help="Print every affected file")
    args = parser.parse_args()

    max_age = args.max_age_days
    if max_age is None:
        max_age = env_float("LOG_RETENTION_DAYS", float(DEFAULT_MAX_AGE_DAYS))

    targets = parse_targets(args.target)
    print(
        f"Log cleanup: retention={max_age}d dry_run={args.dry_run} "
        f"targets={', '.join(f'{d}/{p}' for d, p in targets)}",
        flush=True,
    )

    result = cleanup_default_targets(
        ROOT,
        max_age_days=max_age,
        dry_run=args.dry_run,
        targets=targets,
    )
    if args.verbose:
        for path in result.deleted:
            print(f"  {path}", flush=True)
    for error in result.errors:
        print(f"  WARNING: {error}", file=sys.stderr, flush=True)
    print(result.summary(dry_run=args.dry_run), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
