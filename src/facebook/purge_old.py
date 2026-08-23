"""Purge Marketplace selling-shelf listings older than a day threshold."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Page

from src.facebook.remover import (
    SELLING_URL,
    _ACTIVE_SELLING_SHELVES,
    _delete_shelf_link,
    _find_item_link_scrolled,
    _remove_from_selling_shelf,
    _sort_selling_oldest_first,
    extract_item_id,
    remove_from_selling_by_title,
)
from src.facebook.renewer import _match_needles
from src.facebook.util import random_delay
from src.inventory.snapshot import load_catalog_snapshot
from src.store.db import SyncStore

_MONTHS_ES = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}
_MONTHS_EN = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


@dataclass
class ShelfListing:
    item_id: str
    href: str
    age_days: float | None
    age_text: str
    card_text: str


@dataclass
class PurgeStats:
    found: int = 0
    skipped_young: int = 0
    skipped_unknown_age: int = 0
    deleted: int = 0
    failed: int = 0
    dry_run_candidates: int = 0


def parse_listing_age_days(text: str, *, now: datetime | None = None) -> float | None:
    """Parse Marketplace age strings (EN/ES) into approximate age in days.

    Returns ``None`` when no age phrase is recognized.
    """
    blob = " ".join((text or "").lower().split())
    if not blob:
        return None
    when = now or datetime.now(timezone.utc)

    if re.search(r"\b(just now|hace un momento|hace unos momentos|publicado hoy|listed today)\b", blob):
        return 0.0
    if re.search(r"\b(yesterday|ayer)\b", blob):
        return 1.0

    m = re.search(
        r"(?:publicado|listed|publicad[oa])?\s*(?:hace\s+)?(\d+)\s*min(?:uto)?s?\b",
        blob,
    )
    if m:
        return max(0.0, int(m.group(1)) / (60.0 * 24.0))

    m = re.search(
        r"(?:publicado|listed)?\s*(?:hace\s+)?(\d+)\s*h(?:oras?|ours?)?\b"
        r"|(?:listed|publicado)\s+(\d+)\s*hours?\s+ago",
        blob,
    )
    if m:
        hours = int(m.group(1) or m.group(2))
        return max(0.0, hours / 24.0)

    m = re.search(
        r"(?:publicado\s+hace|hace|listed)\s+(\d+)\s*d[ií]as?(?:\s+ago)?"
        r"|(\d+)\s*days?\s+ago",
        blob,
    )
    if m:
        return float(int(m.group(1) or m.group(2)))

    m = re.search(
        r"(?:publicado\s+hace|hace|listed)\s+(\d+)\s*semanas?(?:\s+ago)?"
        r"|(\d+)\s*weeks?\s+ago",
        blob,
    )
    if m:
        return float(int(m.group(1) or m.group(2)) * 7)

    m = re.search(
        r"(?:publicado\s+hace|hace|listed)\s+(\d+)\s*mes(?:es)?(?:\s+ago)?"
        r"|(\d+)\s*months?\s+ago",
        blob,
    )
    if m:
        return float(int(m.group(1) or m.group(2)) * 30)

    # Absolute: "Publicado el 20 de agosto" / "Listed on August 20"
    m = re.search(
        r"(?:publicado\s+el|listed\s+on)\s+(\d{1,2})\s+de\s+([a-záéíóúñ]+)",
        blob,
    )
    if m:
        day = int(m.group(1))
        month = _MONTHS_ES.get(m.group(2))
        if month:
            year = when.year
            try:
                posted = datetime(year, month, day, tzinfo=timezone.utc)
            except ValueError:
                return None
            if posted > when:
                posted = posted.replace(year=year - 1)
            return max(0.0, (when - posted).total_seconds() / 86400.0)

    m = re.search(
        r"listed\s+on\s+([a-z]+)\s+(\d{1,2})(?:,\s*(\d{4}))?",
        blob,
    )
    if m:
        month = _MONTHS_EN.get(m.group(1))
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else when.year
        if month:
            try:
                posted = datetime(year, month, day, tzinfo=timezone.utc)
            except ValueError:
                return None
            if posted > when and not m.group(3):
                posted = posted.replace(year=year - 1)
            return max(0.0, (when - posted).total_seconds() / 86400.0)

    return None


def _card_blob_for_link(link) -> str:
    parts: list[str] = []
    try:
        href = link.get_attribute("href") or ""
        if href:
            parts.append(href)
    except Exception:
        pass
    try:
        parts.append(link.inner_text(timeout=1_000) or "")
    except Exception:
        pass
    for xpath in (
        "xpath=ancestor::div[@role='article'][1]",
        "xpath=ancestor::div[6]",
        "xpath=ancestor::div[4]",
    ):
        try:
            node = link.locator(xpath).first
            if node.count() > 0:
                parts.append(node.inner_text(timeout=1_000) or "")
                break
        except Exception:
            continue
    return " ".join(p for p in parts if p)


def collect_selling_listings(
    page: Page,
    *,
    max_scrolls: int = 20,
) -> list[ShelfListing]:
    """Scroll `/you/selling` (active shelves) and collect item ids + ages."""
    found: dict[str, ShelfListing] = {}
    shelves = list(_ACTIVE_SELLING_SHELVES[:3]) or [SELLING_URL]

    for shelf in shelves:
        try:
            page.goto(shelf, wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_timeout(2_500)
        except Exception as exc:
            print(f"  WARNING: could not open {shelf}: {exc}", flush=True)
            continue

        stagnant = 0
        for _ in range(max_scrolls):
            before = len(found)
            links = page.locator('a[href*="/marketplace/item/"]')
            try:
                count = links.count()
            except Exception:
                count = 0
            if not isinstance(count, int):
                count = 0
            for i in range(min(count, 120)):
                loc = links.nth(i)
                try:
                    href = loc.get_attribute("href") or ""
                except Exception:
                    continue
                item_id = extract_item_id(href)
                if not item_id or item_id in found:
                    continue
                blob = _card_blob_for_link(loc)
                age = parse_listing_age_days(blob)
                age_snip = ""
                m = re.search(
                    r"(publicado[^\n]{0,40}|listed[^\n]{0,40}|hace\s+\d+[^\n]{0,20}"
                    r"|\d+\s+days?\s+ago|\d+\s+d[ií]as?)",
                    blob,
                    re.I,
                )
                if m:
                    age_snip = m.group(0).strip()
                found[item_id] = ShelfListing(
                    item_id=item_id,
                    href=href,
                    age_days=age,
                    age_text=age_snip,
                    card_text=blob[:240],
                )
            if len(found) == before:
                stagnant += 1
                if stagnant >= 3:
                    break
            else:
                stagnant = 0
            try:
                page.mouse.wheel(0, 2600)
                page.wait_for_timeout(800)
            except Exception:
                break

    return list(found.values())


def _catalog_by_autosell_id() -> dict[str, Any]:
    for candidate in (
        Path("data/snapshots/catalog_latest.json"),
        Path("data/catalog_latest.json"),
    ):
        if candidate.is_file():
            try:
                vehicles = load_catalog_snapshot(candidate)
                return {v.autosell_id: v for v in vehicles}
            except Exception as exc:
                print(f"  WARNING: catalog load failed ({candidate}): {exc}", flush=True)
    return {}


def _title_needles_for_row(
    store: SyncStore,
    account_id: str,
    card: ShelfListing,
    catalog: dict[str, Any],
    page: Page,
) -> list[str]:
    row = _lookup_db_row(store, account_id, card.item_id)
    autosell_id = str(row["autosell_id"]) if row is not None else ""
    vehicle = catalog.get(autosell_id) if autosell_id else None
    if vehicle is not None:
        return _match_needles(vehicle)

    # Fallback: scrape title from detail page.
    listing_url = card.href
    if listing_url and not listing_url.startswith("http"):
        listing_url = f"https://www.facebook.com{listing_url}"
    if not listing_url:
        return []
    try:
        page.goto(listing_url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(2_000)
        title = page.evaluate(
            """() => {
              const h = document.querySelector("h1");
              if (h && h.innerText) return h.innerText.trim();
              const t = document.title || "";
              return t.replace(/\\s*\\|\\s*Facebook.*/i, "")
                      .replace(/^Marketplace\\s*-\\s*/i, "").trim();
            }"""
        )
        if isinstance(title, str) and len(title) >= 5:
            return [title]
    except Exception:
        pass
    return []


def _lookup_db_row(
    store: SyncStore, account_id: str, item_id: str
) -> Any | None:
    for row in store.get_live_listings():
        if row["account_id"] != account_id:
            continue
        url = row["fb_listing_url"] or ""
        if item_id and item_id in url:
            return row
    return None


def _posted_at_age_days(row: Any) -> float | None:
    raw = row["posted_at"] if row is not None and "posted_at" in row.keys() else None
    if not raw:
        return None
    try:
        posted = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return max(0.0, (now - posted).total_seconds() / 86400.0)
    except ValueError:
        return None


def purge_old_listings_on_page(
    page: Page,
    store: SyncStore,
    *,
    account_id: str,
    max_days: float = 3.0,
    dry_run: bool = False,
    max_deletes: int | None = None,
    delay_min_sec: float = 2.0,
    delay_max_sec: float = 4.0,
) -> PurgeStats:
    """Delete selling-shelf cards older than ``max_days``; update sync.db."""
    from src.facebook.remover import remove_vehicle_listing

    stats = PurgeStats()
    listings = collect_selling_listings(page)
    stats.found = len(listings)
    print(
        f"  [{account_id}] shelf scan: {stats.found} listing(s) with item links",
        flush=True,
    )

    # FB sometimes renders selling without /marketplace/item/ anchors.
    # Fall back to sync.db live rows so age-based purge still works.
    used_db_fallback = False
    if not listings:
        used_db_fallback = True
        db_cards: list[ShelfListing] = []
        for row in store.get_live_listings():
            if row["account_id"] != account_id:
                continue
            url = row["fb_listing_url"] or ""
            item_id = extract_item_id(url)
            if not item_id:
                continue
            age = _posted_at_age_days(row)
            db_cards.append(
                ShelfListing(
                    item_id=item_id,
                    href=url,
                    age_days=age,
                    age_text=(
                        f"sync.db posted_at age={age:.1f}d"
                        if age is not None
                        else "sync.db (no posted_at)"
                    ),
                    card_text=f"{row['autosell_id']} {url}",
                )
            )
        listings = db_cards
        stats.found = len(listings)
        print(
            f"  [{account_id}] shelf empty in DOM — using sync.db live rows: "
            f"{stats.found}",
            flush=True,
        )

    targets: list[ShelfListing] = []
    for card in listings:
        age = card.age_days
        source = "ui" if "sync.db" not in (card.age_text or "") else "sync.db"
        if age is None:
            row = _lookup_db_row(store, account_id, card.item_id)
            age = _posted_at_age_days(row)
            if age is not None:
                source = "sync.db posted_at"
        if age is None:
            stats.skipped_unknown_age += 1
            print(
                f"  SKIP unknown age item={card.item_id} "
                f"text={card.age_text or card.card_text[:60]!r}",
                flush=True,
            )
            continue
        if age <= float(max_days):
            stats.skipped_young += 1
            print(
                f"  SKIP young item={card.item_id} age={age:.1f}d "
                f"(≤{max_days}d, via {source})",
                flush=True,
            )
            continue
        targets.append(
            ShelfListing(
                item_id=card.item_id,
                href=card.href,
                age_days=age,
                age_text=card.age_text or f"{age:.1f}d via {source}",
                card_text=card.card_text,
            )
        )

    print(
        f"  [{account_id}] purge candidates: {len(targets)} "
        f"(skipped young={stats.skipped_young}, unknown_age={stats.skipped_unknown_age})",
        flush=True,
    )

    catalog = _catalog_by_autosell_id() if not dry_run else {}
    if used_db_fallback and not dry_run:
        _sort_selling_oldest_first(page)

    for card in targets:
        if max_deletes is not None and stats.deleted >= max_deletes:
            print(f"  hit --max-deletes={max_deletes}; stopping", flush=True)
            break
        print(
            f"  PURGE item={card.item_id} age={card.age_days:.1f}d "
            f"({card.age_text})",
            flush=True,
        )
        if dry_run:
            stats.dry_run_candidates += 1
            continue

        listing_url = card.href
        if listing_url and not listing_url.startswith("http"):
            listing_url = f"https://www.facebook.com{listing_url}"
        row = _lookup_db_row(store, account_id, card.item_id)
        autosell_id = str(row["autosell_id"]) if row is not None else card.item_id

        ok = False
        # Empty shelf DOM / DB fallback: delete via titled Más opciones menu.
        if used_db_fallback:
            needles = _title_needles_for_row(
                store, account_id, card, catalog, page
            )
            print(
                f"  title-menu delete needles={needles[:3]!r} "
                f"autosell_id={autosell_id}",
                flush=True,
            )
            if needles:
                ok = remove_from_selling_by_title(
                    page, needles, action="delete"
                )
        else:
            ok = _remove_from_selling_shelf(
                page,
                card.item_id,
                action="delete",
                confirm_gone=True,
            )

        if not ok and listing_url and not used_db_fallback:
            try:
                Path("data/logs/facebook/purge").mkdir(parents=True, exist_ok=True)
                ok = bool(
                    remove_vehicle_listing(
                        page,
                        listing_url,
                        autosell_id=autosell_id,
                        removal_action="delete",
                        log_dir=Path("data/logs/facebook/purge"),
                        require_verified=True,
                        store=store,
                        account_id=account_id,
                    )
                )
            except Exception as exc:
                print(f"  FAIL item={card.item_id}: detail remove {exc}", flush=True)
                ok = False

        if not ok and not used_db_fallback:
            try:
                page.goto(SELLING_URL, wait_until="domcontentloaded", timeout=90_000)
                page.wait_for_timeout(2_000)
                link = _find_item_link_scrolled(page, card.item_id)
                if link is not None and _delete_shelf_link(page, link):
                    ok = _find_item_link_scrolled(page, card.item_id) is None
            except Exception as exc:
                print(f"  FAIL item={card.item_id}: {exc}", flush=True)
                ok = False

        if not ok:
            stats.failed += 1
            print(f"  FAIL item={card.item_id}: delete unconfirmed", flush=True)
            continue

        # remove_vehicle_listing may already have cleared the row; ensure cleared.
        row = _lookup_db_row(store, account_id, card.item_id)
        if row is not None:
            store.mark_fb_listing_removed(
                row["autosell_id"],
                account_id,
                clear_url=True,
            )
            print(
                f"  OK deleted item={card.item_id} "
                f"autosell_id={row['autosell_id']} (sync.db cleared)",
                flush=True,
            )
        else:
            print(
                f"  OK deleted item={card.item_id} "
                f"(sync.db already cleared / no live mapping)",
                flush=True,
            )
        stats.deleted += 1
        random_delay(delay_min_sec, delay_max_sec)

    return stats
