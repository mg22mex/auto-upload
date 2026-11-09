"""GraphQL/HTTP Marketplace purge — list active items + delete via mutation."""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import Page

from src.facebook.graphql.client import GraphQLClient
from src.facebook.graphql.errors import (
    FacebookGraphqlFallback,
    is_phantom_delete_error,
    is_rate_limit_error,
)
from src.facebook.graphql.remover import delete_listing_graphql, extract_item_id
from src.facebook.graphql.selling import ActiveListing, fetch_active_selling_listings
from src.facebook.graphql.session_ctx import FbSessionContext
from src.facebook.purge_old import (
    PurgeStats,
    ShelfListing,
    _catalog_by_autosell_id,
    _lookup_db_row,
    _posted_at_age_days,
    _row_as_dict,
    _should_keep_listing,
    hard_delete_listing_via_playwright,
    is_purge_age_eligible,
    purge_unknown_age_when_total,
    resolve_listing_age_for_purge,
)
from src.facebook.util import env_bool, random_delay
from src.store.db import SyncStore


def _active_from_dom_shelf(page: Page) -> list[ActiveListing]:
    """Load /marketplace/you/selling in Playwright and parse Listed-on ages."""
    from src.facebook.purge_old import collect_selling_listings

    from src.facebook.purge_old import vehicle_title_from_card_text

    cards = collect_selling_listings(page)
    out: list[ActiveListing] = []
    for card in cards:
        title = vehicle_title_from_card_text(card.card_text or "") or (
            card.card_text or ""
        )[:120]
        out.append(
            ActiveListing(
                item_id=card.item_id,
                title=title,
                age_days=card.age_days,
                age_text=card.age_text,
                href=card.href if str(card.item_id).isdigit() else (card.href or ""),
                source="playwright_dom",
            )
        )
    return out


def _merge_dom_discovery(
    existing: list[ActiveListing],
    dom_listings: list[ActiveListing],
) -> list[ActiveListing]:
    """Prefer Playwright DOM age/Listed-on text over GraphQL/HTTP rows."""
    by_id: dict[str, ActiveListing] = {item.item_id: item for item in existing}
    for item in dom_listings:
        prev = by_id.get(item.item_id)
        if prev is None:
            by_id[item.item_id] = item
            continue
        # DOM wins for age signals (Listed on 10/24 etc.).
        by_id[item.item_id] = ActiveListing(
            item_id=item.item_id,
            title=item.title or prev.title,
            age_days=(
                item.age_days if item.age_days is not None else prev.age_days
            ),
            age_text=item.age_text or prev.age_text,
            href=item.href or prev.href,
            source="playwright_dom",
        )
    return list(by_id.values())


