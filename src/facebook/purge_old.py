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
    _SELLING_SHELVES,
    _click_menu_action,
    _confirm_if_needed,
    _delete_shelf_link,
    _dismiss_delete_survey_if_needed,
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
    skipped_phantom: int = 0  # field_exception on unknown-age / archived ghosts
    deleted: int = 0
    failed: int = 0
    dry_run_candidates: int = 0


def purge_unknown_age_when_total(max_days: float) -> bool:
    """True when ``max_days <= 0`` (total purge): delete items with unknown age too."""
    return float(max_days) <= 0.0


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


def _should_keep_listing(
    card: ShelfListing,
    *,
    keep_item_ids: set[str] | None,
    keep_autosell_ids: set[str] | None,
    keep_title_re: re.Pattern[str] | None,
    store: SyncStore,
    account_id: str,
    catalog: dict[str, Any],
) -> str | None:
    """Return a reason string when this card must be preserved; else None."""
    if keep_item_ids and card.item_id in keep_item_ids:
        return f"keep-item-id={card.item_id}"
    row = _lookup_db_row(store, account_id, card.item_id)
    autosell_id = str(row["autosell_id"]) if row is not None else ""
    if keep_autosell_ids and autosell_id and autosell_id in keep_autosell_ids:
        return f"keep-autosell-id={autosell_id}"
    if keep_title_re is not None:
        haystacks = [card.card_text or ""]
        vehicle = catalog.get(autosell_id) if autosell_id else None
        if vehicle is not None:
            haystacks.append(getattr(vehicle, "marketplace_title", "") or "")
            haystacks.append(getattr(vehicle, "title", "") or "")
            haystacks.append(
                f"{getattr(vehicle, 'brand', '')} {getattr(vehicle, 'title', '')}"
            )
        for hay in haystacks:
            if hay and keep_title_re.search(hay):
                return f"keep-title-re matched {hay[:60]!r}"
    return None


# EN (account_1) + ES (account_2) — Needs attention / Duplicates primary CTAs.
_ATTENTION_DELETE_CTA = re.compile(
    r"delete and try again|"
    r"elim[ií]nala y vuelve a intentarlo|"
    r"elim[ií]nalo y vuelve a intentarlo|"
    r"eliminar y volver a intentar|"
    r"borra(?:r)? y vuelve a intentarlo",
    re.I,
)
_MENU_DELETE_LABEL = re.compile(
    r"eliminar publicación|delete listing|"
    r"^\s*eliminar\s*$|^\s*delete\s*$|"
    r"borrar publicación|delete this listing",
    re.I,
)
_DOM_SETTLE_MS = (1_200, 2_000)  # wait between card actions for DOM refresh


def _dom_settle(page: Page, *, lo_ms: int | None = None, hi_ms: int | None = None) -> None:
    """1–2s pause so Playwright sees post-delete / language-agnostic DOM refresh."""
    lo = lo_ms if lo_ms is not None else _DOM_SETTLE_MS[0]
    hi = hi_ms if hi_ms is not None else _DOM_SETTLE_MS[1]
    random_delay(lo / 1000.0, hi / 1000.0)
    try:
        page.wait_for_timeout(400)
    except Exception:
        pass


def _force_click(locator, *, timeout: int = 8_000) -> bool:
    """scroll_into_view_if_needed + click(force=True) for stubborn Needs-attention cards."""
    try:
        locator.scroll_into_view_if_needed(timeout=min(timeout, 4_000))
    except Exception:
        pass
    try:
        locator.click(timeout=timeout, force=True)
        return True
    except Exception:
        return False


