"""GraphQL/HTTP Marketplace purge — list active items + delete via mutation."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from src.facebook.graphql.client import GraphQLClient
from src.facebook.graphql.errors import (
    FacebookGraphqlFallback,
    is_phantom_delete_error,
)
from src.facebook.graphql.remover import delete_listing_graphql
from src.facebook.graphql.selling import ActiveListing, fetch_active_selling_listings
from src.facebook.graphql.session_ctx import FbSessionContext
from src.facebook.purge_old import (
    PurgeStats,
    ShelfListing,
    _catalog_by_autosell_id,
    _lookup_db_row,
    _posted_at_age_days,
    _should_keep_listing,
    purge_unknown_age_when_total,
)
from src.facebook.util import random_delay
from src.store.db import SyncStore


def purge_old_listings_graphql(
    session: FbSessionContext,
    store: SyncStore,
    *,
    account_id: str,
    max_days: float = 3.0,
    dry_run: bool = False,
    max_deletes: int | None = None,
    delay_min_sec: float = 2.0,
    delay_max_sec: float = 4.0,
    keep_item_ids: set[str] | None = None,
    keep_autosell_ids: set[str] | None = None,
    keep_title_re: re.Pattern[str] | None = None,
    root: Path | None = None,
    client: GraphQLClient | None = None,
) -> PurgeStats:
    """Discover active listings via GraphQL/HTTP and hard-delete old ones.

    Uses ``data/graphql/delete_listing.json`` (``useCometMarketplaceForSaleItemDeleteMutation``).
    Does not touch Playwright DOM.
    """
    stats = PurgeStats()
    owns = client is None
    gql = client or GraphQLClient(session)
    try:
        listings = fetch_active_selling_listings(
            session, root=root, client=gql
        )
        # Enrich / fill from sync.db when discovery is thin or ages missing.
        listings = _merge_with_sync_db(listings, store, account_id)
        stats.found = len(listings)
        print(
            f"  [{account_id}] GraphQL shelf: {stats.found} listing(s)",
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

            age = item.age_days
            source = item.source
            if age is None:
                row = _lookup_db_row(store, account_id, item.item_id)
                age = _posted_at_age_days(row)
                if age is not None:
                    source = "sync.db posted_at"
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
                            source=source,
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
            if age <= float(max_days):
                stats.skipped_young += 1
                print(
                    f"  SKIP young item={item.item_id} age={age:.1f}d "
                    f"(≤{max_days}d, via {source})",
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

        for item in targets:
            if max_deletes is not None and stats.deleted >= max_deletes:
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
            try:
                ok = delete_listing_graphql(
                    session,
                    item.href,
                    autosell_id=autosell_id,
                    root=root,
                    client=gql,
                    verify=False,
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
                print(f"  FAIL item={item.item_id}: {exc}", flush=True)
                ok = False

            if not ok:
                stats.failed += 1
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

        if stats.skipped_phantom:
            print(
                f"  [{account_id}] ignored phantom/archived "
                f"(field_exception): {stats.skipped_phantom}",
                flush=True,
            )
    finally:
        if owns:
            gql.close()
    return stats


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
        if row["account_id"] != account_id:
            continue
        url = row["fb_listing_url"] or ""
        from src.facebook.graphql.remover import extract_item_id

        item_id = extract_item_id(url)
        if not item_id:
            continue
        age = _posted_at_age_days(row)
        existing = by_id.get(item_id)
        if existing is None:
            by_id[item_id] = ActiveListing(
                item_id=item_id,
                title=str(row["autosell_id"]),
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

