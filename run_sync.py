#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.facebook.util import env_int
from src.inventory.autosell import AutosellCatalogError, fetch_catalog
from src.inventory.snapshot import load_catalog_snapshot, save_catalog_snapshot
from src.sync.engine import plan_sync_actions, split_executable_actions
from src.sync.allocator import allocate_from_config, slot_allocator_config
from src.store.db import SyncStore

def load_config(config_path: Path) -> dict:
    with config_path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def archive_snapshot(vehicles, snapshot_dir: Path) -> Path:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return save_catalog_snapshot(vehicles, snapshot_dir / f"catalog_{stamp}.json")


def print_report(vehicles, executable, deferred) -> None:
    creates = [a for a in executable if a.action == "create"]
    updates = [a for a in executable if a.action == "update"]
    removals = [a for a in executable if a.action == "remove"]
    deferred_creates = [a for a in deferred if a.action == "create"]

    print("")
    print("=== Autosell → Facebook sync ===")
    print(f"Public catalog vehicles: {len(vehicles)}")
    print(f"Would create now:        {len(creates)}")
    print(f"Would update:            {len(updates)}")
    print(f"Would remove/mark sold:  {len(removals)}")
    print(f"Deferred creates:        {len(deferred_creates)}")
    print("")

    if removals:
        print("Removals:")
        for action in removals[:20]:
            print(f"  - {action.autosell_id} on {action.account_id}: {action.reason}")
        if len(removals) > 20:
            print(f"  ... and {len(removals) - 20} more")
        print("")

    if creates:
        print("Creates:")
        for action in creates[:15]:
            title = action.vehicle.marketplace_title if action.vehicle else action.slug
            print(f"  - [{action.account_id}] {title} ({action.autosell_id})")
        if len(creates) > 15:
            print(f"  ... and {len(creates) - 15} more")
        print("")

    if updates:
        print("Updates:")
        for action in updates[:10]:
            title = action.vehicle.marketplace_title if action.vehicle else action.slug
            print(f"  - [{action.account_id}] {title}")
        if len(updates) > 10:
            print(f"  ... and {len(updates) - 10} more")
        print("")

    if deferred_creates:
        print("Deferred (daily cap):")
        for action in deferred_creates[:10]:
            title = action.vehicle.marketplace_title if action.vehicle else action.slug
            print(f"  - [{action.account_id}] {title}")
        if len(deferred_creates) > 10:
            print(f"  ... and {len(deferred_creates) - 10} more")
        print("")


def run_scrape(config: dict, output_path: Path, snapshot_dir: Path) -> list:
    print("Fetching public catalog from autosell.mx ...")
    vehicles = fetch_catalog(config)
    save_catalog_snapshot(vehicles, output_path)
    archive_path = archive_snapshot(vehicles, snapshot_dir)
    print(f"Saved catalog artifact: {output_path}")
    print(f"Archived snapshot:      {archive_path}")
    print(f"Vehicle count:          {len(vehicles)}")
    return vehicles