def _click_by_bilingual_label(page: Page, pattern: re.Pattern[str], *, scope=None) -> bool:
    """Click first visible button/menuitem/link matching EN|ES ``pattern``."""
    root = scope if scope is not None else page
    getters = (
        lambda: root.get_by_role("menuitem", name=pattern),
        lambda: root.get_by_role("button", name=pattern),
        lambda: root.get_by_role("link", name=pattern),
        lambda: root.locator('[role="menuitem"]').filter(has_text=pattern),
        lambda: root.locator('[role="button"]').filter(has_text=pattern),
        lambda: root.get_by_text(pattern),
    )
    for getter in getters:
        try:
            loc = getter()
            count = loc.count()
        except Exception:
            continue
        if not isinstance(count, int) or count <= 0:
            continue
        for i in range(min(count, 8)):
            item = loc.nth(i)
            try:
                if not item.is_visible():
                    continue
                if _force_click(item, timeout=8_000):
                    return True
            except Exception:
                continue
    return False


def _click_attention_delete_cta(page: Page, *, scope=None) -> bool:
    """Needs attention / Duplicates: 'Delete and try again' / 'Elimínala…'."""
    if not _click_by_bilingual_label(page, _ATTENTION_DELETE_CTA, scope=scope):
        return False
    _dom_settle(page)
    _confirm_if_needed(page, prefer_delete=True)
    _dom_settle(page)
    return True


def _click_menu_delete_bilingual(page: Page) -> bool:
    """Menu: Eliminar publicación / Eliminar / Delete listing / Delete."""
    if _click_by_bilingual_label(page, _MENU_DELETE_LABEL):
        _dom_settle(page)
        _confirm_if_needed(page, prefer_delete=True)
        _dom_settle(page)
        return True
    # Shared remover patterns (also EN+ES, includes attention CTA).
    if _click_menu_action(page, action="delete"):
        _dom_settle(page)
        _confirm_if_needed(page, prefer_delete=True)
        _dom_settle(page)
        return True
    return False


