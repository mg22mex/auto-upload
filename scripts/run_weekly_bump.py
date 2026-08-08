#!/usr/bin/env python3
"""Marketplace listing bump — default full relist (repost); renew optional."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.sync.weekly_bump import resolve_weekly_bump_mode, weekly_bump_config


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Marketplace listing bump: default full repost/relist (delete+recreate). "
            "Optional --mode renew for FB native Renovar. Age floor defaults to 3 days."
        )
    )
    parser.add_argument(
        "--mode",
        choices=("auto", "renew", "repost"),
        default="auto",
        help="auto = config calendar (default both weeks=repost); or force renew/repost",
    )
    parser.add_argument("--account", nargs="+", help="Limit to these accounts")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ids", help="Comma-separated autosell ids")
    group.add_argument(
        "--all-eligible",
        action="store_true",
        help="Process all eligible live listings (age ≥ min days)",
    )
    parser.add_argument(
        "--older-than",
        default=None,
        help="Min age days (default: REPOST_MIN_AGE_DAYS / config, usually 3)",
    )
    parser.add_argument("--max", type=int, default=None, help="Max per account")
    parser.add_argument("--force", action="store_true", help="Ignore repost holds")
    parser.add_argument("--dry-run", action="store_true", help="Plan only")
    parser.add_argument("--catalog", default="data/catalog_latest.json")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    config = load_config(ROOT / args.config)
    bump = weekly_bump_config(config)

    force = None if args.mode == "auto" else args.mode
    env_force = (os.getenv("WEEKLY_BUMP_MODE") or "").strip().lower()
    if force is None and env_force in ("renew", "repost", "auto"):
        force = None if env_force == "auto" else env_force

    mode = resolve_weekly_bump_mode(
        timezone=bump["timezone"],
        even_week=bump["even_week"],
        odd_week=bump["odd_week"],
        force_mode=force,
    )

    script = ROOT / "scripts" / (
        "run_renew.py" if mode == "renew" else "run_repost.py"
    )
    cmd: list[str] = [sys.executable, str(script)]
    if args.account:
        cmd.extend(["--account", *args.account])
    if args.ids:
        cmd.extend(["--ids", args.ids])
    else:
        cmd.append("--all-eligible")
    if args.older_than:
        cmd.extend(["--older-than", args.older_than])
    if args.max is not None:
        cmd.extend(["--max", str(args.max)])
    if args.force:
        cmd.append("--force")
    if args.dry_run:
        cmd.append("--dry-run")
    cmd.extend(["--catalog", args.catalog, "--config", args.config])

    print("=== Listing bump (relist-first) ===", flush=True)
    print(f"Mode:      {mode} ({'forced' if force else 'auto config'})", flush=True)
    print(f"Timezone:  {bump['timezone']}", flush=True)
    print(
        f"Even/odd:  {bump['even_week']} / {bump['odd_week']} "
        f"(min_age_days={bump.get('min_age_days', 3)})",
        flush=True,
    )
    print(f"Command:   {' '.join(cmd)}", flush=True)
    print("", flush=True)

    result = subprocess.run(cmd, cwd=str(ROOT))
    return int(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
