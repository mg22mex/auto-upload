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
    # Shelf abbreviations (Listed on Oct 24)
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
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
    # True when max_deletes stopped before all age-eligible targets were cleared.
    incomplete: bool = False
    ui_fallback_deletes: int = 0


def purge_unknown_age_when_total(max_days: float) -> bool:
    """True when ``max_days <= 0`` (total purge): delete items with unknown age too."""
    return float(max_days) <= 0.0


def _title_needles_for_purge(
    *,
    store: SyncStore,
    account_id: str,
    item_id: str,
    autosell_id: str,
    vehicle_title: str = "",
    card_text: str = "",
) -> list[str]:
    """Build title needles for Seller Hub delete when item_id href is missing."""
    needles: list[str] = []
    title = (vehicle_title or "").strip()
    if len(title) >= 5:
        needles.append(title)
    # Card text often starts with "YYYY Brand Model … Listed on …"
    blob = (card_text or "").strip()
    if blob:
        # Take first line / chunk before Listed on / Publicado.
        chunk = re.split(
            r"(?i)\b(?:listed on|publicado)\b",
            blob,
            maxsplit=1,
        )[0].strip()
        chunk = re.sub(r"\s+", " ", chunk)
        if len(chunk) >= 5:
            needles.append(chunk[:120])
        # Year + brand + model style: "2018 Audi Q5"
        m = re.search(
            r"\b(19|20)\d{2}\s+[A-Za-zÁÉÍÓÚÑáéíóúñ][A-Za-zÁÉÍÓÚÑáéíóúñ0-9\-\s]{2,60}",
            blob,
        )
        if m:
            needles.append(m.group(0).strip()[:80])

    catalog = _catalog_by_autosell_id()
    aid = (autosell_id or "").strip()
    if not aid or aid == item_id:
        row = _lookup_db_row(store, account_id, item_id)
        if row is not None:
            aid = str(row["autosell_id"] or "")
    vehicle = catalog.get(aid) if aid else None
    if vehicle is not None:
        try:
            needles.extend(_match_needles(vehicle))
        except Exception:
            pass
        vt = str(getattr(vehicle, "title", "") or "").strip()
        if len(vt) >= 5:
            needles.append(vt)

    # Dedupe longest-first (same as remover).
    return sorted(
        {n.strip() for n in needles if n and len(n.strip()) >= 5},
        key=len,
        reverse=True,
    )


