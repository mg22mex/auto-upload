#!/usr/bin/env python3
"""Aggressive mass purge of ALL Marketplace listings (account_1 + account_2).

Targets active/published AND phantom/duplicate/Needs-attention leftovers.

1. GraphQL mass purge — discover every listing id (selling shelves + sync.db),
   then ``useDirectMarketplaceForSaleItemDeleteMutation`` (or captured
   ``data/graphql/delete_listing.json``) sequentially. Never stops on errors;
   logs total deleted.

2. Accelerated DOM sweeper — ``/marketplace/you/selling`` (+ attention/sold)
   and ``/marketplace/you/notifications``. Aggressive scroll, bilingual
   EN/ES Delete CTAs / ⋮ menus with a 2000ms timeout per element
   (awaited clicks + container scroll to render all cards), then
   Confirm/Eliminar modals, then dismiss post-delete survey
   (Prefer not to answer / Prefiero no responder → Submit/Enviar).
   Also targets "This looks like a duplicate so we didn't post it /
   Delete it and try again" banner cards (account_1 EN).

3. Reset ``data/sync.db`` fb_listings + repost_holds → 0 mappings.

Examples:
  FB_USE_GRAPHQL=1 python scripts/purge_all.py --dry-run
  FB_USE_GRAPHQL=1 python scripts/purge_all.py --yes
  FB_USE_GRAPHQL=1 python scripts/purge_all.py --yes --accounts account_1
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.facebook.graphql.errors import FacebookGraphqlFallback
from src.facebook.graphql.mode import graphql_enabled, graphql_ui_fallback
from src.facebook.purge_old import (
    PurgeStats,
    purge_mass_dom_fast,
    purge_old_listings_on_page,
)
from src.facebook.session import (
    format_session_login_error,
    get_page,
    is_logged_in,
    open_account_context,
    resolve_session_dir,
    session_health_report,
)
from src.facebook.util import ensure_unbuffered_stdio, env_bool, env_float
from src.store.db import SyncStore

DEFAULT_ACCOUNTS = ("account_1", "account_2")


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def resolve_accounts(config: dict, requested: list[str] | None) -> list[str]:
    all_ids = [a["id"] for a in config.get("accounts", [])]
    if requested:
        unknown = set(requested) - set(all_ids)
        if unknown:
            raise SystemExit(f"Unknown account(s): {', '.join(sorted(unknown))}")
        return list(requested)
    preferred = [a for a in DEFAULT_ACCOUNTS if a in all_ids]
    if preferred:
        return preferred
    active = config.get("sync", {}).get("active_accounts")
    return list(active) if active else all_ids


def reset_sync_mappings(db_path: Path, *, account_ids: list[str] | None = None) -> None:
    """Wipe fb_listings / repost_holds → 0 mappings."""
    if not db_path.is_file():
        print(f"  sync.db missing at {db_path} — skip DB reset", flush=True)
        return
    con = sqlite3.connect(db_path)
    before = {
        "fb_listings": con.execute("SELECT COUNT(*) FROM fb_listings").fetchone()[0],
        "repost_holds": con.execute("SELECT COUNT(*) FROM repost_holds").fetchone()[0],
    }
    if account_ids:
        placeholders = ",".join("?" for _ in account_ids)
        con.execute(
            f"DELETE FROM fb_listings WHERE account_id IN ({placeholders})",
            tuple(account_ids),
        )
        con.execute("DELETE FROM repost_holds")
    else:
        con.execute("DELETE FROM fb_listings")
        con.execute("DELETE FROM repost_holds")
    con.commit()
    after = {
        "fb_listings": con.execute("SELECT COUNT(*) FROM fb_listings").fetchone()[0],
        "repost_holds": con.execute("SELECT COUNT(*) FROM repost_holds").fetchone()[0],
    }
    con.close()
    print(
        f"  sync.db reset → 0 mappings: "
        f"fb_listings {before['fb_listings']}→{after['fb_listings']}, "
        f"repost_holds {before['repost_holds']}→{after['repost_holds']}",
        flush=True,
    )


def _merge_stats(a: PurgeStats, b: PurgeStats) -> PurgeStats:
    return PurgeStats(
        found=a.found + b.found,
        skipped_young=a.skipped_young + b.skipped_young,
        skipped_unknown_age=a.skipped_unknown_age + b.skipped_unknown_age,
        skipped_phantom=a.skipped_phantom + b.skipped_phantom,
        deleted=a.deleted + b.deleted,
        failed=a.failed + b.failed,
        dry_run_candidates=a.dry_run_candidates + b.dry_run_candidates,
    )


def purge_account(
    *,
    config: dict,
    account_id: str,
    store: SyncStore,
    dry_run: bool,
    headless: bool,
    max_deletes: int | None,
    skip_dom: bool = False,
    gql_delay_sec: float = 0.15,
    dom_click_ms: int = 2000,
) -> tuple[int, int, int]:
    """Mass GraphQL wipe then accelerated DOM sweeper. Return (found, deleted, failed)."""
    session_dir = resolve_session_dir(config, account_id, ROOT)
    health = session_health_report(session_dir)
    print(
        f"\n=== {account_id} MASS PURGE ===\n"
        f"  session exists={health.get('exists')} path={session_dir}",
        flush=True,
    )

    stats: PurgeStats | None = None

    if graphql_enabled():
        try:
            from src.facebook.graphql.purge import mass_purge_all_listings_graphql
            from src.facebook.graphql.session_ctx import (
                extract_session_context_from_profile,
            )

            session = extract_session_context_from_profile(
                config, account_id, root=ROOT, headless=headless
            )
            print(
                f"  [{account_id}] GraphQL session {session.redacted_summary()}",
                flush=True,
            )
            stats = mass_purge_all_listings_graphql(
                session,
                store,
                account_id=account_id,
                dry_run=dry_run,
                max_deletes=max_deletes,
                delay_sec=gql_delay_sec,
                root=ROOT,
            )
            print(
                f"  [{account_id}] GraphQL mass deleted={stats.deleted} "
                f"(failed={stats.failed} phantom={stats.skipped_phantom})",
                flush=True,
            )
        except FacebookGraphqlFallback as exc:
            if not graphql_ui_fallback():
                raise
            print(
                f"  [{account_id}] GraphQL mass fallback → DOM ({exc})",
                flush=True,
            )
            stats = None
        except Exception as exc:
            if not graphql_ui_fallback():
                raise
            print(
                f"  [{account_id}] GraphQL mass error → DOM ({exc})",
                flush=True,
            )
            stats = None

    if skip_dom:
        if stats is None:
            with open_account_context(
                config, account_id, root=ROOT, headless=headless
            ) as context:
                page = get_page(context)
                if not is_logged_in(page):
                    print(
                        format_session_login_error(account_id, session_dir),
                        file=sys.stderr,
                        flush=True,
                    )
                    return 0, 0, 1
                stats = purge_old_listings_on_page(
                    page,
                    store,
                    account_id=account_id,
                    max_days=0.0,
                    dry_run=dry_run,
                    max_deletes=max_deletes,
                    delay_min_sec=0.2,
                    delay_max_sec=0.4,
                    root=ROOT,
                )
    else:
        with open_account_context(
            config, account_id, root=ROOT, headless=headless
        ) as context:
            page = get_page(context)
            if not is_logged_in(page):
                print(
                    format_session_login_error(account_id, session_dir),
                    file=sys.stderr,
                    flush=True,
                )
                return (
                    (stats.found if stats else 0),
                    (stats.deleted if stats else 0),
                    (stats.failed if stats else 0) + 1,
                )
            lang_hint = (
                "EN"
                if account_id == "account_1"
                else "ES"
                if account_id == "account_2"
                else "EN+ES"
            )
            print(
                f"  [{account_id}] Accelerated DOM sweeper "
                f"(selling + notifications, {dom_click_ms}ms clicks, {lang_hint})",
                flush=True,
            )
            dom = purge_mass_dom_fast(
                page,
                account_id=account_id,
                dry_run=dry_run,
                click_timeout_ms=dom_click_ms,
            )
            stats = _merge_stats(stats, dom) if stats is not None else dom

    assert stats is not None
    print(
        f"  [{account_id}] ACCOUNT TOTAL: found={stats.found} "
        f"deleted={stats.deleted} failed={stats.failed} "
        f"phantom_skip={stats.skipped_phantom}"
        + (
            f" dry_run_candidates={stats.dry_run_candidates}"
            if dry_run
            else ""
        ),
        flush=True,
    )
    return stats.found, stats.deleted, stats.failed


def main() -> int:
    ensure_unbuffered_stdio()
    os.environ.setdefault("FB_USE_GRAPHQL", "1")

    parser = argparse.ArgumentParser(
        description=(
            "Aggressive mass purge: GraphQL delete-all (continue on errors) + "
            "fast DOM sweeper for phantoms/attention, then sync.db → 0."
        )
    )
    parser.add_argument(
        "--accounts",
        "--account",
        dest="accounts",
        nargs="+",
        help="Account ids (default: account_1 account_2)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover and log candidates only; do not delete or reset DB",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required for live deletes + sync.db reset",
    )
    parser.add_argument(
        "--max-deletes",
        type=int,
        default=None,
        help="Optional safety cap per account (GraphQL phase)",
    )
    parser.add_argument(
        "--gql-delay",
        type=float,
        default=None,
        help="Seconds between GraphQL deletes (default 0.15)",
    )
    parser.add_argument(
        "--dom-click-ms",
        type=int,
        default=2000,
        help="Playwright click timeout ms for DOM sweeper (default 2000)",
    )
    parser.add_argument(
        "--skip-db-reset",
        action="store_true",
        help="Do not clear fb_listings / repost_holds after purge",
    )
    parser.add_argument(
        "--skip-dom",
        action="store_true",
        help="Skip accelerated Playwright DOM sweeper (GraphQL-only)",
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--db",
        default=None,
        help="Path to sync.db (default: DB_PATH or data/sync.db)",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    if not str(os.getenv("FB_USE_GRAPHQL") or "").strip():
        os.environ["FB_USE_GRAPHQL"] = "1"

    dry_run = bool(args.dry_run) or env_bool("DRY_RUN", False)
    if not dry_run and not args.yes:
        print(
            "Refusing destructive purge without --yes (or pass --dry-run).",
            file=sys.stderr,
        )
        return 2

    config = load_config(ROOT / args.config)
    account_ids = resolve_accounts(config, args.accounts)
    headless = env_bool(
        "FB_HEADLESS", bool(config.get("facebook", {}).get("headless", True))
    )
    db_raw = args.db or os.getenv("DB_PATH", "data/sync.db")
    db_path = Path(db_raw)
    if not db_path.is_absolute():
        db_path = ROOT / db_path

    gql_delay = (
        float(args.gql_delay)
        if args.gql_delay is not None
        else env_float("PURGE_GQL_DELAY_SEC", 0.15)
    )
    dom_click_ms = int(args.dom_click_ms)

    print("=== MASS PURGE ALL Marketplace listings ===", flush=True)
    print(f"Accounts:   {', '.join(account_ids)}", flush=True)
    print(f"Mode:       {'DRY-RUN' if dry_run else 'LIVE hard-delete'}", flush=True)
    print(f"GraphQL:    {graphql_enabled()} (continue-on-error, delay={gql_delay}s)", flush=True)
    print(
        f"DOM:        {'skip' if args.skip_dom else f'fast sweeper ({dom_click_ms}ms)'}",
        flush=True,
    )
    print(f"DB reset:   {'skip' if args.skip_db_reset or dry_run else '→ 0 mappings'}", flush=True)
    print(f"DB:         {db_path}", flush=True)
    print("", flush=True)

    store = SyncStore(db_path)
    total_found = total_deleted = total_failed = 0
    try:
        for account_id in account_ids:
            try:
                found, deleted, failed = purge_account(
                    config=config,
                    account_id=account_id,
                    store=store,
                    dry_run=dry_run,
                    headless=headless,
                    max_deletes=args.max_deletes,
                    skip_dom=bool(args.skip_dom),
                    gql_delay_sec=gql_delay,
                    dom_click_ms=dom_click_ms,
                )
                total_found += found
                total_deleted += deleted
                total_failed += failed
            except Exception as exc:
                print(f"ERROR {account_id}: {exc}", file=sys.stderr, flush=True)
                total_failed += 1
    finally:
        store.close()

    if not dry_run and not args.skip_db_reset:
        print("\n=== Final database sync (reset → 0) ===", flush=True)
        reset_sync_mappings(db_path, account_ids=account_ids)

    print("\n=== Done ===", flush=True)
    print(
        f"TOTAL deleted={total_deleted} found={total_found} failed={total_failed}",
        flush=True,
    )
    return 1 if total_failed and not total_deleted else 0


if __name__ == "__main__":
    raise SystemExit(main())
