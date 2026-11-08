"""Pre-flight Marketplace shelf/history cleanup before slot allocation.

Runs at the start of live ``run_sync`` so every listing older than 7 days is
hard-deleted (and ``sync.db`` updated) **before** the allocator plans creates.

Zero-tolerance: any failed / unconfirmed 7-day delete aborts the sync cycle
(``PreflightPurgeAborted``) so creates never run on a dirty shelf.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.facebook.errors import PreflightPurgeAborted
from src.facebook.purge_old import (
    PurgeStats,
    collect_selling_listings,
    purge_old_listings_on_page,
    purge_requieren_atencion_matches,
    vehicle_title_from_card_text,
)
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

# Absolute first-step rule: hard-delete listings older than this many days.
DEFAULT_MAX_DAYS = 7.0


@dataclass
class PreflightPurgeConfig:
    enabled: bool = True
    max_days: float = DEFAULT_MAX_DAYS
    keep_autosell_ids: set[str] = field(default_factory=set)
    keep_item_ids: set[str] = field(default_factory=set)
    keep_title_re: re.Pattern[str] | None = None
    max_deletes: int | None = None
    delay_min_sec: float = 2.0
    delay_max_sec: float = 4.0
    # Abort sync when any age-eligible delete fails / is unconfirmed (default ON).
    abort_on_failed_deletes: bool = True


@dataclass
class PreflightPurgeResult:
    ran: bool = False
    skipped_reason: str = ""
    accounts: list[str] = field(default_factory=list)
    found: int = 0
    deleted: int = 0
    failed: int = 0
    dry_run_candidates: int = 0
    aborted: bool = False
    by_account: dict[str, PurgeStats] = field(default_factory=dict)


def _csv_set(raw: str | list[Any] | None) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, list):
        return {str(part).strip() for part in raw if str(part).strip()}
    return {part.strip() for part in str(raw).split(",") if part.strip()}


def _preflight_queue_titles(
    page,
    store: SyncStore,
    *,
    account_id: str,
    max_days: float,
    keep_item_ids: set[str] | None = None,
    keep_autosell_ids: set[str] | None = None,
    keep_title_re: re.Pattern[str] | None = None,
) -> list[str]:
    """Age-eligible + live shelf titles for the Requieren atención force-delete pass."""
    from src.facebook.purge_old import (
        _catalog_by_autosell_id,
        _lookup_db_row,
        _posted_at_age_days,
        _should_keep_listing,
        is_purge_age_eligible,
        purge_unknown_age_when_total,
        resolve_listing_age_for_purge,
    )
    from src.facebook.remover import extract_item_id

    titles: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        t = re.sub(r"\s+", " ", (raw or "").strip())
        if len(t) < 4:
            return
        key = t.lower()
        if key in seen:
            return
        seen.add(key)
        titles.append(t)

    catalog = _catalog_by_autosell_id()
    try:
        listings = collect_selling_listings(page)
    except Exception as exc:
        print(
            f"  [{account_id}] preflight queue titles: shelf scan failed ({exc})",
            flush=True,
        )
        listings = []

    for card in listings:
        keep_reason = _should_keep_listing(
            card,
            keep_item_ids=keep_item_ids,
            keep_autosell_ids=keep_autosell_ids,
            keep_title_re=keep_title_re,
            store=store,
            account_id=account_id,
            catalog=catalog,
        )
        if keep_reason:
            continue
        row = _lookup_db_row(store, account_id, card.item_id)
        db_age = _posted_at_age_days(row)
        age, _source = resolve_listing_age_for_purge(
            live_age_days=card.age_days,
            live_age_text=card.age_text or "",
            live_card_text=card.card_text or "",
            db_age_days=db_age,
        )
        # Attention/duplicate cards often lack Listed-on — still queue the title.
        in_attention = "requieren" in (card.card_text or "").lower() or (
            "duplicate" in (card.card_text or "").lower()
            or "duplicado" in (card.card_text or "").lower()
        )
        eligible = (
            age is not None and is_purge_age_eligible(age, max_days)
        ) or (age is None and (purge_unknown_age_when_total(max_days) or in_attention))
        if not eligible:
            continue
        title = vehicle_title_from_card_text(card.card_text or "") or (
            card.card_text or ""
        )[:80]
        _add(title)
        if row is not None:
            autosell_id = str(row.get("autosell_id") or "")
            vehicle = catalog.get(autosell_id) if autosell_id else None
            if vehicle is not None:
                _add(getattr(vehicle, "marketplace_title", "") or "")
                _add(getattr(vehicle, "title", "") or "")
                _add(
                    f"{getattr(vehicle, 'year', '')} "
                    f"{getattr(vehicle, 'brand', '')} "
                    f"{getattr(vehicle, 'title', '')}".strip()
                )

    # sync.db live rows (covers GraphQL-only ids before DOM attention merge).
    for row in store.get_live_listings():
        if row["account_id"] != account_id:
            continue
        age = _posted_at_age_days(row)
        if age is None or not is_purge_age_eligible(age, max_days):
            if not purge_unknown_age_when_total(max_days):
                continue
        autosell_id = str(row.get("autosell_id") or "")
        if keep_autosell_ids and autosell_id in keep_autosell_ids:
            continue
        item_id = extract_item_id(row.get("fb_listing_url") or "") or ""
        if keep_item_ids and item_id and item_id in keep_item_ids:
            continue
        vehicle = catalog.get(autosell_id) if autosell_id else None
        if vehicle is not None:
            _add(getattr(vehicle, "marketplace_title", "") or "")
            _add(getattr(vehicle, "title", "") or "")
            _add(
                f"{getattr(vehicle, 'year', '')} "
                f"{getattr(vehicle, 'brand', '')} "
                f"{getattr(vehicle, 'title', '')}".strip()
            )

    print(
        f"  [{account_id}] preflight attention queue titles: {len(titles)}",
        flush=True,
    )
    return titles


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
        float(raw.get("max_days", DEFAULT_MAX_DAYS)),
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

    abort_default = bool(raw.get("abort_on_failed_deletes", True))
    abort_on_failed = env_bool(
        "FB_PREFLIGHT_ABORT_ON_FAIL",
        abort_default,
        "PREFLIGHT_ABORT_ON_FAIL",
    )

    return PreflightPurgeConfig(
        enabled=enabled,
        max_days=max_days,
        keep_autosell_ids=keep_ids,
        keep_item_ids=keep_items,
        keep_title_re=keep_title_re,
        max_deletes=max_deletes,
        delay_min_sec=float(raw.get("delay_min_sec", 2.0)),
        delay_max_sec=float(raw.get("delay_max_sec", 4.0)),
        abort_on_failed_deletes=abort_on_failed,
    )


def run_preflight_shelf_purge(
    config: dict,
    store: SyncStore,
    account_ids: list[str],
    *,
    root: Path,
    dry_run: bool = False,
    headless: bool | None = None,
    raise_on_failure: bool | None = None,
) -> PreflightPurgeResult:
    """Open each account session and purge shelf listings older than ``max_days``.

    Intended to run **before** slot allocation so ``sync.db`` live rows reflect
    the cleaned shelf. When ``dry_run`` is True, scans/report only (no deletes).

    GraphQL deletes fall back to Playwright automatically on failure / rate-limit
    (``FB_GQL_FALLBACK_UI``, default on). After the age purge, a dedicated
    ``Requieren atención`` pass force-deletes duplicate-blocked cards via
    ⋯ → Eliminar publicación (GraphQL OK often leaves them stuck on account_2).
    Any remaining failed delete aborts the sync cycle when
    ``abort_on_failed_deletes`` is True (default).
    """
    cfg = preflight_purge_config(config)
    result = PreflightPurgeResult(accounts=list(account_ids))
    should_raise = (
        cfg.abort_on_failed_deletes if raise_on_failure is None else raise_on_failure
    )

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

    print("=== Preflight shelf/history purge (7-day hard-delete first) ===", flush=True)
    print(
        f"Accounts: {', '.join(account_ids)}  max_days={cfg.max_days} "
        f"(delete age ≥ {cfg.max_days}d from live shelf Listed-on text; "
        f"sync.db is fallback only)  dry_run={dry_run} "
        f"abort_on_fail={should_raise and not dry_run}",
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

    from src.facebook.graphql.mode import graphql_enabled

    result.ran = True
    use_graphql = graphql_enabled()
    print(
        "Preflight discovery: Playwright DOM on /marketplace/you/selling "
        "(Listed on / Publicado / bare 10/24|24/10 dates; title from card block) "
        "— GraphQL HTTP shelf skipped when empty/400"
        + (" ; deletes prefer GraphQL+UI fallback" if use_graphql else ""),
        flush=True,
    )

    for account_id in account_ids:
        session_dir = resolve_session_dir(config, account_id, root)
        health = session_health_report(session_dir)
        print(
            f"--- preflight {account_id} session exists={health.get('exists')} ---",
            flush=True,
        )
        try:
            # Always open browser so GraphQL deletes can fall back to DOM.
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
                    if should_raise and not dry_run:
                        result.aborted = True
                        raise PreflightPurgeAborted(
                            f"Preflight abort: {account_id} not logged in — "
                            f"cannot enforce 7-day hard-delete before sync"
                        )
                    continue
                # Capture age-eligible titles BEFORE GraphQL deletes so we can
                # still match Requieren atención cards after a fake GraphQL OK.
                queue_titles = _preflight_queue_titles(
                    page,
                    store,
                    account_id=account_id,
                    max_days=cfg.max_days,
                    keep_item_ids=cfg.keep_item_ids or None,
                    keep_autosell_ids=cfg.keep_autosell_ids or None,
                    keep_title_re=cfg.keep_title_re,
                )
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
                    # Never miss Oct 24/28 shelf cards when GraphQL returns 0.
                    force_dom_discovery=True,
                )
                # account_2: GraphQL delete returns OK but cards stay under
                # ``Requieren atención`` — force ⋯ → Eliminar publicación.
                attn = purge_requieren_atencion_matches(
                    page,
                    account_id=account_id,
                    queue_titles=queue_titles,
                    dry_run=dry_run,
                    delay_min_sec=cfg.delay_min_sec,
                    delay_max_sec=cfg.delay_max_sec,
                    also_purge_duplicate_banners=True,
                )
                if attn.found or attn.deleted or attn.failed:
                    stats.found += attn.found
                    stats.deleted += attn.deleted
                    stats.failed += attn.failed
                    stats.dry_run_candidates += attn.dry_run_candidates
                    stats.ui_fallback_deletes += attn.ui_fallback_deletes
                    if attn.failed and not dry_run:
                        stats.incomplete = True
                    print(
                        f"  [{account_id}] Requieren atención pass: "
                        f"found={attn.found} deleted={attn.deleted} "
                        f"failed={attn.failed}",
                        flush=True,
                    )
        except PreflightPurgeAborted:
            raise
        except Exception as exc:
            print(f"ERROR preflight {account_id}: {exc}", flush=True)
            result.failed += 1
            if should_raise and not dry_run:
                result.aborted = True
                raise PreflightPurgeAborted(
                    f"Preflight abort: {account_id} purge crashed ({exc}) — "
                    f"refusing create/sync until 7-day hard-delete succeeds"
                ) from exc
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
                f" ui_fallback={stats.ui_fallback_deletes}"
                if stats.ui_fallback_deletes
                else ""
            )
            + (
                f" dry_candidates={stats.dry_run_candidates}"
                if dry_run
                else ""
            )
            + (" INCOMPLETE" if stats.incomplete else ""),
            flush=True,
        )
        print("", flush=True)

        if (
            should_raise
            and not dry_run
            and (stats.failed > 0 or stats.incomplete)
        ):
            result.aborted = True
            raise PreflightPurgeAborted(
                f"Preflight abort: {account_id} left {stats.failed} "
                f"unconfirmed/failed 7-day hard-delete(s) "
                f"(deleted={stats.deleted}) — refusing create/sync"
            )

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

    if should_raise and not dry_run and result.failed > 0:
        result.aborted = True
        raise PreflightPurgeAborted(
            f"Preflight abort: {result.failed} failed 7-day hard-delete(s) "
            f"across accounts — refusing create/sync"
        )
    return result