def purge_old_listings_graphql(
    session: FbSessionContext,
    store: SyncStore,
    *,
    account_id: str,
    max_days: float = 7.0,
    dry_run: bool = False,
    max_deletes: int | None = None,
    delay_min_sec: float = 2.0,
    delay_max_sec: float = 4.0,
    keep_item_ids: set[str] | None = None,
    keep_autosell_ids: set[str] | None = None,
    keep_title_re: re.Pattern[str] | None = None,
    root: Path | None = None,
    client: GraphQLClient | None = None,
    page: Page | None = None,
    force_dom_discovery: bool = False,
) -> PurgeStats:
    """Discover active listings via GraphQL/HTTP and hard-delete old ones.

    Uses ``data/graphql/delete_listing.json``. When GraphQL discovery returns 0
    items / errors, or ``force_dom_discovery`` is set, loads the selling shelf
    in Playwright and parses ``Listed on…`` DOM text. Delete failures /
    rate-limits also fall back to Playwright when ``page`` is provided.
    """
    stats = PurgeStats()
    owns = client is None
    gql = client or GraphQLClient(session)
    try:
        listings: list[ActiveListing] = []
        discovery_error: Exception | None = None
        if not force_dom_discovery:
            try:
                listings = fetch_active_selling_listings(
                    session, root=root, client=gql
                )
            except Exception as exc:
                discovery_error = exc
                status = getattr(exc, "status_code", None)
                print(
                    f"  [{account_id}] GraphQL/HTTP selling discovery failed"
                    f"{f' HTTP {status}' if status else ''}: {exc}",
                    flush=True,
                )
                listings = []

        # Enrich / fill from sync.db when discovery is thin or ages missing.
        listings = _merge_with_sync_db(listings, store, account_id)
        print(
            f"  [{account_id}] GraphQL shelf: {len(listings)} listing(s)",
            flush=True,
        )

        need_dom = force_dom_discovery or len(listings) == 0 or discovery_error is not None
        if need_dom and page is not None:
            why = (
                "forced"
                if force_dom_discovery
                else ("discovery error" if discovery_error else "0 GraphQL/HTTP items")
            )
            print(
                f"  [{account_id}] Playwright DOM selling discovery "
                f"({why}) → https://www.facebook.com/marketplace/you/selling",
                flush=True,
            )
            try:
                dom_listings = _active_from_dom_shelf(page)
                listings = _merge_dom_discovery(listings, dom_listings)
                print(
                    f"  [{account_id}] Playwright shelf: {len(dom_listings)} "
                    f"card(s); combined={len(listings)}",
                    flush=True,
                )
            except Exception as exc:
                print(
                    f"  [{account_id}] Playwright DOM discovery failed: {exc}",
                    flush=True,
                )
                if not listings and discovery_error is not None:
                    raise discovery_error from exc

        stats.found = len(listings)
        print(
            f"  [{account_id}] purge discovery total: {stats.found} listing(s)",
            flush=True,
        )

        catalog = _catalog_by_autosell_id()
        targets: list[ActiveListing] = []
        skipped_keep = 0
        for item in listings:
            card = ShelfListing(
                item_id=item.item_id,
                href=item.href,
                age_days=item.age_days,
                age_text=item.age_text,
                card_text=item.title or item.card_text_like(),
            )
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
                skipped_keep += 1
                print(f"  KEEP item={item.item_id} ({keep_reason})", flush=True)
                continue

            row = _lookup_db_row(store, account_id, item.item_id)
            db_age = _posted_at_age_days(row)
            live_blob = " ".join(
                part
                for part in (
                    item.age_text or "",
                    item.title or "",
                    item.card_text_like() if hasattr(item, "card_text_like") else "",
                )
                if part
            )
            age, source = resolve_listing_age_for_purge(
                live_age_days=item.age_days,
                live_age_text=item.age_text or "",
                live_card_text=live_blob,
                db_age_days=db_age,
            )
            if age is None:
                if purge_unknown_age_when_total(max_days):
                    print(
                        f"  PURGE unknown age item={item.item_id} "
                        f"(max_days={max_days} total mode) "
                        f"title={item.title[:60]!r}",
                        flush=True,
                    )
                    targets.append(
                        ActiveListing(
                            item_id=item.item_id,
                            title=item.title,
                            age_days=None,
                            age_text=item.age_text or "unknown age (total purge)",
                            href=item.href,
                            source=item.source or source,
                        )
                    )
                    continue
                stats.skipped_unknown_age += 1
                print(
                    f"  SKIP unknown age item={item.item_id} "
                    f"title={item.title[:60]!r}",
                    flush=True,
                )
                continue
            if not is_purge_age_eligible(age, max_days):
                stats.skipped_young += 1
                print(
                    f"  SKIP young item={item.item_id} age={age:.1f}d "
                    f"(<{max_days}d, via {source})",
                    flush=True,
                )
                continue
            targets.append(
                ActiveListing(
                    item_id=item.item_id,
                    title=item.title,
                    age_days=age,
                    age_text=item.age_text or f"{age:.1f}d via {source}",
                    href=item.href,
                    source=source,
                )
            )

        print(
            f"  [{account_id}] GraphQL purge candidates: {len(targets)} "
            f"(skipped young={stats.skipped_young}, "
            f"unknown_age={stats.skipped_unknown_age}, kept={skipped_keep}; "
            f"phantom field_exception counted after deletes)",
            flush=True,
        )

        allow_ui = env_bool("FB_GQL_FALLBACK_UI", True) and page is not None
        remaining_after_cap = 0
        for idx, item in enumerate(targets):
            if max_deletes is not None and stats.deleted >= max_deletes:
                remaining_after_cap = len(targets) - idx
                print(f"  hit --max-deletes={max_deletes}; stopping", flush=True)
                break
            age_label = (
                f"{item.age_days:.1f}d"
                if item.age_days is not None
                else "unknown"
            )
            print(
                f"  PURGE item={item.item_id} age={age_label} "
                f"({item.age_text}) [graphql]",
                flush=True,
            )
            if dry_run:
                stats.dry_run_candidates += 1
                continue

            row = _lookup_db_row(store, account_id, item.item_id)
            autosell_id = (
                str(row["autosell_id"]) if row is not None else item.item_id
            )
            ok = False
            # Title-only DOM cards (``dom-…``) skip GraphQL — Playwright title delete.
            try:
                if str(item.item_id).isdigit() and item.href:
                    ok = delete_listing_graphql(
                        session,
                        item.href,
                        autosell_id=autosell_id,
                        root=root,
                        client=gql,
                        verify=True,
                    )
                else:
                    print(
                        f"  SKIP GraphQL (no numeric item_id) item={item.item_id} "
                        f"title={item.title[:60]!r} — UI title delete",
                        flush=True,
                    )
            except Exception as exc:
                # Unknown-age ghosts: FB field_exception on delete — ignore, not FAIL.
                if item.age_days is None and is_phantom_delete_error(exc):
                    stats.skipped_phantom += 1
                    print(
                        f"  SKIP phantom/archived item={item.item_id} "
                        f"(field_exception; unknown age — ignored)",
                        flush=True,
                    )
                    _clear_sync_mapping_if_any(store, account_id, item.item_id)
                    continue
                why = "rate-limit" if is_rate_limit_error(exc) else "error"
                print(
                    f"  GraphQL delete {why} item={item.item_id}: {exc}",
                    flush=True,
                )
                ok = False

            if not ok and allow_ui:
                print(
                    f"  RETRY item={item.item_id} via Playwright shelf delete "
                    f"(title-match fallback if no item_id href)",
                    flush=True,
                )
                try:
                    card_blob = " ".join(
                        part
                        for part in (
                            item.title or "",
                            item.age_text or "",
                            item.card_text_like()
                            if hasattr(item, "card_text_like")
                            else "",
                        )
                        if part
                    )
                    ok = hard_delete_listing_via_playwright(
                        page,
                        item_id=item.item_id,
                        listing_url=item.href,
                        autosell_id=autosell_id,
                        store=store,
                        account_id=account_id,
                        log_dir=(root / "data/logs/facebook/purge")
                        if root
                        else None,
                        vehicle_title=item.title or "",
                        card_text=card_blob,
                    )
                    if ok:
                        stats.ui_fallback_deletes += 1
                except Exception as exc:
                    print(
                        f"  Playwright fallback FAIL item={item.item_id}: {exc}",
                        flush=True,
                    )
                    ok = False

            if not ok:
                stats.failed += 1
                print(
                    f"  FAIL item={item.item_id}: delete unconfirmed "
                    f"(GraphQL + UI)",
                    flush=True,
                )
                continue

            row = _lookup_db_row(store, account_id, item.item_id)
            if row is not None:
                store.mark_fb_listing_removed(
                    row["autosell_id"], account_id, clear_url=True
                )
                print(
                    f"  OK deleted item={item.item_id} "
                    f"autosell_id={row['autosell_id']} (sync.db cleared)",
                    flush=True,
                )
            else:
                print(
                    f"  OK deleted item={item.item_id} "
                    f"(no live sync.db mapping)",
                    flush=True,
                )
            stats.deleted += 1
            random_delay(delay_min_sec, delay_max_sec)

        if remaining_after_cap > 0:
            stats.incomplete = True
            stats.failed += remaining_after_cap
            print(
                f"  [{account_id}] INCOMPLETE: {remaining_after_cap} age-eligible "
                f"listing(s) left after max_deletes={max_deletes}",
                flush=True,
            )

        if stats.skipped_phantom:
            print(
                f"  [{account_id}] ignored phantom/archived "
                f"(field_exception): {stats.skipped_phantom}",
                flush=True,
            )
        if stats.ui_fallback_deletes:
            print(
                f"  [{account_id}] Playwright fallback deletes: "
                f"{stats.ui_fallback_deletes}",
                flush=True,
            )
    finally:
        if owns:
            gql.close()
    return stats


