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

from src.facebook.util import ensure_unbuffered_stdio, env_bool
from src.sync.process_lock import ProcessLock
from src.sync.weekly_bump import resolve_weekly_bump_mode, weekly_bump_config


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _run(cmd: list[str]) -> int:
    print(f"Command:   {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, cwd=str(ROOT))
    return int(result.returncode)


def skip_odoo_requested(*, cli: bool = False) -> bool:
    """True when CLI ``--skip-odoo`` or ``SKIP_ODOO`` env is set."""
    return bool(cli) or env_bool("SKIP_ODOO", False)


def run_catalog_sync(
    *,
    catalog: str,
    config_path: str,
    dry_run: bool,
    accounts: list[str] | None,
    scrape: bool,
    skip_odoo: bool = False,
) -> int:
    """Autosell scrape → Odoo inventory → Facebook creates for missing catalog rows.

    ``skip_odoo`` / ``SKIP_ODOO`` skips XML-RPC inventory sync and uses the local
    catalog snapshot only (fast FB debug path).
    """
    print("=== Catalog sync (before listing bump) ===", flush=True)
    if skip_odoo:
        print("SKIP_ODOO: bypassing Odoo inventory sync (local catalog only)", flush=True)
        catalog_path = ROOT / catalog
        if not catalog_path.is_file():
            print(f"Catalog not found: {catalog_path}", file=sys.stderr, flush=True)
            return 1
        scrape = False

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

    if not skip_odoo and (os.getenv("ODOO_URL") or os.getenv("ODOO_DB")):
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
    if skip_odoo:
        sync_cmd.append("--skip-odoo")
    if accounts:
        sync_cmd.extend(["--accounts", *accounts])
    return _run(sync_cmd)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Marketplace listing bump: default full repost/relist (delete+recreate). "
            "Optional --mode renew for FB native Renovar. Age floor defaults to 2 days. "
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
        help="Process eligible live listings (age ≥ min days). Capped by --max-per-account.",
    )
    group.add_argument(
        "--dry-run-allocation",
        action="store_true",
        help="Print slot allocation table (Account | assigned | free) and exit",
    )
    parser.add_argument(
        "--older-than",
        "--min-age-days",
        dest="older_than",
        default=None,
        help="Min age days (default: config/env, usually 2; supports 1.5). Pass 0 to include fresh listings.",
    )
    parser.add_argument(
        "--max",
        "--max-per-account",
        dest="max",
        type=int,
        default=None,
        help="Max reposts per account (default: config 25). --force does not lift this.",
    )
    parser.add_argument(
        "--unlimited",
        action="store_true",
        help="Full-shelf run (ignore per-account cap)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore holds and min-age; still respects --max-per-account unless --unlimited",
    )
    parser.add_argument(
        "--no-lock",
        action="store_true",
        help="Skip /tmp/auto_upload_bump.lock (not recommended)",
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
    parser.add_argument(
        "--skip-odoo",
        action="store_true",
        help=(
            "Skip Odoo XML-RPC inventory sync; use local catalog_latest.json only "
            "(also set by SKIP_ODOO=true). Implies no autosell scrape in catalog sync."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Plan only")
    parser.add_argument("--catalog", default="data/catalog_latest.json")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    ensure_unbuffered_stdio()
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    load_dotenv(ROOT / ".env")
    config = load_config(ROOT / args.config)
    bump = weekly_bump_config(config)
    repost_cfg = (config.get("sync") or {}).get("repost") or {}

    lock: ProcessLock | None = None
    if not args.dry_run and not args.dry_run_allocation and not args.no_lock:
        lock = ProcessLock()
        lock.install_signal_handlers()
        lock.acquire()

    try:
        return _run_bump(args, config, bump, repost_cfg)
    finally:
        if lock is not None:
            lock.release()


def _print_allocation(args, config: dict) -> int:
    from src.inventory.snapshot import load_catalog_snapshot
    from src.store.db import SyncStore
    from src.sync.allocator import allocate_slots, slot_allocator_config

    alloc_cfg = slot_allocator_config(config)
    accounts = args.account
    if not accounts:
        accounts = list(config.get("sync", {}).get("active_accounts") or [])
        if not accounts:
            accounts = [a["id"] for a in config.get("accounts", []) if a.get("id")]
    catalog_path = ROOT / args.catalog
    if not catalog_path.is_file():
        print(f"Catalog not found: {catalog_path}", file=sys.stderr)
        return 1
    vehicles = load_catalog_snapshot(catalog_path)
    db_path = os.getenv("DB_PATH", "data/sync.db")
    store = SyncStore(ROOT / db_path if not Path(db_path).is_absolute() else db_path)
    allocation = allocate_slots(
        vehicles,
        accounts,
        store.get_live_listings(),
        max_per_account=alloc_cfg["max_listings_per_account"],
    )
    print("=== Slot allocation (dry-run) ===", flush=True)
    print(
        f"Active accounts: {', '.join(allocation.account_ids)}  |  "
        f"cap={allocation.max_per_account}/account  |  "
        f"capacity={allocation.total_capacity()}  |  catalog={len(vehicles)}",
        flush=True,
    )
    print(allocation.format_table(), flush=True)
    if allocation.waitlist:
        print(
            f"Waitlist sample: {', '.join(allocation.waitlist[:12])}"
            + (" …" if len(allocation.waitlist) > 12 else ""),
            flush=True,
        )
    if allocation.overflow:
        print(
            f"Overflow live (not in slot partition): {len(allocation.overflow)} "
            f"(set sync.slot_allocator.enforce_overflow_removals: true to remove)",
            flush=True,
        )
    return 0


def build_bump_command(
    *,
    script: Path,
    accounts: list[str] | None = None,
    ids: str | None = None,
    older_than: str | None = None,
    max_per: int | None = None,
    unlimited: bool = False,
    force: bool = False,
    dry_run: bool = False,
    catalog: str = "data/catalog_latest.json",
    config: str = "config.yaml",
    python: str = sys.executable,
) -> list[str]:
    """Build the run_repost.py / run_renew.py argv.

    ``--older-than 0`` / ``--min-age-days 0`` is forwarded as ``--min-age-days 0``
    (process all live dates). ``--force`` is forwarded separately.
    """
    cmd: list[str] = [python, "-u", str(script)]
    if accounts:
        cmd.extend(["--account", *accounts])
    if ids:
        cmd.extend(["--ids", ids])
    else:
        cmd.append("--all-eligible")
    if older_than is not None:
        cmd.extend(["--min-age-days", str(older_than)])
    if unlimited:
        cmd.append("--unlimited")
    elif max_per is not None:
        cmd.extend(["--max", str(max_per)])
    if force:
        cmd.append("--force")
    if dry_run:
        cmd.append("--dry-run")
    cmd.extend(["--catalog", catalog, "--config", config])
    return cmd


def _run_bump(args, config: dict, bump: dict, repost_cfg: dict) -> int:
    if args.dry_run_allocation:
        return _print_allocation(args, config)
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

    skip_odoo = skip_odoo_requested(cli=bool(args.skip_odoo))

    if do_sync:
        sync_rc = run_catalog_sync(
            catalog=args.catalog,
            config_path=args.config,
            dry_run=args.dry_run,
            accounts=args.account,
            scrape=not args.skip_scrape and not skip_odoo,
            skip_odoo=skip_odoo,
        )
        if sync_rc:
            print(
                f"WARNING: catalog sync exited {sync_rc} — continuing listing bump",
                flush=True,
            )

    script = ROOT / "scripts" / (
        "run_renew.py" if mode == "renew" else "run_repost.py"
    )
    max_per = args.max
    if max_per is None:
        max_per = int(
            repost_cfg.get("max_per_account_per_run")
            or bump.get("max_per_account_per_run")
            or 25
        )
    cmd = build_bump_command(
        script=script,
        accounts=args.account,
        ids=args.ids,
        older_than=args.older_than,
        max_per=max_per,
        unlimited=args.unlimited,
        force=args.force,
        dry_run=args.dry_run,
        catalog=args.catalog,
        config=args.config,
    )

    print("=== Listing bump (daily incremental) ===", flush=True)
    print(f"Mode:      {mode} ({'forced' if force else 'auto config'})", flush=True)
    print(f"Timezone:  {bump['timezone']}", flush=True)
    print(f"Skip Odoo: {skip_odoo}", flush=True)
    print(f"Force:     {args.force}", flush=True)
    print(f"Max/acct:  {'unlimited' if args.unlimited else max_per}", flush=True)
    print(
        f"Even/odd:  {bump['even_week']} / {bump['odd_week']} "
        f"(min_age_days={args.older_than if args.older_than is not None else bump.get('min_age_days', 2)}"
        f"{' overridden' if args.force or args.older_than == '0' else ''})",
        flush=True,
    )
    print(f"Command:   {' '.join(cmd)}", flush=True)
    print("", flush=True)

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    result = subprocess.run(cmd, cwd=str(ROOT), env=env)
    return int(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