def _collect_shelf_item_ids(page: Page, *, max_scrolls: int = 10) -> list[str]:
    """All marketplace item ids across selling shelves (incl. Needs attention)."""
    seen: set[str] = set()
    ordered: list[str] = []
    for shelf_url in _SELLING_SHELVES:
        try:
            page.goto(shelf_url, wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_timeout(2_000)
        except Exception:
            continue
        stagnant = 0
        for _ in range(max_scrolls):
            before = len(seen)
            links = page.locator('a[href*="/marketplace/item/"]')
            try:
                count = links.count()
            except Exception:
                count = 0
            if not isinstance(count, int):
                count = 0
            for i in range(min(count, 120)):
                try:
                    href = links.nth(i).get_attribute("href") or ""
                except Exception:
                    continue
                item_id = extract_item_id(href)
                if item_id and item_id not in seen:
                    seen.add(item_id)
                    ordered.append(item_id)
            if len(seen) == before:
                stagnant += 1
                if stagnant >= 2:
                    break
            else:
                stagnant = 0
            try:
                page.mouse.wheel(0, 2600)
                page.wait_for_timeout(700)
            except Exception:
                break
    return ordered


def _count_attention_ctas(page: Page) -> int:
    """Visible Needs-attention delete CTAs (EN + ES) on current page."""
    try:
        loc = page.get_by_role("button", name=_ATTENTION_DELETE_CTA)
        n = loc.count()
        return n if isinstance(n, int) else 0
    except Exception:
        return 0


def _delete_first_orphan_shelf_card(page: Page) -> bool:
    """Delete one shelf card when no ``/marketplace/item/`` anchors exist.

    Prefers Needs-attention CTA (EN/ES), then ⋮ → Eliminar/Delete → confirm.
    """
    main = page.locator("div[role='main']")
    # 1) Card-level attention CTA (Duplicates / Needs attention).
    if _click_attention_delete_cta(page, scope=main):
        return True
    if _click_attention_delete_cta(page):
        return True

    more_sel = (
        '[aria-label="More options"], [aria-label="Más opciones"], '
        '[aria-label="More"], [aria-label="Más"], '
        '[aria-label*="More options" i], [aria-label*="Más opciones" i], '
        '[aria-label*="Options" i], [aria-label*="Opciones" i]'
    )
    try:
        btns = main.locator(more_sel)
        count = btns.count()
    except Exception:
        return False
    if not isinstance(count, int) or count <= 0:
        return False
    for i in range(min(count, 12)):
        btn = btns.nth(i)
        try:
            if not btn.is_visible():
                continue
            label = (btn.get_attribute("aria-label") or "").lower()
            if any(x in label for x in ("boost", "promote", "share", "compartir")):
                continue
            if not _force_click(btn, timeout=5_000):
                continue
            _dom_settle(page, lo_ms=1_000, hi_ms=1_500)
            if _click_menu_delete_bilingual(page):
                return True
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            _dom_settle(page, lo_ms=800, hi_ms=1_200)
        except Exception:
            continue
    try:
        link = page.locator('a[href*="/marketplace/item/"]').first
        if link.count() > 0:
            ok = bool(_delete_shelf_link(page, link))
            if ok:
                _dom_settle(page)
            return ok
    except Exception:
        pass
    return False


def _dom_delete_item_with_retry(
    page: Page,
    item_id: str,
    *,
    account_id: str,
    attempts: int = 2,
) -> bool:
    """Delete one item via shelf helpers; bilingual CTA + 1–2s settle/retry."""
    for attempt in range(1, attempts + 1):
        # Attention CTA may sit on the card without opening ⋮.
        try:
            page.goto(SELLING_URL, wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_timeout(1_500)
        except Exception:
            pass
        if _click_attention_delete_cta(page):
            _dom_settle(page)
            return True

        ok = False
        try:
            ok = bool(_remove_from_selling_shelf(page, item_id, action="delete"))
        except Exception as exc:
            print(
                f"  [{account_id}] DOM delete {item_id} "
                f"attempt {attempt}/{attempts} error: {exc}",
                flush=True,
            )
        if ok:
            _dom_settle(page)
            return True

        for shelf_url in _SELLING_SHELVES:
            try:
                page.goto(shelf_url, wait_until="domcontentloaded", timeout=90_000)
                page.wait_for_timeout(1_500)
                # Card-scoped attention CTA near this item.
                link = _find_item_link_scrolled(page, item_id, max_scrolls=8)
                if link is not None:
                    try:
                        card = link.locator("xpath=ancestor::div[4]")
                        if _click_attention_delete_cta(page, scope=card):
                            _dom_settle(page)
                            return True
                    except Exception:
                        pass
                    if _delete_shelf_link(page, link):
                        _dom_settle(page)
                        return True
                elif _click_attention_delete_cta(page):
                    _dom_settle(page)
                    return True
            except Exception:
                continue

        _dom_settle(page, lo_ms=1_500, hi_ms=2_000)
    return False


def purge_shelf_until_empty(
    page: Page,
    *,
    account_id: str,
    dry_run: bool = False,
    max_rounds: int = 40,
    delay_min_sec: float = 1.5,
    delay_max_sec: float = 3.0,
) -> PurgeStats:
    """DOM delete every selling-shelf card (incl. Needs attention) until none remain.

    Bilingual (account_1 EN / account_2 ES):
      • menu: Eliminar publicación / Eliminar / Delete listing / Delete
      • attention: Delete and try again / Elimínala y vuelve a intentarlo
      • confirm: Delete / Eliminar / Confirm / Confirmar
    Settles 1–2s between card actions for DOM refresh.
    """
    stats = PurgeStats()
    print(
        f"  [{account_id}] Playwright DOM purge → {SELLING_URL} "
        f"(EN+ES: Delete/Eliminar + attention CTA)",
        flush=True,
    )
    try:
        page.goto(SELLING_URL, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(2_500)
        _sort_selling_oldest_first(page)
    except Exception:
        pass

    for round_idx in range(1, max_rounds + 1):
        item_ids = _collect_shelf_item_ids(page)
        # Also count attention CTAs when item anchors are missing/stale.
        try:
            page.goto(SELLING_URL, wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_timeout(1_500)
        except Exception:
            pass
        attention_n = _count_attention_ctas(page)

        if not item_ids and attention_n == 0:
            if dry_run:
                print(
                    f"  [{account_id}] DOM purge round {round_idx}: "
                    f"0 cards (dry-run stop)",
                    flush=True,
                )
                break
            if _delete_first_orphan_shelf_card(page):
                stats.deleted += 1
                stats.found = max(stats.found, stats.deleted)
                print(
                    f"  [{account_id}] DOM deleted orphan shelf card "
                    f"(round {round_idx})",
                    flush=True,
                )
                random_delay(delay_min_sec, delay_max_sec)
                continue
            print(
                f"  [{account_id}] DOM purge round {round_idx}: 0 cards — done",
                flush=True,
            )
            break

        if not item_ids and attention_n > 0:
            if round_idx == 1:
                stats.found = attention_n
            print(
                f"  [{account_id}] DOM purge round {round_idx}: "
                f"{attention_n} attention CTA(s) (no item anchors)",
                flush=True,
            )
            if dry_run:
                stats.dry_run_candidates = attention_n
                break
            progress = False
            for _ in range(attention_n + 2):
                if not _click_attention_delete_cta(page) and not _delete_first_orphan_shelf_card(
                    page
                ):
                    break
                stats.deleted += 1
                progress = True
                random_delay(delay_min_sec, delay_max_sec)
                _dom_settle(page)
            if not progress:
                break
            continue

        if round_idx == 1:
            stats.found = len(item_ids)
        print(
            f"  [{account_id}] DOM purge round {round_idx}: {len(item_ids)} card(s) "
            f"{item_ids[:8]}{'…' if len(item_ids) > 8 else ''}",
            flush=True,
        )
        if dry_run:
            stats.dry_run_candidates = len(item_ids)
            print(
                f"  [{account_id}] dry-run: would DOM-delete {len(item_ids)} card(s)",
                flush=True,
            )
            break

        progress = False
        for item_id in list(item_ids):
            ok = _dom_delete_item_with_retry(
                page, item_id, account_id=account_id, attempts=2
            )
            if ok:
                stats.deleted += 1
                progress = True
                print(f"  [{account_id}] DOM deleted {item_id}", flush=True)
            else:
                stats.failed += 1
                print(f"  [{account_id}] DOM delete failed {item_id}", flush=True)
            random_delay(delay_min_sec, delay_max_sec)
            _dom_settle(page)

        if not progress:
            remaining = _collect_shelf_item_ids(page)
            if remaining:
                print(
                    f"  [{account_id}] DOM purge stalled with {len(remaining)} left; stop",
                    flush=True,
                )
            break
    else:
        remaining = _collect_shelf_item_ids(page)
        if remaining:
            print(
                f"  [{account_id}] DOM purge hit max_rounds={max_rounds} "
                f"with {len(remaining)} left",
                flush=True,
            )

    left = _collect_shelf_item_ids(page) if not dry_run else []
    print(
        f"  [{account_id}] DOM purge done: deleted={stats.deleted} failed={stats.failed} "
        f"remaining={len(left)}",
        flush=True,
    )
    return stats


# Mass DOM: selling + notifications (phantoms / attention / duplicates).
_MASS_DOM_URLS = (
    SELLING_URL,
    f"{SELLING_URL}?status=NEED_ATTENTION",
    f"{SELLING_URL}?status=NEEDS_ATTENTION",
    f"{SELLING_URL}?status=PENDING",
    f"{SELLING_URL}?status=IN_REVIEW",
    f"{SELLING_URL}?status=SOLD",
    f"{SELLING_URL}?status=DRAFT",
    "https://www.facebook.com/marketplace/you/notifications",
)
_FAST_CLICK_MS = 2000  # cards need ~2s to settle; 200ms was too aggressive
_FAST_SETTLE_AFTER_MS = 800  # wait after delete/confirm before next card
_FAST_DELETE_RE = re.compile(
    r"delete and try again|"
    r"elim[ií]nala y vuelve a intentarlo|"
    r"elim[ií]nalo y vuelve a intentarlo|"
    r"eliminar publicación|delete listing|"
    r"^\s*eliminar\s*$|^\s*delete\s*$|"
    r"borrar publicación",
    re.I,
)
_FAST_CONFIRM_RE = re.compile(
    r"eliminar publicación|delete listing|"
    r"^\s*eliminar\s*$|^\s*delete\s*$|"
    r"confirmar|confirm|aceptar|ok",
    re.I,
)
_FAST_MORE_SEL = (
    '[aria-label="More options"], [aria-label="Más opciones"], '
    '[aria-label="More"], [aria-label="Más"], '
    '[aria-label*="More options" i], [aria-label*="Más opciones" i], '
    '[aria-label*="Options" i], [aria-label*="Opciones" i]'
)


def _fast_hover_scroll_click(locator, *, timeout_ms: int | None = None) -> bool:
    """Hover → scroll_into_view → awaited click(force=True)."""
    ms = int(timeout_ms) if timeout_ms is not None else int(_FAST_CLICK_MS)
    try:
        locator.hover(timeout=ms)
    except Exception:
        try:
            locator.hover(timeout=ms, force=True)
        except Exception:
            pass
    try:
        locator.scroll_into_view_if_needed(timeout=ms)
    except Exception:
        pass
    try:
        # Await the click fully before returning (no fire-and-forget).
        locator.click(timeout=ms, force=True, no_wait_after=False)
        return True
    except Exception:
        return False


def _fast_click_label(
    page: Page, pattern: re.Pattern[str], *, timeout_ms: int | None = None
) -> bool:
    """Wait for matching EN/ES control, then await click before returning."""
    ms = int(timeout_ms) if timeout_ms is not None else int(_FAST_CLICK_MS)
    for getter in (
        lambda: page.get_by_role("menuitem", name=pattern),
        lambda: page.get_by_role("button", name=pattern),
        lambda: page.locator('[role="menuitem"]').filter(has_text=pattern),
        lambda: page.locator('[role="button"]').filter(has_text=pattern),
        lambda: page.locator('[role="dialog"] button').filter(has_text=pattern),
        lambda: page.locator('[role="alertdialog"] button').filter(has_text=pattern),
    ):
        try:
            loc = getter()
            # Prefer waiting for first match to appear (menu/dialog settle).
            try:
                loc.first.wait_for(state="visible", timeout=ms)
            except Exception:
                pass
            n = loc.count()
        except Exception:
            continue
        if not isinstance(n, int) or n <= 0:
            continue
        for i in range(min(n, 4)):
            item = loc.nth(i)
            try:
                item.wait_for(state="visible", timeout=ms)
            except Exception:
                continue
            if _fast_hover_scroll_click(item, timeout_ms=ms):
                return True
    return False


def _wait_menu_or_dialog(page: Page, *, timeout_ms: int | None = None) -> bool:
    """Block until a menuitem or confirm dialog is visible after opening ⋮ / CTA."""
    ms = int(timeout_ms) if timeout_ms is not None else int(_FAST_CLICK_MS)
    try:
        page.locator(
            '[role="menuitem"], [role="dialog"], [role="alertdialog"]'
        ).first.wait_for(state="visible", timeout=ms)
        return True
    except Exception:
        return False


def _aggressive_scroll_load(page: Page, *, max_scrolls: int = 60) -> None:
    """Scroll window + main container so virtualized cards fully render."""
    try:
        page.wait_for_timeout(500)  # initial paint settle
    except Exception:
        pass

    def _heights() -> tuple[int, int]:
        try:
            return tuple(
                page.evaluate(
                    """() => {
                      const main = document.querySelector('[role="main"]');
                      return [
                        document.body ? document.body.scrollHeight : 0,
                        main ? main.scrollHeight : 0
                      ];
                    }"""
                )
            )
        except Exception:
            return (0, 0)

    stagnant = 0
    last = _heights()
    for _ in range(max_scrolls):
        try:
            # Prefer scrolling the Marketplace feed container when present.
            scrolled = page.evaluate(
                """() => {
                  const main = document.querySelector('[role="main"]');
                  const candidates = [];
                  if (main) {
                    candidates.push(main);
                    main.querySelectorAll('div').forEach((el) => {
                      const st = window.getComputedStyle(el);
                      if ((st.overflowY === 'auto' || st.overflowY === 'scroll')
                          && el.scrollHeight > el.clientHeight + 80) {
                        candidates.push(el);
                      }
                    });
                  }
                  let best = null;
                  let bestH = 0;
                  for (const el of candidates) {
                    if (el.scrollHeight > bestH) {
                      best = el;
                      bestH = el.scrollHeight;
                    }
                  }
                  if (best) {
                    best.scrollTop = Math.min(
                      best.scrollTop + 3500,
                      best.scrollHeight
                    );
                    return best.scrollTop;
                  }
                  window.scrollBy(0, 3500);
                  return window.scrollY;
                }"""
            )
            _ = scrolled
        except Exception:
            try:
                page.mouse.wheel(0, 3500)
            except Exception:
                break
        try:
            page.wait_for_timeout(350)  # allow lazy cards to mount
        except Exception:
            pass
        cur = _heights()
        if cur[0] <= last[0] and cur[1] <= last[1]:
            stagnant += 1
            if stagnant >= 5:
                break
        else:
            stagnant = 0
        last = cur

    # Return to top so clicks start from first rendered card.
    try:
        page.evaluate(
            """() => {
              const main = document.querySelector('[role="main"]');
              if (main) {
                main.querySelectorAll('div').forEach((el) => {
                  if (el.scrollTop) el.scrollTop = 0;
                });
                main.scrollTop = 0;
              }
              window.scrollTo(0, 0);
            }"""
        )
        page.wait_for_timeout(400)
    except Exception:
        pass


def _fast_delete_pass_on_page(page: Page, *, account_id: str) -> int:
    """One pass: await every attention CTA / ⋮→Delete / confirm. Returns deletes."""
    deleted = 0
    ms = int(_FAST_CLICK_MS)
    settle = int(_FAST_SETTLE_AFTER_MS)

    # 1) Direct attention / duplicate CTAs (no menu).
    for _ in range(80):
        if not _fast_click_label(page, _ATTENTION_DELETE_CTA, timeout_ms=ms):
            break
        _wait_menu_or_dialog(page, timeout_ms=ms)
        # Confirm must be awaited before next card.
        _fast_click_label(page, _FAST_CONFIRM_RE, timeout_ms=ms)
        # FB often shows a post-delete survey — dismiss instantly.
        _dismiss_delete_survey_if_needed(page, timeout_ms=ms)
        deleted += 1
        try:
            page.wait_for_timeout(settle)
        except Exception:
            pass

    # 2) ⋮ menus → Eliminar/Delete → confirm (await each step).
    main = page.locator("div[role='main']")
    try:
        btns = main.locator(_FAST_MORE_SEL)
        count = btns.count()
    except Exception:
        count = 0
    if not isinstance(count, int):
        count = 0
    for i in range(min(count, 80) - 1, -1, -1):
        try:
            btn = btns.nth(i)
            try:
                btn.wait_for(state="visible", timeout=ms)
            except Exception:
                continue
            if not btn.is_visible():
                continue
            label = (btn.get_attribute("aria-label") or "").lower()
            if any(x in label for x in ("boost", "promote", "share", "compartir")):
                continue
            # Hover parent card first (reveals hidden actions on attention cards).
            try:
                card = btn.locator("xpath=ancestor::div[3]")
                card.hover(timeout=ms)
                page.wait_for_timeout(200)
            except Exception:
                pass
            if not _fast_hover_scroll_click(btn, timeout_ms=ms):
                continue
            if not _wait_menu_or_dialog(page, timeout_ms=ms):
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
                continue
            if not _fast_click_label(page, _FAST_DELETE_RE, timeout_ms=ms):
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
                continue
            _wait_menu_or_dialog(page, timeout_ms=ms)
            _fast_click_label(page, _FAST_CONFIRM_RE, timeout_ms=ms)
            _dismiss_delete_survey_if_needed(page, timeout_ms=ms)
            deleted += 1
            try:
                page.wait_for_timeout(settle)
            except Exception:
                pass
        except Exception as exc:
            print(f"  [{account_id}] fast DOM skip: {exc}", flush=True)
            continue
    return deleted


def purge_mass_dom_fast(
    page: Page,
    *,
    account_id: str,
    dry_run: bool = False,
    max_rounds: int = 30,
    click_timeout_ms: int = _FAST_CLICK_MS,
) -> PurgeStats:
    """DOM sweeper for phantoms / attention / sold leftovers.

    Visits ``/marketplace/you/selling`` (+ attention tabs) and
    ``/marketplace/you/notifications``, scrolls the feed container to render
    all cards, then awaits bilingual Delete CTAs / ⋮ menus with
    ``click_timeout_ms`` (default 2000). Never aborts on a single card failure.
    """
    global _FAST_CLICK_MS
    prev_fast = _FAST_CLICK_MS
    _FAST_CLICK_MS = max(500, int(click_timeout_ms))
    stats = PurgeStats()
    print(
        f"  [{account_id}] MASS DOM sweeper "
        f"(selling + notifications, timeout={_FAST_CLICK_MS}ms, EN+ES, await clicks)",
        flush=True,
    )
    prev_default = None
    try:
        try:
            prev_default = page.context._timeout_settings.default_timeout  # type: ignore[attr-defined]
        except Exception:
            prev_default = None
        try:
            page.set_default_timeout(_FAST_CLICK_MS)
        except Exception:
            pass

        for round_idx in range(1, max_rounds + 1):
            round_deleted = 0
            for url in _MASS_DOM_URLS:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                    page.wait_for_timeout(1_500)  # let shelf cards settle
                except Exception as exc:
                    print(
                        f"  [{account_id}] MASS DOM nav skip {url}: {exc}",
                        flush=True,
                    )
                    continue
                _aggressive_scroll_load(page)
                if dry_run:
                    try:
                        n_more = page.locator("div[role='main']").locator(
                            _FAST_MORE_SEL
                        ).count()
                        n_cta = page.get_by_role(
                            "button", name=_ATTENTION_DELETE_CTA
                        ).count()
                    except Exception:
                        n_more, n_cta = 0, 0
                    print(
                        f"  [{account_id}] dry-run {url}: more≈{n_more} cta≈{n_cta}",
                        flush=True,
                    )
                    stats.dry_run_candidates += int(n_more or 0) + int(n_cta or 0)
                    continue
                got = _fast_delete_pass_on_page(page, account_id=account_id)
                round_deleted += got
                stats.deleted += got

            if dry_run:
                break
            print(
                f"  [{account_id}] MASS DOM round {round_idx}: "
                f"+{round_deleted} (total deleted={stats.deleted})",
                flush=True,
            )
            if round_deleted == 0:
                print(
                    f"  [{account_id}] MASS DOM: 0 deletes this round — stop",
                    flush=True,
                )
                break
            if round_idx == 1:
                stats.found = max(stats.found, stats.deleted)
    finally:
        _FAST_CLICK_MS = prev_fast
        try:
            page.set_default_timeout(prev_default or 30_000)
        except Exception:
            pass

    print(
        f"  [{account_id}] MASS DOM done: deleted={stats.deleted} "
        f"dry_run_candidates={stats.dry_run_candidates}",
        flush=True,
    )
    return stats


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
    keep_item_ids: set[str] | None = None,
    keep_autosell_ids: set[str] | None = None,
    keep_title_re: re.Pattern[str] | None = None,
    root: Path | None = None,
) -> PurgeStats:
    """Delete selling-shelf cards older than ``max_days``; update sync.db.

    Optional keep filters preserve specific item ids, autosell ids, or title
    regex matches (e.g. today's Ford Mustang GT).

    When ``FB_USE_GRAPHQL=1``, discovers active item ids via GraphQL/HTTP and
    deletes with ``data/graphql/delete_listing.json`` (no DOM scan).
    """
    from src.facebook.graphql.errors import FacebookGraphqlFallback
    from src.facebook.graphql.mode import graphql_enabled, graphql_ui_fallback
    from src.facebook.remover import remove_vehicle_listing

    if graphql_enabled():
        try:
            from src.facebook.graphql.purge import purge_old_listings_graphql
            from src.facebook.graphql.session_ctx import extract_session_context

            session = extract_session_context(
                page.context, page=page, account_id=account_id, fetch_tokens=True
            )
            print(
                f"  [{account_id}] GraphQL purge session {session.redacted_summary()}",
                flush=True,
            )
            return purge_old_listings_graphql(
                session,
                store,
                account_id=account_id,
                max_days=max_days,
                dry_run=dry_run,
                max_deletes=max_deletes,
                delay_min_sec=delay_min_sec,
                delay_max_sec=delay_max_sec,
                keep_item_ids=keep_item_ids,
                keep_autosell_ids=keep_autosell_ids,
                keep_title_re=keep_title_re,
                root=root,
            )
        except FacebookGraphqlFallback as exc:
            if not graphql_ui_fallback():
                raise
            print(
                f"  [{account_id}] GraphQL purge fallback → DOM ({exc})",
                flush=True,
            )
        except Exception as exc:
            if not graphql_ui_fallback():
                raise
            print(
                f"  [{account_id}] GraphQL purge error → DOM ({exc})",
                flush=True,
            )

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

    catalog = _catalog_by_autosell_id()
    targets: list[ShelfListing] = []
    skipped_keep = 0
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
            skipped_keep += 1
            print(
                f"  KEEP item={card.item_id} ({keep_reason})",
                flush=True,
            )
            continue
        age = card.age_days
        source = "ui" if "sync.db" not in (card.age_text or "") else "sync.db"
        if age is None:
            row = _lookup_db_row(store, account_id, card.item_id)
            age = _posted_at_age_days(row)
            if age is not None:
                source = "sync.db posted_at"
        if age is None:
            if purge_unknown_age_when_total(max_days):
                print(
                    f"  PURGE unknown age item={card.item_id} "
                    f"(max_days={max_days} total mode) "
                    f"text={card.age_text or card.card_text[:60]!r}",
                    flush=True,
                )
                targets.append(
                    ShelfListing(
                        item_id=card.item_id,
                        href=card.href,
                        age_days=None,
                        age_text=card.age_text or "unknown age (total purge)",
                        card_text=card.card_text,
                    )
                )
                continue
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
        f"(skipped young={stats.skipped_young}, unknown_age={stats.skipped_unknown_age}, "
        f"kept={skipped_keep})",
        flush=True,
    )

    if used_db_fallback and not dry_run:
        _sort_selling_oldest_first(page)
    for card in targets:
        if max_deletes is not None and stats.deleted >= max_deletes:
            print(f"  hit --max-deletes={max_deletes}; stopping", flush=True)
            break
        age_label = (
            f"{card.age_days:.1f}d"
            if card.age_days is not None
            else "unknown"
        )
        print(
            f"  PURGE item={card.item_id} age={age_label} ({card.age_text})",
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