def mass_purge_all_listings_graphql(
    session: FbSessionContext,
    store: SyncStore,
    *,
    account_id: str,
    dry_run: bool = False,
    max_deletes: int | None = None,
    delay_sec: float = 0.15,
    root: Path | None = None,
    client: GraphQLClient | None = None,
) -> PurgeStats:
    """Aggressive GraphQL wipe: every discovered item id, never stop on errors.

    Discovers ids via selling GraphQL/HTTP (all shelves) + sync.db URLs, then
    issues ``useDirectMarketplaceForSaleItemDeleteMutation`` (or captured
    ``delete_listing.json`` friendly_name) sequentially. field_exception and
    other failures are logged and skipped — the loop continues.
    """
    stats = PurgeStats()
    owns = client is None
    gql = client or GraphQLClient(session)
    try:
        listings = fetch_active_selling_listings(
            session, root=root, client=gql, max_pages=50
        )
        listings = _merge_with_sync_db_all(listings, store, account_id)
        stats.found = len(listings)
        print(
            f"  [{account_id}] MASS GraphQL: {stats.found} listing id(s) to wipe "
            f"(delay={delay_sec:.2f}s, continue-on-error)",
            flush=True,
        )
        if dry_run:
            stats.dry_run_candidates = stats.found
            for item in listings[:50]:
                print(
                    f"  DRY-RUN would delete item={item.item_id} "
                    f"title={item.title[:50]!r}",
                    flush=True,
                )
            if stats.found > 50:
                print(f"  … and {stats.found - 50} more", flush=True)
            return stats

        t0 = time.monotonic()
        for idx, item in enumerate(listings, start=1):
            if max_deletes is not None and stats.deleted >= max_deletes:
                print(f"  hit --max-deletes={max_deletes}; stopping", flush=True)
                break
            href = item.href or (
                f"https://www.facebook.com/marketplace/item/{item.item_id}/"
            )
            row = _lookup_db_row(store, account_id, item.item_id)
            autosell_id = (
                str(row["autosell_id"]) if row is not None else item.item_id
            )
            try:
                ok = delete_listing_graphql(
                    session,
                    href,
                    autosell_id=autosell_id,
                    root=root,
                    client=gql,
                    verify=False,
                )
            except Exception as exc:
                if is_phantom_delete_error(exc):
                    stats.skipped_phantom += 1
                    print(
                        f"  [{idx}/{stats.found}] PHANTOM item={item.item_id} "
                        f"— continue ({exc})",
                        flush=True,
                    )
                    _clear_sync_mapping_if_any(store, account_id, item.item_id)
                else:
                    stats.failed += 1
                    print(
                        f"  [{idx}/{stats.found}] FAIL item={item.item_id} "
                        f"— continue ({exc})",
                        flush=True,
                    )
                if delay_sec > 0:
                    time.sleep(delay_sec)
                continue

            if not ok:
                stats.failed += 1
                print(
                    f"  [{idx}/{stats.found}] FAIL item={item.item_id} "
                    f"(delete returned False) — continue",
                    flush=True,
                )
            else:
                stats.deleted += 1
                if row is not None:
                    try:
                        store.mark_fb_listing_removed(
                            row["autosell_id"], account_id, clear_url=True
                        )
                    except Exception:
                        pass
                if idx == 1 or idx % 25 == 0 or idx == stats.found:
                    elapsed = time.monotonic() - t0
                    print(
                        f"  [{idx}/{stats.found}] OK deleted={stats.deleted} "
                        f"failed={stats.failed} phantom={stats.skipped_phantom} "
                        f"({elapsed:.0f}s)",
                        flush=True,
                    )
            if delay_sec > 0:
                time.sleep(delay_sec)

        print(
            f"  [{account_id}] MASS GraphQL done: deleted={stats.deleted} "
            f"failed={stats.failed} phantom={stats.skipped_phantom} "
            f"found={stats.found}",
            flush=True,
        )
    finally:
        if owns:
            gql.close()
    return stats