def run_sync_from_catalog(
    vehicles,
    config: dict,
    *,
    db_path: str,
    dry_run: bool,
    max_posts: int,
    account_ids: list[str] | None = None,
) -> int:
    if account_ids is None:
        account_ids = [account["id"] for account in config.get("accounts", [])]
    if not account_ids:
        print("No accounts configured", file=sys.stderr)
        return 1

    store = SyncStore(db_path)
    run_id = store.start_sync_run(dry_run=dry_run)

    try:
        active_ids = {vehicle.autosell_id for vehicle in vehicles}

        for vehicle in vehicles:
            store.upsert_catalog_snapshot(vehicle)
        store.commit_catalog_snapshot(active_ids)
        store.commit()

        live_listings = store.get_live_listings()
        allocation = allocate_from_config(config, vehicles, account_ids, live_listings)
        alloc_cfg = slot_allocator_config(config)
        if allocation:
            print("=== Slot allocation ===", flush=True)
            print(allocation.format_table(), flush=True)
            print("", flush=True)
        actions = plan_sync_actions(
            vehicles,
            account_ids,
            live_listings,
            max_creates_per_account=max_posts,
            allocation=allocation,
            enforce_overflow_removals=alloc_cfg["enforce_overflow_removals"],
        )
        executable, deferred = split_executable_actions(actions)
        print_report(vehicles, executable, deferred)

        creates = len([a for a in executable if a.action == "create"])
        updates = len([a for a in executable if a.action == "update"])
        removals = len([a for a in executable if a.action == "remove"])

        executed_creates = 0
        executed_updates = 0
        executed_removals = 0
        fb_errors = 0
        notes = ""

        if dry_run:
            print("Dry run complete. No Facebook actions executed.")
            notes = f"DRY RUN. Deferred creates: {len(deferred)}."
        else:
            from src.facebook.executor import execute_actions

            print("Executing Facebook actions...")
            fb_result = execute_actions(
                executable, store, config, root=ROOT, account_order=account_ids
            )
            executed_creates = fb_result.creates
            executed_updates = fb_result.updates
            executed_removals = fb_result.removals
            fb_errors = len(fb_result.errors)
            if fb_result.errors:
                for err in fb_result.errors:
                    print(f"  FB error: {err}", file=sys.stderr)
            print(
                f"Facebook done: {executed_creates} created, "
                f"{executed_updates} updated, {executed_removals} removed."
            )
            notes = (
                f"LIVE. Executed creates={executed_creates}, updates={executed_updates}, "
                f"removals={executed_removals}, errors={fb_errors}, deferred={len(deferred)}."
            )

        store.finish_sync_run(
            run_id,
            vehicles_found=len(vehicles),
            creates=executed_creates if not dry_run else creates,
            updates=executed_updates if not dry_run else updates,
            removals=executed_removals if not dry_run else removals,
            notes=notes,
        )

        return 1 if (not dry_run and fb_errors) else 0

    except Exception as exc:
        store.finish_sync_run(run_id, vehicles_found=0, creates=0, updates=0, removals=0, notes=str(exc))
        raise
    finally:
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync autosell.mx catalog with Facebook Marketplace.")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument(
        "--scrape-only",
        action="store_true",
        help="Fetch public catalog and write JSON only (GitHub cloud job)",
    )
    parser.add_argument(
        "--from-snapshot",
        metavar="PATH",
        help="Load catalog JSON from file instead of scraping (fb-worker job)",
    )
    parser.add_argument(
        "--output",
        default="data/catalog_latest.json",
        help="Output path for --scrape-only",
    )
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Plan only; do not touch Facebook (default from DRY_RUN env)",
    )
    parser.add_argument(
        "--accounts",
        metavar="ID",
        nargs="+",
        help="Limit sync to these account ids (e.g. account_2). Default: all in config.yaml",
    )
    parser.add_argument(
        "--skip-odoo",
        action="store_true",
        help=(
            "Fast FB debug: skip live autosell scrape when no --from-snapshot; "
            "load data/catalog_latest.json. Odoo inventory sync is a separate step "
            "(also skipped when SKIP_ODOO=true in workflows / weekly bump)."
        ),
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    config = load_config(ROOT / args.config)

    dry_run = env_bool("DRY_RUN", True) if args.dry_run is None else args.dry_run
    skip_odoo = bool(args.skip_odoo) or env_bool("SKIP_ODOO", False)
    db_path = os.getenv("DB_PATH", "data/sync.db")
    snapshot_dir = Path(os.getenv("SNAPSHOT_DIR", "data/snapshots"))
    output_path = Path(args.output)
    max_posts = env_int(
        "MAX_POSTS_PER_ACCOUNT_PER_RUN",
        int(config.get("sync", {}).get("max_posts_per_account_per_run", 15)),
    )

    all_account_ids = [account["id"] for account in config.get("accounts", [])]
    if args.accounts:
        account_ids = args.accounts
    else:
        env_accounts = os.getenv("SYNC_ACCOUNTS", "").strip()
        if env_accounts:
            account_ids = [part.strip() for part in env_accounts.split(",") if part.strip()]
        else:
            account_ids = list(config.get("sync", {}).get("active_accounts") or all_account_ids)
    unknown = set(account_ids) - set(all_account_ids)
    if unknown:
        print(f"Unknown account(s): {', '.join(sorted(unknown))}", file=sys.stderr)
        return 1
    if account_ids != all_account_ids:
        print(f"Accounts limited to: {', '.join(account_ids)}")

    try:
        if args.scrape_only:
            if skip_odoo:
                print(
                    "SKIP_ODOO set with --scrape-only: still scraping autosell "
                    "(Odoo sync is a separate workflow step)",
                    flush=True,
                )
            run_scrape(config, output_path, snapshot_dir)
            return 0

        if args.from_snapshot:
            snapshot_path = Path(args.from_snapshot)
            if not snapshot_path.is_file():
                print(f"Snapshot not found: {snapshot_path}", file=sys.stderr)
                return 1
            if skip_odoo:
                print(
                    f"SKIP_ODOO: using snapshot {snapshot_path} "
                    "(no live scrape; Odoo inventory sync skipped upstream)",
                    flush=True,
                )
            vehicles = load_catalog_snapshot(snapshot_path)
            print(f"Loaded {len(vehicles)} vehicles from {snapshot_path}")
            return run_sync_from_catalog(
                vehicles,
                config,
                db_path=db_path,
                dry_run=dry_run,
                max_posts=max_posts,
                account_ids=account_ids,
            )

        if skip_odoo:
            snapshot_path = output_path if output_path.is_file() else Path("data/catalog_latest.json")
            if not snapshot_path.is_file():
                print(
                    f"SKIP_ODOO: cached catalog not found: {snapshot_path}",
                    file=sys.stderr,
                )
                return 1
            print(
                f"SKIP_ODOO: using cached catalog {snapshot_path} (no live scrape)",
                flush=True,
            )
            vehicles = load_catalog_snapshot(snapshot_path)
            print(f"Loaded {len(vehicles)} vehicles from {snapshot_path}")
            return run_sync_from_catalog(
                vehicles,
                config,
                db_path=db_path,
                dry_run=dry_run,
                max_posts=max_posts,
                account_ids=account_ids,
            )

        vehicles = run_scrape(config, output_path, snapshot_dir)
        return run_sync_from_catalog(
            vehicles,
            config,
            db_path=db_path,
            dry_run=dry_run,
            max_posts=max_posts,
            account_ids=account_ids,
        )

    except AutosellCatalogError as exc:
        print(f"Catalog error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
