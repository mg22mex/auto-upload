"""Pre-flight Marketplace shelf/history cleanup before slot allocation.

Runs at the start of live ``run_sync`` so old selling-shelf cards are deleted
(and ``sync.db`` updated) before the allocator plans creates. Keep filters
protect specific listings (e.g. today's Ford Mustang GT).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.facebook.purge_old import PurgeStats, purge_old_listings_on_page
from src.facebook.session import (
    format_session_login_error,
    get_page,
    is_logged_in,
    open_account_context,
    resolve_session_dir,
    session_health_report,
)
from src.facebook.util import env_bool, env_float, env_str
from src.store.db import SyncStore


@dataclass
class PreflightPurgeConfig:
    enabled: bool = True
    max_days: float = 0.0
    keep_autosell_ids: set[str] = field(default_factory=set)
    keep_item_ids: set[str] = field(default_factory=set)
    keep_title_re: re.Pattern[str] | None = None
    max_deletes: int | None = None
    delay_min_sec: float = 2.0
    delay_max_sec: float = 4.0


@dataclass
class PreflightPurgeResult:
    ran: bool = False
    skipped_reason: str = ""
    accounts: list[str] = field(default_factory=list)
    found: int = 0
    deleted: int = 0
    failed: int = 0
    dry_run_candidates: int = 0
    by_account: dict[str, PurgeStats] = field(default_factory=dict)


def _csv_set(raw: str | list[Any] | None) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, list):
        return {str(part).strip() for part in raw if str(part).strip()}
    return {part.strip() for part in str(raw).split(",") if part.strip()}


def preflight_purge_config(config: dict[str, Any] | None = None) -> PreflightPurgeConfig:
    """Resolve purge settings from ``config.yaml`` ``sync.preflight_purge`` + env."""
    sync = (config or {}).get("sync") or {}
    raw = sync.get("preflight_purge") or {}
    if not isinstance(raw, dict):
        raw = {}

    enabled_default = bool(raw.get("enabled", True))
    enabled = env_bool("FB_PREFLIGHT_PURGE", enabled_default, "PREFLIGHT_PURGE")

    max_days = env_float(
        "FB_PREFLIGHT_PURGE_MAX_DAYS",
        float(raw.get("max_days", 0.0)),
        "PREFLIGHT_PURGE_MAX_DAYS",
    )

    keep_ids = _csv_set(raw.get("keep_autosell_ids") or raw.get("keep_ids"))
    keep_ids |= _csv_set(env_str("FB_PREFLIGHT_KEEP_IDS", "", "PREFLIGHT_KEEP_IDS"))

    keep_items = _csv_set(raw.get("keep_item_ids"))
    keep_items |= _csv_set(
        env_str("FB_PREFLIGHT_KEEP_ITEM_IDS", "", "PREFLIGHT_KEEP_ITEM_IDS")
    )

    title_re_raw = str(
        env_str(
            "FB_PREFLIGHT_KEEP_TITLE_RE",
            str(raw.get("keep_title_re") or ""),
            "PREFLIGHT_KEEP_TITLE_RE",
        )
    ).strip()
    keep_title_re = re.compile(title_re_raw) if title_re_raw else None

    max_deletes_raw = env_str("FB_PREFLIGHT_PURGE_MAX_DELETES", "")
    if max_deletes_raw.strip():
        max_deletes: int | None = int(max_deletes_raw)
    elif raw.get("max_deletes") is not None:
        max_deletes = int(raw["max_deletes"])
    else:
        max_deletes = None

    return PreflightPurgeConfig(
        enabled=enabled,
        max_days=max_days,
        keep_autosell_ids=keep_ids,
        keep_item_ids=keep_items,
        keep_title_re=keep_title_re,
        max_deletes=max_deletes,
        delay_min_sec=float(raw.get("delay_min_sec", 2.0)),
        delay_max_sec=float(raw.get("delay_max_sec", 4.0)),
    )


def run_preflight_shelf_purge(
    config: dict,
    store: SyncStore,
    account_ids: list[str],
    *,
    root: Path,
    dry_run: bool = False,
    headless: bool | None = None,
) -> PreflightPurgeResult:
    """Open each account session and purge old shelf listings (keep filters applied).

    Intended to run **before** slot allocation so ``sync.db`` live rows reflect
    the cleaned shelf. When ``dry_run`` is True, scans/report only (no deletes).
    """
    cfg = preflight_purge_config(config)
    result = PreflightPurgeResult(accounts=list(account_ids))

    if not cfg.enabled:
        result.skipped_reason = "disabled (sync.preflight_purge.enabled / FB_PREFLIGHT_PURGE)"
        print(f"Preflight purge: skipped — {result.skipped_reason}", flush=True)
        return result

    if not account_ids:
        result.skipped_reason = "no accounts"
        print(f"Preflight purge: skipped — {result.skipped_reason}", flush=True)
        return result

    if headless is None:
        headless = env_bool(
            "FB_HEADLESS",
            bool((config.get("facebook") or {}).get("headless", True)),
        )

    print("=== Preflight shelf/history purge ===", flush=True)
    print(
        f"Accounts: {', '.join(account_ids)}  max_days={cfg.max_days} "
        f"(delete age > {cfg.max_days})  dry_run={dry_run}",
        flush=True,
    )
    if cfg.keep_autosell_ids:
        print(
            f"Keep autosell ids: {', '.join(sorted(cfg.keep_autosell_ids))}",
            flush=True,
        )
    if cfg.keep_item_ids:
        print(
            f"Keep FB item ids: {', '.join(sorted(cfg.keep_item_ids))}",
            flush=True,
        )
    if cfg.keep_title_re is not None:
        print(f"Keep title re: {cfg.keep_title_re.pattern}", flush=True)
    if cfg.max_deletes is not None:
        print(f"Max deletes/account: {cfg.max_deletes}", flush=True)
    print("", flush=True)

    from src.facebook.graphql.errors import FacebookGraphqlFallback
    from src.facebook.graphql.mode import graphql_enabled, graphql_ui_fallback

    result.ran = True
    use_graphql = graphql_enabled()
    if use_graphql:
        print("Preflight mode: GraphQL/HTTP (FB_USE_GRAPHQL=1)", flush=True)

    for account_id in account_ids:
        session_dir = resolve_session_dir(config, account_id, root)
        health = session_health_report(session_dir)
        print(
            f"--- preflight {account_id} session exists={health.get('exists')} ---",
            flush=True,
        )
        try:
            stats: PurgeStats | None = None
            if use_graphql:
                try:
                    from src.facebook.graphql.purge import purge_old_listings_graphql
                    from src.facebook.graphql.session_ctx import (
                        extract_session_context_from_profile,
                    )

                    session = extract_session_context_from_profile(
                        config,
                        account_id,
                        root=root,
                        headless=headless,
                    )
                    print(
                        f"  [{account_id}] GraphQL session {session.redacted_summary()}",
                        flush=True,
                    )
                    stats = purge_old_listings_graphql(
                        session,
                        store,
                        account_id=account_id,
                        max_days=cfg.max_days,
                        dry_run=dry_run,
                        max_deletes=cfg.max_deletes,
                        delay_min_sec=cfg.delay_min_sec,
                        delay_max_sec=cfg.delay_max_sec,
                        keep_item_ids=cfg.keep_item_ids or None,
                        keep_autosell_ids=cfg.keep_autosell_ids or None,
                        keep_title_re=cfg.keep_title_re,
                        root=root,
                    )
                except FacebookGraphqlFallback as exc:
                    if not graphql_ui_fallback():
                        raise
                    print(
                        f"  [{account_id}] GraphQL preflight fallback → UI ({exc})",
                        flush=True,
                    )
                except Exception as exc:
                    if not graphql_ui_fallback():
                        raise
                    print(
                        f"  [{account_id}] GraphQL preflight error → UI ({exc})",
                        flush=True,
                    )

            if stats is None:
                with open_account_context(
                    config, account_id, root=root, headless=headless
                ) as context:
                    page = get_page(context)
                    if not is_logged_in(page):
                        print(
                            format_session_login_error(account_id, session_dir),
                            file=__import__("sys").stderr,
                            flush=True,
                        )
                        result.failed += 1
                        continue
                    stats = purge_old_listings_on_page(
                        page,
                        store,
                        account_id=account_id,
                        max_days=cfg.max_days,
                        dry_run=dry_run,
                        max_deletes=cfg.max_deletes,
                        delay_min_sec=cfg.delay_min_sec,
                        delay_max_sec=cfg.delay_max_sec,
                        keep_item_ids=cfg.keep_item_ids or None,
                        keep_autosell_ids=cfg.keep_autosell_ids or None,
                        keep_title_re=cfg.keep_title_re,
                        root=root,
                    )
        except Exception as exc:
            print(f"ERROR preflight {account_id}: {exc}", flush=True)
            result.failed += 1
            continue

        result.by_account[account_id] = stats
        result.found += stats.found
        result.deleted += stats.deleted
        result.failed += stats.failed
        result.dry_run_candidates += stats.dry_run_candidates
        print(
            f"  [{account_id}] preflight: found={stats.found} "
            f"deleted={stats.deleted} failed={stats.failed}"
            + (
                f" dry_candidates={stats.dry_run_candidates}"
                if dry_run
                else ""
            ),
            flush=True,
        )
        print("", flush=True)

    print(
        f"Preflight purge done: found={result.found} deleted={result.deleted} "
        f"failed={result.failed}"
        + (
            f" dry_candidates={result.dry_run_candidates}"
            if dry_run
            else ""
        ),
        flush=True,
    )
    print("", flush=True)
    return result