def _merge_with_sync_db_all(
    listings: list[ActiveListing],
    store: SyncStore,
    account_id: str,
) -> list[ActiveListing]:
    """Union discovery with every sync.db row that has a Marketplace URL."""
    by_id = {item.item_id: item for item in listings}
    try:
        rows = store._conn.execute(
            """
            SELECT autosell_id, account_id, fb_listing_url, status, posted_at
            FROM fb_listings
            WHERE account_id = ?
              AND fb_listing_url IS NOT NULL
              AND fb_listing_url != ''
            """,
            (account_id,),
        ).fetchall()
    except Exception:
        rows = []
    for row in rows:
        data = _row_as_dict(row)
        url = str(data.get("fb_listing_url") or "")
        item_id = extract_item_id(url)
        if not item_id or item_id in by_id:
            continue
        age = _posted_at_age_days(data)
        by_id[item_id] = ActiveListing(
            item_id=item_id,
            title=str(data.get("autosell_id") or ""),
            age_days=age,
            age_text=f"sync.db status={data.get('status') or ''}",
            href=url
            if url.startswith("http")
            else f"https://www.facebook.com/marketplace/item/{item_id}/",
            source="sync.db",
        )
    return list(by_id.values())


def _clear_sync_mapping_if_any(
    store: SyncStore, account_id: str, item_id: str
) -> None:
    row = _lookup_db_row(store, account_id, item_id)
    if row is None:
        return
    try:
        store.mark_fb_listing_removed(
            row["autosell_id"], account_id, clear_url=True
        )
        print(
            f"  sync.db cleared for phantom item={item_id} "
            f"autosell_id={row['autosell_id']}",
            flush=True,
        )
    except Exception as exc:
        print(f"  WARNING: sync.db clear failed for {item_id}: {exc}", flush=True)


def _merge_with_sync_db(
    listings: list[ActiveListing],
    store: SyncStore,
    account_id: str,
) -> list[ActiveListing]:
    by_id = {item.item_id: item for item in listings}
    for row in store.get_live_listings():
        data = _row_as_dict(row)
        if data.get("account_id") != account_id:
            continue
        url = str(data.get("fb_listing_url") or "")
        item_id = extract_item_id(url)
        if not item_id:
            continue
        age = _posted_at_age_days(data)
        existing = by_id.get(item_id)
        if existing is None:
            by_id[item_id] = ActiveListing(
                item_id=item_id,
                title=str(data.get("autosell_id") or ""),
                age_days=age,
                age_text=(
                    f"sync.db posted_at age={age:.1f}d"
                    if age is not None
                    else "sync.db (no posted_at)"
                ),
                href=url
                if url.startswith("http")
                else f"https://www.facebook.com/marketplace/item/{item_id}/",
                source="sync.db",
            )
        elif existing.age_days is None and age is not None:
            existing.age_days = age
            existing.age_text = f"sync.db posted_at age={age:.1f}d"
    return list(by_id.values())

