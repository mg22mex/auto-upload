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


def _run(cmd: list[str]) -> int:
    print(f"Command:   {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, cwd=str(ROOT))
    return int(result.returncode)


def run_catalog_sync(
    *,
    catalog: str,
    config_path: str,
    dry_run: bool,
    accounts: list[str] | None,
    scrape: bool,
) -> int:
    """Autosell scrape → Odoo inventory → Facebook creates for missing catalog rows."""
    print("=== Catalog sync (before listing bump) ===", flush=True)
    if scrape:
        rc = _run(
            [
                sys.executable,
                str(ROOT / "run_sync.py"),
                "--config",
                config_path,
                "--scrape-only",
                "--output",
                catalog,
            ]
        )
        if rc:
            return rc
        snap_dir = ROOT / "data" / "snapshots"
        snap_dir.mkdir(parents=True, exist_ok=True)
        src = ROOT / catalog
        dest = snap_dir / "catalog_latest.json"
        if src.is_file():
            dest.write_bytes(src.read_bytes())

    if os.getenv("ODOO_URL") or os.getenv("ODOO_DB"):
        odoo_rc = _run(
            [
                sys.executable,
                str(ROOT / "scripts" / "sync_odoo_inventory.py"),
                "--from-snapshot",
                catalog,
            ]
        )
        if odoo_rc:
            print(
                f"WARNING: Odoo inventory sync exited {odoo_rc} — continuing bump",
                flush=True,
            )

    sync_cmd = [
        sys.executable,
        str(ROOT / "run_sync.py"),
        "--config",
        config_path,
        "--from-snapshot",
        catalog,
        "--dry-run" if dry_run else "--no-dry-run",
    ]
    if accounts:
        sync_cmd.extend(["--accounts", *accounts])
    return _run(sync_cmd)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Marketplace listing bump: default full repost/relist (delete+recreate). "
            "Optional --mode renew for FB native Renovar. Age floor defaults to 3 days. "
            "With --all-eligible, catalog sync (creates missing vehicles) runs first."
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
        help="Process all eligible live listings (age ≥ min days, or all dates with --force / --min-age-days 0)",
    )
    parser.add_argument(
        "--older-than",
        "--min-age-days",
        dest="older_than",
        default=None,
        help="Min age days (default: config/env, usually 3). Pass 0 to include 1–7 day old listings.",
    )
    parser.add_argument("--max", type=int, default=None, help="Max per account")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore holds and min-age; process all live catalog listings",
    )
    parser.add_argument(
        "--sync-catalog",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Scrape + Odoo + Facebook create-missing before bump (default: on for --all-eligible)",
    )
    parser.add_argument(
        "--skip-scrape",
        action="store_true",
        help="With catalog sync, reuse existing catalog JSON (no autosell.mx fetch)",
    )
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

    do_sync = args.sync_catalog
    if do_sync is None:
        do_sync = bool(args.all_eligible)

    if do_sync:
        sync_rc = run_catalog_sync(
            catalog=args.catalog,
            config_path=args.config,
            dry_run=args.dry_run,
            accounts=args.account,
            scrape=not args.skip_scrape,
        )
        if sync_rc:
            print(
                f"WARNING: catalog sync exited {sync_rc} — continuing listing bump",
                flush=True,
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
    if args.older_than is not None:
        cmd.extend(["--min-age-days", str(args.older_than)])
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
    print(f"Force:     {args.force}", flush=True)
    print(
        f"Even/odd:  {bump['even_week']} / {bump['odd_week']} "
        f"(min_age_days={args.older_than if args.older_than is not None else bump.get('min_age_days', 3)}"
        f"{' overridden' if args.force or args.older_than == '0' else ''})",
        flush=True,
    )
    print(f"Command:   {' '.join(cmd)}", flush=True)
    print("", flush=True)

    result = subprocess.run(cmd, cwd=str(ROOT))
    return int(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