def hard_delete_listing_via_playwright(
    page: Page,
    *,
    item_id: str,
    listing_url: str,
    autosell_id: str,
    store: SyncStore,
    account_id: str,
    log_dir: Path | None = None,
    vehicle_title: str = "",
    card_text: str = "",
    title_needles: list[str] | None = None,
) -> bool:
    """Playwright hard-delete for one shelf item (GraphQL failure / rate-limit fallback).

    When the card has no ``/marketplace/item/{id}`` href, matches by vehicle
    title (⋮ → Delete listing / Eliminar → confirm) and verifies the card is gone.
    """
    from src.facebook.remover import remove_vehicle_listing

    href = listing_url or ""
    if href and not href.startswith("http"):
        href = f"https://www.facebook.com{href}"
    if not href and item_id:
        href = f"https://www.facebook.com/marketplace/item/{item_id}/"

    needles = list(title_needles or [])
    if not needles:
        needles = _title_needles_for_purge(
            store=store,
            account_id=account_id,
            item_id=item_id,
            autosell_id=autosell_id,
            vehicle_title=vehicle_title,
            card_text=card_text,
        )

    ok = False
    if item_id:
        try:
            ok = _remove_from_selling_shelf(
                page,
                item_id,
                action="delete",
                confirm_gone=True,
            )
        except Exception as exc:
            print(
                f"  UI shelf delete miss item={item_id}: {exc}",
                flush=True,
            )

    # Title-based Seller Hub delete when item_id link is missing / unconfirmed.
    if not ok and needles:
        print(
            f"  UI title-match delete needles={needles[:3]!r} "
            f"item={item_id or '?'}",
            flush=True,
        )
        try:
            ok = remove_from_selling_by_title(
                page,
                needles,
                action="delete",
            )
        except Exception as exc:
            print(f"  UI title-match delete fail item={item_id}: {exc}", flush=True)
            ok = False

    if not ok and href:
        try:
            purge_log = log_dir or Path("data/logs/facebook/purge")
            purge_log.mkdir(parents=True, exist_ok=True)
            ok = bool(
                remove_vehicle_listing(
                    page,
                    href,
                    autosell_id=autosell_id,
                    removal_action="delete",
                    log_dir=purge_log,
                    require_verified=True,
                    store=store,
                    account_id=account_id,
                    skip_graphql=True,  # already tried GraphQL; DOM only
                    title_needles=needles or None,
                )
            )
        except Exception as exc:
            print(f"  UI detail delete fail item={item_id}: {exc}", flush=True)
            ok = False

    if not ok and item_id:
        try:
            page.goto(SELLING_URL, wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_timeout(2_000)
            link = _find_item_link_scrolled(page, item_id)
            if link is not None and _delete_shelf_link(page, link):
                ok = _find_item_link_scrolled(page, item_id) is None
        except Exception as exc:
            print(f"  UI force shelf fail item={item_id}: {exc}", flush=True)
            ok = False

    return bool(ok)


def _age_from_calendar_date(
    year: int,
    month: int,
    day: int,
    *,
    when: datetime,
    year_explicit: bool = False,
) -> float | None:
    try:
        posted = datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return None
    if posted > when and not year_explicit:
        posted = posted.replace(year=year - 1)
    return max(0.0, (when - posted).total_seconds() / 86400.0)


def parse_listing_age_days(text: str, *, now: datetime | None = None) -> float | None:
    """Parse Marketplace age strings (EN/ES) into approximate age in days.

    Handles relative phrases and absolute shelf labels such as
    ``Listed on 10/24``, ``Listed on Oct 24``, ``Publicado el 24/10``.

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

    # Absolute numeric: "Listed on 10/24" / "Listed on 10/24/2025" (US MM/DD).
    m = re.search(
        r"listed\s+on\s+(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?",
        blob,
    )
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year_raw = m.group(3)
        year_explicit = bool(year_raw)
        if year_raw:
            year = int(year_raw)
            if year < 100:
                year += 2000
        else:
            year = when.year
        age = _age_from_calendar_date(
            year, month, day, when=when, year_explicit=year_explicit
        )
        if age is not None:
            return age

    # Absolute numeric ES: "Publicado el 24/10" / "Publicado el 24/10/2025" (DD/MM).
    m = re.search(
        r"publicado\s+(?:el|en)\s+(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?",
        blob,
    )
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year_raw = m.group(3)
        year_explicit = bool(year_raw)
        if year_raw:
            year = int(year_raw)
            if year < 100:
                year += 2000
        else:
            year = when.year
        age = _age_from_calendar_date(
            year, month, day, when=when, year_explicit=year_explicit
        )
        if age is not None:
            return age

    # Absolute: "Publicado el 20 de agosto" / "Listed on August 20"
    m = re.search(
        r"(?:publicado\s+el|listed\s+on)\s+(\d{1,2})\s+de\s+([a-záéíóúñ]+)",
        blob,
    )
    if m:
        day = int(m.group(1))
        month = _MONTHS_ES.get(m.group(2))
        if month:
            age = _age_from_calendar_date(when.year, month, day, when=when)
            if age is not None:
                return age

    m = re.search(
        r"listed\s+on\s+([a-z]+)\s+(\d{1,2})(?:,\s*(\d{4}))?",
        blob,
    )
    if m:
        month = _MONTHS_EN.get(m.group(1))
        day = int(m.group(2))
        year_raw = m.group(3)
        year = int(year_raw) if year_raw else when.year
        if month:
            age = _age_from_calendar_date(
                year, month, day, when=when, year_explicit=bool(year_raw)
            )
            if age is not None:
                return age

    return None


def resolve_listing_age_for_purge(
    *,
    live_age_days: float | None,
    live_age_text: str = "",
    live_card_text: str = "",
    db_age_days: float | None = None,
    now: datetime | None = None,
) -> tuple[float | None, str]:
    """Resolve purge age: live FB shelf/GraphQL wins over ``sync.db``.

    Re-parses ``Listed on …`` / relative age text when ``live_age_days`` is
    missing. Database ``posted_at`` is used only when the live platform gives
    no age signal.
    """
    when = now or datetime.now(timezone.utc)
    age = live_age_days
    source = "live"
    if age is None:
        blob = f"{live_age_text} {live_card_text}".strip()
        age = parse_listing_age_days(blob, now=when)
        if age is not None:
            source = "live_text"

    if age is not None:
        if db_age_days is not None and abs(float(age) - float(db_age_days)) >= 0.5:
            print(
                f"  age conflict: live={age:.1f}d vs sync.db={db_age_days:.1f}d "
                f"— using live platform state",
                flush=True,
            )
            return float(age), f"{source} (db={db_age_days:.1f}d ignored)"
        return float(age), source

    if db_age_days is not None:
        return float(db_age_days), "sync.db posted_at"
    return None, "unknown"


def is_purge_age_eligible(age_days: float, max_days: float) -> bool:
    """True when listing is ``max_days`` or older (strict 7-day rule).

    ``max_days <= 0`` means total purge (every aged/unknown target).
    """
    if float(max_days) <= 0:
        return True
    return float(age_days) >= float(max_days)


def extract_listed_on_snip(text: str) -> str:
    """Best-effort snippet of FB shelf date/age label for logs."""
    blob = text or ""
    m = re.search(
        r"(listed\s+on\s+\d{1,2}/\d{1,2}(?:/\d{2,4})?"
        r"|listed\s+on\s+[A-Za-z]+\s+\d{1,2}(?:,\s*\d{4})?"
        r"|publicado\s+(?:el|en)\s+\d{1,2}/\d{1,2}(?:/\d{2,4})?"
        r"|publicado[^\n]{0,48}"
        r"|listed[^\n]{0,48}"
        r"|hace\s+\d+[^\n]{0,24}"
        r"|\d+\s+days?\s+ago"
        r"|\d+\s+d[ií]as?)",
        blob,
        re.I,
    )
    return m.group(0).strip() if m else ""


# Prefer text content over fragile structural roles (Seller Hub DOM churns).
_LISTED_ON_TEXT_NEEDLES = (
    "Listed on",
    "Publicado el",
    "Publicado en",
    "Publicado",
)

_ITEM_HREF_SELECTORS = (
    'a[href*="/marketplace/item/"]',
    '[role="link"][href*="/marketplace/item/"]',
    'a[href*="marketplace/item/"]',
)


def _shelf_wait_timeout_ms() -> int:
    from src.facebook.util import env_int

    return max(5_000, env_int("FB_SHELF_WAIT_MS", 15_000))


def _hydrate_scroll_count() -> int:
    from src.facebook.util import env_int

    return max(1, min(8, env_int("FB_SHELF_HYDRATE_SCROLLS", 3)))


def wait_for_selling_shelf_cards(
    page: Page,
    *,
    timeout_ms: int | None = None,
) -> bool:
    """Wait until Seller Hub shows ``Listed on`` / ``Publicado`` date labels."""
    ms = int(timeout_ms if timeout_ms is not None else _shelf_wait_timeout_ms())
    deadline_errors: list[str] = []

    # 1) Text-first wait (matches current Seller Hub; roles often missing).
    for needle in _LISTED_ON_TEXT_NEEDLES:
        try:
            page.get_by_text(needle, exact=False).first.wait_for(
                state="visible", timeout=ms
            )
            print(
                f"  shelf wait OK text={needle!r} timeout_ms={ms}",
                flush=True,
            )
            return True
        except Exception as exc:
            deadline_errors.append(f"text:{needle}: {exc}")
            ms = max(3_000, ms // 2)
            continue

    # 2) Soft fallback: marketplace item anchors (older layouts).
    for sel in (
        'a[href*="/marketplace/item/"]',
        'xpath=//*[contains(text(),"Listed on") or contains(text(),"Publicado")]',
    ):
        try:
            page.wait_for_selector(sel, state="visible", timeout=ms)
            print(
                f"  shelf wait OK selector={sel!r} timeout_ms={ms}",
                flush=True,
            )
            return True
        except Exception as exc:
            deadline_errors.append(f"{sel}: {exc}")
            ms = max(3_000, ms // 2)
            continue

    print(
        "  WARNING: selling shelf Listed-on labels did not appear within wait budget "
        f"({'; '.join(deadline_errors[:2])})",
        flush=True,
    )
    return False


def hydrate_selling_shelf_scrolls(
    page: Page,
    *,
    scrolls: int | None = None,
) -> None:
    """Smooth scroll loop so lazy-loaded late-October cards enter the DOM."""
    n = int(scrolls if scrolls is not None else _hydrate_scroll_count())
    print(f"  shelf hydrate scrolls={n}", flush=True)
    try:
        page.wait_for_timeout(600)
    except Exception:
        pass
    for i in range(n):
        try:
            page.evaluate(
                """(step) => {
                  const main = document.querySelector('[role="main"]');
                  const delta = Math.max(900, Math.floor(window.innerHeight * 0.85));
                  const targets = [];
                  if (main) {
                    targets.push(main);
                    main.querySelectorAll('div').forEach((el) => {
                      const st = window.getComputedStyle(el);
                      if ((st.overflowY === 'auto' || st.overflowY === 'scroll')
                          && el.scrollHeight > el.clientHeight + 80) {
                        targets.push(el);
                      }
                    });
                  }
                  let scrolled = false;
                  for (const el of targets) {
                    const before = el.scrollTop;
                    el.scrollBy({ top: delta, left: 0, behavior: 'smooth' });
                    if (el.scrollTop !== before) scrolled = true;
                  }
                  window.scrollBy({ top: delta, left: 0, behavior: 'smooth' });
                  return { step, scrolled };
                }""",
                i + 1,
            )
        except Exception:
            try:
                page.mouse.wheel(0, 2200)
            except Exception:
                break
        try:
            page.wait_for_timeout(900)
        except Exception:
            pass
    # Nudge back toward top so first-screen cards stay queryable.
    try:
        page.evaluate(
            """() => {
              const main = document.querySelector('[role="main"]');
              if (main) main.scrollTo({ top: 0, behavior: 'instant' });
              window.scrollTo({ top: 0, behavior: 'instant' });
            }"""
        )
        page.wait_for_timeout(500)
    except Exception:
        pass


def _card_blob_for_link(link) -> str:
    parts: list[str] = []
    try:
        href = link.get_attribute("href") or ""
        if href:
            parts.append(href)
    except Exception:
        pass
    try:
        aria = link.get_attribute("aria-label") or ""
        if aria:
            parts.append(aria)
    except Exception:
        pass
    try:
        parts.append(link.inner_text(timeout=1_500) or "")
    except Exception:
        pass
    for xpath in (
        "xpath=ancestor::div[@role='article'][1]",
        "xpath=ancestor::div[@role='listitem'][1]",
        "xpath=ancestor::div[.//span[contains(.,'Listed on') or contains(.,'Publicado')]][1]",
        "xpath=ancestor::div[8]",
        "xpath=ancestor::div[6]",
        "xpath=ancestor::div[4]",
    ):
        try:
            node = link.locator(xpath).first
            if node.count() > 0:
                parts.append(node.inner_text(timeout=1_500) or "")
                break
        except Exception:
            continue
    return " ".join(p for p in parts if p)


def _extract_selling_cards_via_js(page: Page) -> list[dict[str, str]]:
    """Find Seller Hub cards by ``Listed on`` / ``Publicado`` text, then climb.

    Facebook's current Seller Hub often lacks ``role=listitem`` / item anchors
    at the top level. Start from date-label text nodes, walk up to a card-sized
    parent, then pull listing href/id + the exact date snippet.
    """
    try:
        raw = page.evaluate(
            r"""() => {
              const DATE_RE = /Listed on|Publicado(?:\s+el|\s+en)?/i;
              const DATE_SNIP_RE =
                /Listed on\s+[^\n,]{0,40}|Publicado(?:\s+el|\s+en)?\s+[^\n,]{0,40}/i;
              const ID_RE = /\/marketplace\/item\/(\d{8,})/;
              const ANY_ID_RE = /(?:item_id|listing_id|for_sale_item_id|story_id)[=:\"'\s]+(\d{8,})/i;
              const out = [];
              const seen = new Set();
              const root = document.querySelector('[role="main"]') || document.body;

              const isVisible = (el) => {
                if (!el || !el.getBoundingClientRect) return false;
                const r = el.getBoundingClientRect();
                return r.width > 2 && r.height > 2;
              };

              // Collect text-bearing elements that mention Listed on / Publicado.
              const candidates = [];
              const walk = document.createTreeWalker(
                root,
                NodeFilter.SHOW_ELEMENT,
                null
              );
              let node = walk.currentNode;
              while (node) {
                if (node.nodeType === 1) {
                  const el = node;
                  // Prefer leaf-ish labels (span/div with short own text).
                  const own = (el.childNodes && el.childNodes.length)
                    ? Array.from(el.childNodes)
                        .filter((c) => c.nodeType === 3)
                        .map((c) => (c.textContent || '').trim())
                        .join(' ')
                    : '';
                  const direct = own || ((el.children && el.children.length === 0)
                    ? (el.textContent || '').trim()
                    : '');
                  if (direct && DATE_RE.test(direct) && direct.length < 120) {
                    candidates.push(el);
                  }
                }
                node = walk.nextNode();
              }
              // Fallback: broader innerText match when leaf scan is empty.
              if (!candidates.length) {
                root.querySelectorAll('span, div, a, p').forEach((el) => {
                  const t = (el.innerText || el.textContent || '').trim();
                  if (t && DATE_RE.test(t) && t.length < 160 && isVisible(el)) {
                    candidates.push(el);
                  }
                });
              }

              const climbCard = (el) => {
                let n = el;
                let best = el;
                for (let i = 0; i < 14 && n; i++) {
                  const txt = (n.innerText || '').replace(/\s+/g, ' ').trim();
                  const hasLink = !!(
                    n.querySelector
                    && (n.querySelector('a[href*="/marketplace/item/"]')
                        || n.querySelector('a[href*="marketplace"]')
                        || n.querySelector('[href*="/marketplace/item/"]'))
                  );
                  const len = txt.length;
                  // Card-sized wrapper: has date + (link or title-ish length).
                  if (DATE_RE.test(txt) && len >= 20 && len <= 1800) {
                    best = n;
                    if (hasLink || (n.querySelector && n.querySelector('img'))) {
                      // Prefer containers that look like a listing tile.
                      if (len >= 40) return n;
                    }
                  }
                  n = n.parentElement;
                }
                return best;
              };

              const extractHrefId = (card) => {
                const anchors = card.querySelectorAll
                  ? card.querySelectorAll('a[href], [role="link"][href], [href]')
                  : [];
                for (const a of anchors) {
                  const href = a.href || a.getAttribute('href') || '';
                  let m = href.match(ID_RE);
                  if (m) return { item_id: m[1], href };
                }
                // Some Seller Hub tiles encode id only in nested data / aria.
                const blob = (card.innerHTML || '') + ' ' + (card.getAttribute
                  ? (card.getAttribute('aria-label') || '')
                  : '');
                let m = blob.match(ID_RE) || blob.match(ANY_ID_RE);
                if (m) {
                  const id = m[1];
                  return {
                    item_id: id,
                    href: 'https://www.facebook.com/marketplace/item/' + id + '/',
                  };
                }
                return { item_id: '', href: '' };
              };

              for (const labelEl of candidates) {
                if (!isVisible(labelEl)) continue;
                const card = climbCard(labelEl);
                if (!card) continue;
                const labelText = (labelEl.innerText || labelEl.textContent || '')
                  .replace(/\s+/g, ' ').trim();
                const cardText = (card.innerText || '').replace(/\s+/g, ' ').trim();
                const dateMatch = (labelText.match(DATE_SNIP_RE)
                  || cardText.match(DATE_SNIP_RE));
                const date_text = dateMatch ? dateMatch[0].trim() : labelText;
                const { item_id, href } = extractHrefId(card);
                const key = item_id || (date_text + '|' + cardText.slice(0, 80));
                if (seen.has(key)) continue;
                seen.add(key);
                if (!item_id) continue;  // need an id for hard-delete queue
                out.push({
                  item_id,
                  href: href || ('https://www.facebook.com/marketplace/item/' + item_id + '/'),
                  date_text,
                  blob: (date_text + ' ' + cardText).slice(0, 900),
                });
              }
              return out;
            }"""
        )
    except Exception as exc:
        print(f"  WARNING: JS Listed-on card extract failed: {exc}", flush=True)
        return []
    if not isinstance(raw, list):
        return []
    cleaned: list[dict[str, str]] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        item_id = str(row.get("item_id") or "").strip()
        href = str(row.get("href") or "").strip()
        blob = str(row.get("blob") or "").strip()
        date_text = str(row.get("date_text") or "").strip()
        if date_text and date_text not in blob:
            blob = f"{date_text} {blob}".strip()
        if item_id.isdigit() and len(item_id) >= 8:
            cleaned.append(
                {
                    "item_id": item_id,
                    "href": href,
                    "blob": blob,
                    "date_text": date_text,
                }
            )
    return cleaned


def _extract_selling_cards_via_text_locators(page: Page) -> list[dict[str, str]]:
    """Playwright locator fallback: get_by_text → parent card → link + date."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for needle in _LISTED_ON_TEXT_NEEDLES:
        try:
            labels = page.get_by_text(needle, exact=False)
            count = labels.count()
        except Exception:
            continue
        if not isinstance(count, int):
            continue
        for i in range(min(count, 80)):
            label = labels.nth(i)
            try:
                date_text = (label.inner_text(timeout=1_000) or "").strip()
            except Exception:
                date_text = needle
            # Climb to a card-sized ancestor that also holds a marketplace link.
            card = None
            for xpath in (
                "xpath=ancestor::div[.//a[contains(@href,'/marketplace/item/')]][1]",
                "xpath=ancestor::div[.//a[contains(@href,'marketplace')]][1]",
                "xpath=ancestor::div[contains(.,'Listed on') or contains(.,'Publicado')][4]",
                "xpath=ancestor::div[6]",
                "xpath=ancestor::div[8]",
            ):
                try:
                    cand = label.locator(xpath).first
                    if cand.count() > 0:
                        card = cand
                        break
                except Exception:
                    continue
            if card is None:
                continue
            href = ""
            item_id = ""
            try:
                link = card.locator('a[href*="/marketplace/item/"]').first
                if link.count() > 0:
                    href = link.get_attribute("href") or ""
                    item_id = extract_item_id(href) or ""
            except Exception:
                pass
            if not item_id:
                try:
                    html = card.inner_html(timeout=1_000) or ""
                    m = re.search(r"/marketplace/item/(\d{8,})", html)
                    if m:
                        item_id = m.group(1)
                        href = f"https://www.facebook.com/marketplace/item/{item_id}/"
                except Exception:
                    pass
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            try:
                card_text = (card.inner_text(timeout=1_500) or "").strip()
            except Exception:
                card_text = date_text
            blob = f"{date_text} {card_text}".strip()
            out.append(
                {
                    "item_id": item_id,
                    "href": href
                    or f"https://www.facebook.com/marketplace/item/{item_id}/",
                    "blob": blob[:900],
                    "date_text": date_text,
                }
            )
    return out


def collect_selling_listings(
    page: Page,
    *,
    max_scrolls: int = 20,
) -> list[ShelfListing]:
    """Load `/you/selling`, wait for Listed-on labels, hydrate-scroll, collect.

    Discovery is **text-first**: find ``Listed on`` / ``Publicado el`` nodes,
    climb to the parent card wrapper, then extract the date snippet + item link
    for the hard-delete queue (does not rely on ``role=listitem``).
    """
    found: dict[str, ShelfListing] = {}
    shelves = list(_ACTIVE_SELLING_SHELVES[:3]) or [SELLING_URL]

    def _ingest(row: dict[str, str]) -> None:
        item_id = str(row.get("item_id") or "").strip()
        if not item_id or item_id in found:
            return
        href = str(row.get("href") or "").strip()
        blob = str(row.get("blob") or "").strip()
        date_text = str(row.get("date_text") or "").strip()
        if date_text and date_text not in blob:
            blob = f"{date_text} {blob}".strip()
        age = parse_listing_age_days(blob) or parse_listing_age_days(date_text)
        age_snip = extract_listed_on_snip(blob) or date_text
        found[item_id] = ShelfListing(
            item_id=item_id,
            href=href
            or f"https://www.facebook.com/marketplace/item/{item_id}/",
            age_days=age,
            age_text=age_snip,
            card_text=blob[:500],
        )

    for shelf in shelves:
        try:
            page.goto(shelf, wait_until="domcontentloaded", timeout=90_000)
        except Exception as exc:
            print(f"  WARNING: could not open {shelf}: {exc}", flush=True)
            continue

        wait_for_selling_shelf_cards(page)
        hydrate_selling_shelf_scrolls(page)

        # Primary: Listed-on / Publicado text → parent card → date + link.
        for row in _extract_selling_cards_via_js(page):
            _ingest(row)
        for row in _extract_selling_cards_via_text_locators(page):
            _ingest(row)

        stagnant = 0
        for _ in range(max_scrolls):
            before = len(found)
            for row in _extract_selling_cards_via_js(page):
                _ingest(row)
            for row in _extract_selling_cards_via_text_locators(page):
                _ingest(row)

            # Soft legacy pass: marketplace item anchors still help when present.
            for sel in _ITEM_HREF_SELECTORS:
                links = page.locator(sel)
                try:
                    count = links.count()
                except Exception:
                    count = 0
                if not isinstance(count, int):
                    count = 0
                for i in range(min(count, 160)):
                    loc = links.nth(i)
                    try:
                        href = loc.get_attribute("href") or ""
                    except Exception:
                        continue
                    item_id = extract_item_id(href)
                    if not item_id or item_id in found:
                        continue
                    blob = _card_blob_for_link(loc)
                    _ingest(
                        {
                            "item_id": item_id,
                            "href": href,
                            "blob": blob,
                            "date_text": extract_listed_on_snip(blob),
                        }
                    )

            if len(found) == before:
                stagnant += 1
                if stagnant >= 3:
                    break
            else:
                stagnant = 0
            try:
                page.evaluate(
                    """() => {
                      const main = document.querySelector('[role="main"]');
                      const delta = Math.max(1200, Math.floor(window.innerHeight * 0.9));
                      if (main) main.scrollBy(0, delta);
                      window.scrollBy(0, delta);
                    }"""
                )
                page.wait_for_timeout(850)
            except Exception:
                try:
                    page.mouse.wheel(0, 2600)
                    page.wait_for_timeout(800)
                except Exception:
                    break

        with_dates = sum(1 for c in found.values() if c.age_text)
        print(
            f"  shelf scan {shelf}: {len(found)} unique item(s) so far "
            f"({with_dates} with Listed-on/Publicado date text)",
            flush=True,
        )

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
    f"{SELLING_URL}?status=HIDDEN",
    f"{SELLING_URL}?status=INACTIVE",
    f"{SELLING_URL}?status=SOLD",
    f"{SELLING_URL}?status=DRAFT",
    "https://www.facebook.com/marketplace/you/notifications",
)
# Card banner text for rejected duplicate creates (account_1 EN / account_2 ES).
_DUPLICATE_BANNER_RE = re.compile(
    r"this looks like a duplicate|"
    r"we didn'?t post it|"
    r"didn'?t post it|"
    r"delete it and try again|"
    r"parece un duplicado|"
    r"no lo publicamos|"
    r"no la publicamos|"
    r"elim[ií]nala y vuelve a intentarlo|"
    r"elim[ií]nalo y vuelve a intentarlo",
    re.I,
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


def _purge_duplicate_banner_cards(page: Page, *, account_id: str, timeout_ms: int) -> int:
    """Delete cards showing 'This looks like a duplicate… / Delete it and try again'."""
    deleted = 0
    try:
        banners = page.get_by_text(_DUPLICATE_BANNER_RE)
        n = banners.count()
    except Exception:
        n = 0
    if not isinstance(n, int) or n <= 0:
        return 0
    print(
        f"  [{account_id}] duplicate banner card(s) visible≈{n} — purging",
        flush=True,
    )
    for _ in range(min(n, 40)):
        # Prefer the explicit attention CTA near the banner.
        if _fast_click_label(page, _ATTENTION_DELETE_CTA, timeout_ms=timeout_ms):
            _wait_menu_or_dialog(page, timeout_ms=timeout_ms)
            _fast_click_label(page, _FAST_CONFIRM_RE, timeout_ms=timeout_ms)
            _dismiss_delete_survey_if_needed(page, timeout_ms=timeout_ms)
            deleted += 1
            try:
                page.wait_for_timeout(int(_FAST_SETTLE_AFTER_MS))
            except Exception:
                pass
            continue
        # Fallback: hover banner card → ⋮ → Eliminar/Delete.
        try:
            banners = page.get_by_text(_DUPLICATE_BANNER_RE)
            if banners.count() <= 0:
                break
            node = banners.first
            try:
                node.scroll_into_view_if_needed(timeout=timeout_ms)
                node.hover(timeout=timeout_ms)
            except Exception:
                pass
            card = node.locator("xpath=ancestor::div[5]")
            more = card.locator(_FAST_MORE_SEL).first
            if more.count() > 0 and _fast_hover_scroll_click(more, timeout_ms=timeout_ms):
                if _fast_click_label(page, _FAST_DELETE_RE, timeout_ms=timeout_ms):
                    _wait_menu_or_dialog(page, timeout_ms=timeout_ms)
                    _fast_click_label(page, _FAST_CONFIRM_RE, timeout_ms=timeout_ms)
                    _dismiss_delete_survey_if_needed(page, timeout_ms=timeout_ms)
                    deleted += 1
                    try:
                        page.wait_for_timeout(int(_FAST_SETTLE_AFTER_MS))
                    except Exception:
                        pass
                    continue
        except Exception as exc:
            print(f"  [{account_id}] duplicate-banner purge skip: {exc}", flush=True)
        break
    return deleted


def _fast_delete_pass_on_page(page: Page, *, account_id: str) -> int:
    """One pass: await every attention CTA / ⋮→Delete / confirm. Returns deletes."""
    deleted = 0
    ms = int(_FAST_CLICK_MS)
    settle = int(_FAST_SETTLE_AFTER_MS)

    # 0) Explicit duplicate-warning cards first (account_1 EN banner).
    deleted += _purge_duplicate_banner_cards(
        page, account_id=account_id, timeout_ms=ms
    )

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
    max_days: float = 7.0,
    dry_run: bool = False,
    max_deletes: int | None = None,
    delay_min_sec: float = 2.0,
    delay_max_sec: float = 4.0,
    keep_item_ids: set[str] | None = None,
    keep_autosell_ids: set[str] | None = None,
    keep_title_re: re.Pattern[str] | None = None,
    root: Path | None = None,
    force_dom_discovery: bool = False,
) -> PurgeStats:
    """Delete selling-shelf cards older than ``max_days``; update sync.db.

    Optional keep filters preserve specific item ids, autosell ids, or title
    regex matches (e.g. today's Ford Mustang GT).

    When ``FB_USE_GRAPHQL=1``, discovers via GraphQL/HTTP then deletes with
    ``data/graphql/delete_listing.json``. If discovery returns 0 items / 400,
    or ``force_dom_discovery=True`` (preflight), loads
    ``/marketplace/you/selling`` in Playwright and parses ``Listed on…`` text.
    """
    from src.facebook.graphql.errors import FacebookGraphqlFallback, FacebookGraphqlError
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
                f"  [{account_id}] GraphQL purge session {session.redacted_summary()}"
                + (" (DOM discovery forced)" if force_dom_discovery else ""),
                flush=True,
            )
            stats_gql = purge_old_listings_graphql(
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
                page=page,
                force_dom_discovery=force_dom_discovery,
            )
            # DOM-forced / non-empty discovery: done (deletes already ran).
            if force_dom_discovery or stats_gql.found > 0:
                return stats_gql
            print(
                f"  [{account_id}] GraphQL shelf still empty after DOM merge — "
                f"running full Playwright purge path",
                flush=True,
            )
        except FacebookGraphqlFallback as exc:
            if not graphql_ui_fallback():
                raise
            print(
                f"  [{account_id}] GraphQL purge fallback → DOM ({exc})",
                flush=True,
            )
        except FacebookGraphqlError as exc:
            status = getattr(exc, "status_code", None)
            if not graphql_ui_fallback():
                raise
            print(
                f"  [{account_id}] GraphQL purge HTTP "
                f"{status or 'error'} → Playwright DOM ({exc})",
                flush=True,
            )
        except Exception as exc:
            if not graphql_ui_fallback():
                raise
            print(
                f"  [{account_id}] GraphQL purge error → DOM ({exc})",
                flush=True,
            )
    elif force_dom_discovery:
        print(
            f"  [{account_id}] Playwright DOM shelf discovery "
            f"(force_dom_discovery=1) — Listed-on text from /you/selling",
            flush=True,
        )

    stats = PurgeStats()
    listings = collect_selling_listings(page)
    stats.found = len(listings)
    print(
        f"  [{account_id}] Playwright shelf scan: {stats.found} listing(s) "
        f"with item links + Listed-on ages",
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
        row = _lookup_db_row(store, account_id, card.item_id)
        db_age = _posted_at_age_days(row)
        age, source = resolve_listing_age_for_purge(
            live_age_days=card.age_days,
            live_age_text=card.age_text or "",
            live_card_text=card.card_text or "",
            db_age_days=db_age,
        )
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
        if not is_purge_age_eligible(age, max_days):
            stats.skipped_young += 1
            print(
                f"  SKIP young item={card.item_id} age={age:.1f}d "
                f"(<{max_days}d, via {source})",
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
    remaining_after_cap = 0
    for idx, card in enumerate(targets):
        if max_deletes is not None and stats.deleted >= max_deletes:
            remaining_after_cap = len(targets) - idx
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
        needles = _title_needles_for_purge(
            store=store,
            account_id=account_id,
            item_id=card.item_id,
            autosell_id=autosell_id,
            vehicle_title="",
            card_text=card.card_text or card.age_text or "",
        )
        # Catalog needles only (no detail-page navigation mid-shelf purge).
        vehicle = catalog.get(autosell_id) if autosell_id else None
        if vehicle is not None:
            try:
                needles = sorted(
                    set(needles) | set(_match_needles(vehicle)),
                    key=len,
                    reverse=True,
                )
            except Exception:
                pass

        # Empty shelf DOM / DB fallback: delete via titled Más opciones menu.
        if used_db_fallback:
            if not needles:
                # Last resort for DB-only rows: may open detail once for title.
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
            if not ok and needles:
                print(
                    f"  item_id href miss — title-match delete "
                    f"needles={needles[:3]!r}",
                    flush=True,
                )
                ok = remove_from_selling_by_title(
                    page, needles, action="delete"
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
                        title_needles=needles or None,
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

    if remaining_after_cap > 0:
        stats.incomplete = True
        stats.failed += remaining_after_cap
        print(
            f"  [{account_id}] INCOMPLETE: {remaining_after_cap} age-eligible "
            f"listing(s) left after max_deletes={max_deletes}",
            flush=True,
        )

    return stats
