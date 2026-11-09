"""Purge Marketplace selling-shelf listings older than a day threshold."""
from __future__ import annotations

import hashlib
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

# Clicks / Marketplace meta — not a listing date (must precede calendar parsers).
_CLICKS_META_RE = re.compile(
    r"listed\s+on\s+marketplace|publicado\s+en\s+marketplace|"
    r"clicks?\s+on\s+listing|clics?\s+en\s+la\s+publicaci[oó]n|"
    r"visualizaciones",
    re.I,
)


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
    require_year: bool = False,
) -> list[str]:
    """Build title needles for Seller Hub delete when item_id href is missing.

    Returns full + partial cores (``2019 Porsche Cayenne``). When
    ``require_year`` (sync.db fallbacks), every needle keeps the model year so
    ``2019`` cannot match a ``2023 Porsche Cayenne`` card.
    """
    from src.facebook.remover import _expand_partial_title_needles, _strip_title_noise

    needles: list[str] = []
    title = _strip_title_noise(vehicle_title or "")
    if len(title) >= 4:
        needles.append(title)
    # Card text often starts with "YYYY Brand Model … Listed on …"
    blob = (card_text or "").strip()
    if blob:
        chunk = re.split(
            r"(?i)\b(?:listed on|publicado|clicks?\s+on|clics?\s+en)\b",
            blob,
            maxsplit=1,
        )[0].strip()
        chunk = _strip_title_noise(re.sub(r"\s+", " ", chunk))
        if len(chunk) >= 4:
            needles.append(chunk[:120])
        m = re.search(
            r"\b((?:19|20)\d{2}\s+[A-Za-zÁÉÍÓÚÑáéíóúñ]"
            r"[A-Za-zÁÉÍÓÚÑáéíóúñ0-9\-]*(?:\s+[A-Za-zÁÉÍÓÚÑáéíóúñ0-9\-]{1,24}){0,3})",
            blob,
        )
        if m:
            needles.append(m.group(1).strip()[:80])

    catalog = _catalog_by_autosell_id()
    aid = (autosell_id or "").strip()
    if not aid or aid == item_id or aid.startswith("dom-"):
        row = _lookup_db_row(store, account_id, item_id)
        if row is not None:
            aid = str(row["autosell_id"] or "")
    vehicle = catalog.get(aid) if aid else None
    if vehicle is not None:
        try:
            needles.extend(_match_needles(vehicle))
        except Exception:
            pass
        vt = _strip_title_noise(str(getattr(vehicle, "title", "") or ""))
        year = str(getattr(vehicle, "year", "") or "").strip()
        brand = str(getattr(vehicle, "brand", "") or "").strip()
        if year and brand and vt:
            needles.append(f"{year} {brand} {vt}")
        elif year and vt and year not in vt:
            needles.append(f"{year} {vt}")
        if len(vt) >= 4:
            needles.append(vt)

    return _expand_partial_title_needles(needles, require_year=require_year)


def _mark_db_row_already_removed(
    store: SyncStore,
    account_id: str,
    *,
    autosell_id: str,
    item_id: str,
    reason: str,
) -> None:
    """Clear a dead sync.db mapping so preflight can proceed (ALREADY_REMOVED)."""
    aid = (autosell_id or "").strip() or item_id
    try:
        store.mark_fb_listing_removed(aid, account_id, clear_url=True)
        print(
            f"  ALREADY_REMOVED autosell_id={aid} item={item_id} "
            f"— sync.db cleared ({reason})",
            flush=True,
        )
    except Exception as exc:
        print(
            f"  WARNING: ALREADY_REMOVED clear failed autosell_id={aid}: {exc}",
            flush=True,
        )


def _db_listing_already_gone_on_facebook(
    page: Page,
    *,
    listing_url: str,
    needles: list[str],
) -> bool:
    """True when detail is 404/unavailable and year-locked title is absent on shelf."""
    from src.facebook.remover import (
        _detail_looks_manually_gone,
        _is_content_unavailable,
        _listing_already_gone,
        extract_item_id,
        selling_title_present,
    )

    href = listing_url or ""
    if href and not href.startswith("http"):
        href = f"https://www.facebook.com{href}"
    if not href:
        # No URL — rely on year-strict shelf absence only.
        if not needles:
            return False
        return not selling_title_present(
            page, needles, max_scrolls=16, require_year=True
        )

    try:
        page.goto(href, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(1_200)
    except Exception as exc:
        print(f"  ALREADY_REMOVED check nav failed: {exc}", flush=True)

    item_id = extract_item_id(href) or ""
    detail_gone = (
        _is_content_unavailable(page)
        or _listing_already_gone(page, listing_url=href, item_id=item_id)
        or _detail_looks_manually_gone(page)
    )
    if not detail_gone:
        return False
    if needles and selling_title_present(
        page, needles, max_scrolls=16, require_year=True
    ):
        return False
    return True


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

    # Catalog / sync.db rows always year-lock title match (avoid 2019→2023).
    strict_year = bool(
        (autosell_id or "").startswith("obj")
        or bool(re.search(r"\b(?:19|20)\d{2}\b", vehicle_title or card_text or ""))
    )
    needles = list(title_needles or [])
    if not needles:
        needles = _title_needles_for_purge(
            store=store,
            account_id=account_id,
            item_id=item_id,
            autosell_id=autosell_id,
            vehicle_title=vehicle_title,
            card_text=card_text,
            require_year=strict_year,
        )

    ok = False
    if item_id and str(item_id).isdigit():
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
            f"item={item_id or '?'} require_year={strict_year}",
            flush=True,
        )
        try:
            ok = remove_from_selling_by_title(
                page,
                needles,
                action="delete",
                require_year=strict_year,
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

    # Dead sync.db row: detail 404 + year-specific title absent → clear mapping.
    if not ok and _db_listing_already_gone_on_facebook(
        page, listing_url=href, needles=needles
    ):
        _mark_db_row_already_removed(
            store,
            account_id,
            autosell_id=autosell_id,
            item_id=item_id,
            reason="detail 404/gone and year-title absent on shelf",
        )
        return True

    if not ok and item_id and str(item_id).isdigit():
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
    """Elapsed whole calendar days from ``YYYY-MM-DD`` to ``when``'s date (UTC).

    ``10/24`` vs ``2026-11-05`` → ``12.0`` (not a month-offset approximation).
    October→November transitions are plain date subtraction — no month rollover hacks.
    """
    from datetime import date as date_cls

    try:
        posted_d = date_cls(year, month, day)
    except ValueError:
        return None
    when_utc = when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)
    when_d = when_utc.astimezone(timezone.utc).date()
    if posted_d > when_d and not year_explicit:
        try:
            posted_d = date_cls(year - 1, month, day)
        except ValueError:
            return None
    return float((when_d - posted_d).days)


def _parse_slash_md(
    a: int,
    b: int,
    *,
    prefer_md: bool = True,
) -> tuple[int, int] | None:
    """Map slash pair ``a/b`` → ``(month, day)``. ``10/24`` → ``(10, 24)``."""
    candidates: list[tuple[int, int]] = []
    if a > 12 and 1 <= b <= 12:
        candidates.append((b, a))  # DD/MM
    elif b > 12 and 1 <= a <= 12:
        candidates.append((a, b))  # MM/DD
    elif 1 <= a <= 12 and 1 <= b <= 12:
        candidates = [(a, b), (b, a)] if prefer_md else [(b, a), (a, b)]
    for month, day in candidates:
        if 1 <= month <= 12 and 1 <= day <= 31:
            return month, day
    return None


def _parse_slash_date_age(
    a: int,
    b: int,
    *,
    year: int,
    when: datetime,
    year_explicit: bool,
    prefer_md: bool = True,
) -> float | None:
    """Age from ``a/b`` slash date. ``10/24`` → Oct 24 (US MM/DD) when unambiguous."""
    md = _parse_slash_md(a, b, prefer_md=prefer_md)
    if md is None:
        return None
    month, day = md
    # Also try alternate orientation when ambiguous so caller can take max.
    ages: list[float] = []
    age = _age_from_calendar_date(
        year, month, day, when=when, year_explicit=year_explicit
    )
    if age is not None and 0 <= age <= 400:
        ages.append(age)
    if prefer_md and 1 <= a <= 12 and 1 <= b <= 12 and a != b:
        alt = _parse_slash_md(a, b, prefer_md=False)
        if alt is not None and alt != (month, day):
            age_alt = _age_from_calendar_date(
                year, alt[0], alt[1], when=when, year_explicit=year_explicit
            )
            if age_alt is not None and 0 <= age_alt <= 400:
                ages.append(age_alt)
    if not ages:
        return None
    # Seller Hub US: prefer MM/DD (first). Purge-safe callers may max() across matches.
    return ages[0]


def parse_explicit_dom_calendar_age(
    text: str, *, now: datetime | None = None
) -> float | None:
    """Strict calendar-date age from DOM labels (overrides GraphQL/DB).

    Recognizes ``Listed on 10/24``, ``Publicado el 24/10``, ``Oct 24``, and bare
    ``10/24`` / ``10/28``. When a card blob contains multiple slash dates (e.g. a
    newer boost/meta ``11/1`` before the real ``10/24``), prefers labeled shelf
    dates, then the **oldest** plausible bare date so October listings are not
    collapsed to ~4.9d.
    """
    raw = text or ""
    # Drop clicks / Marketplace meta so they cannot confuse slash matching.
    scrubbed = _CLICKS_META_RE.sub(" ", raw)
    blob = " ".join(scrubbed.lower().split())
    if not blob:
        return None
    when = now or datetime.now(timezone.utc)

    def _year_from(group: str | None) -> tuple[int, bool]:
        if group:
            year = int(group)
            if year < 100:
                year += 2000
            return year, True
        return when.year, False

    labeled_ages: list[tuple[float, str]] = []

    # "Listed on 10/24" / "Listed on 10/24/2025" (US MM/DD) — not Marketplace.
    for m in re.finditer(
        r"listed\s+on\s+(?!marketplace)(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?",
        blob,
    ):
        year, explicit = _year_from(m.group(3))
        month, day = int(m.group(1)), int(m.group(2))
        age = _age_from_calendar_date(
            year, month, day, when=when, year_explicit=explicit
        )
        if age is not None and 0 <= age <= 400:
            labeled_ages.append((age, f"listed_on {month:02d}/{day:02d}"))

    # "Publicado el/en 24/10" (ES DD/MM).
    for m in re.finditer(
        r"publicado\s+(?:el|en)\s+(?!marketplace)(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?",
        blob,
    ):
        year, explicit = _year_from(m.group(3))
        day, month = int(m.group(1)), int(m.group(2))
        age = _age_from_calendar_date(
            year, month, day, when=when, year_explicit=explicit
        )
        if age is not None and 0 <= age <= 400:
            labeled_ages.append((age, f"publicado {day:02d}/{month:02d}"))

    # "Publicado el 20 de agosto"
    m = re.search(
        r"(?:publicado\s+el|listed\s+on)\s+(\d{1,2})\s+de\s+([a-záéíóúñ]+)",
        blob,
    )
    if m:
        month = _MONTHS_ES.get(m.group(2))
        if month:
            age = _age_from_calendar_date(when.year, month, int(m.group(1)), when=when)
            if age is not None and 0 <= age <= 400:
                labeled_ages.append((age, f"publicado_es {month:02d}/{int(m.group(1)):02d}"))

    # "Listed on Oct 24" / "Listed on October 24, 2025"
    m = re.search(
        r"listed\s+on\s+(?!marketplace)([a-z]+)\s+(\d{1,2})(?:,\s*(\d{4}))?",
        blob,
    )
    if m:
        month = _MONTHS_EN.get(m.group(1))
        if month:
            year, explicit = _year_from(m.group(3))
            age = _age_from_calendar_date(
                year, month, int(m.group(2)), when=when, year_explicit=explicit
            )
            if age is not None and 0 <= age <= 400:
                labeled_ages.append(
                    (age, f"listed_on_mon {month:02d}/{int(m.group(2)):02d}")
                )

    if labeled_ages:
        # Oldest labeled shelf date wins (purge-safe across multi-date cards).
        labeled_ages.sort(key=lambda t: t[0], reverse=True)
        age, how = labeled_ages[0]
        print(
            f"  DOM calendar parse: {how} → {age:.1f}d "
            f"(vs {when.astimezone(timezone.utc).date().isoformat()})",
            flush=True,
        )
        return age

    # Bare slash dates: US Seller Hub is MM/DD (10/24=Oct 24). Collect each
    # primary reading and pick the oldest so a newer meta date (11/1→4.9d)
    # cannot beat the real listing date (10/24→12d). Do NOT flip ambiguous
    # pairs to DD/MM here — that turns 11/1 into Jan 11 (~300d) falsely.
    bare_ages: list[tuple[float, str]] = []
    for m in re.finditer(
        r"(?<!\d)(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?!\d)",
        blob,
    ):
        year, explicit = _year_from(m.group(3))
        a, b = int(m.group(1)), int(m.group(2))
        # Unambiguous ES DD/MM (24/10): a>12.
        prefer_md = not (a > 12 and 1 <= b <= 12)
        md = _parse_slash_md(a, b, prefer_md=prefer_md)
        if md is None:
            continue
        month, day = md
        age = _age_from_calendar_date(
            year, month, day, when=when, year_explicit=explicit
        )
        if age is not None and 0 <= age <= 120:
            # Cap at ~4 months — rejects bogus far-past flips while keeping
            # October listings in a November purge window.
            bare_ages.append((age, f"bare {month:02d}/{day:02d}"))

    if bare_ages:
        bare_ages.sort(key=lambda t: t[0], reverse=True)
        age, how = bare_ages[0]
        print(
            f"  DOM calendar parse: {how} → {age:.1f}d "
            f"(vs {when.astimezone(timezone.utc).date().isoformat()}; "
            f"{len(bare_ages)} slash candidate(s))",
            flush=True,
        )
        return age

    return None


def parse_listing_age_days(text: str, *, now: datetime | None = None) -> float | None:
    """Parse Marketplace age strings (EN/ES) into approximate age in days.

    Prefers explicit calendar shelf labels (``10/24``, ``Listed on Oct 24``,
    ``Publicado el 24/10``) over relative phrases so DOM dates drive the
    7-day hard purge.
    """
    when = now or datetime.now(timezone.utc)
    cal = parse_explicit_dom_calendar_age(text, now=when)
    if cal is not None:
        return cal

    blob = " ".join((text or "").lower().split())
    if not blob:
        return None

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

    return None


def resolve_listing_age_for_purge(
    *,
    live_age_days: float | None,
    live_age_text: str = "",
    live_card_text: str = "",
    db_age_days: float | None = None,
    now: datetime | None = None,
) -> tuple[float | None, str]:
    """Resolve purge age: explicit DOM calendar date beats everything else.

    Priority:
      1. Explicit shelf calendar date (``10/24``, ``Listed on 10/28``, …)
      2. Other live age text / relative phrases
      3. Numeric ``live_age_days`` (GraphQL creation_time etc.)
      4. ``sync.db`` ``posted_at`` (last resort only)
    """
    when = now or datetime.now(timezone.utc)
    age_snip = (live_age_text or "").strip()
    card_blob = (live_card_text or "").strip()
    blob = f"{age_snip} {card_blob}".strip()

    # 1) Strict DOM calendar date — source of truth for 7-day hard purge.
    # Prefer the isolated date snip (``10/24``) before scanning the full card
    # blob, which may contain newer meta dates (``11/1`` → false ~4.9d).
    cal = None
    cal_src = ""
    if age_snip:
        cal = parse_explicit_dom_calendar_age(age_snip, now=when)
        if cal is not None:
            cal_src = "dom_calendar(age_text)"
    if cal is None and card_blob:
        cal = parse_explicit_dom_calendar_age(card_blob, now=when)
        if cal is not None:
            cal_src = "dom_calendar(card)"
    if cal is None and blob and blob not in (age_snip, card_blob):
        cal = parse_explicit_dom_calendar_age(blob, now=when)
        if cal is not None:
            cal_src = "dom_calendar(blob)"
    if cal is not None:
        source = cal_src or "dom_calendar"
        if live_age_days is not None and abs(float(cal) - float(live_age_days)) >= 0.5:
            print(
                f"  age override: DOM calendar={cal:.1f}d "
                f"overrides live_age_days={float(live_age_days):.1f}d",
                flush=True,
            )
            source = f"{source} (live_age ignored)"
        if db_age_days is not None and abs(float(cal) - float(db_age_days)) >= 0.5:
            print(
                f"  age conflict: DOM calendar={cal:.1f}d vs sync.db={db_age_days:.1f}d "
                f"— using DOM date",
                flush=True,
            )
            source = f"{source} (db={db_age_days:.1f}d ignored)"
        floored, oct_hit = apply_october_hard_purge_floor(cal, blob, when=when)
        if oct_hit and floored is not None and floored > float(cal):
            print(
                f"  October hard-purge: DOM {cal:.1f}d → {floored:.1f}d "
                f"(10/XX in November; queue UI delete)",
                flush=True,
            )
            source = f"{source}+october_hard_purge"
            return float(floored), source
        if oct_hit:
            source = f"{source}+october"
        return float(cal), source

    # 2) Relative / other live text (no explicit calendar).
    if blob:
        text_age = parse_listing_age_days(blob, now=when)
        if text_age is not None:
            floored, oct_hit = apply_october_hard_purge_floor(
                text_age, blob, when=when
            )
            if oct_hit and floored is not None and floored > float(text_age):
                print(
                    f"  October hard-purge: live_text {text_age:.1f}d → {floored:.1f}d "
                    f"(10/XX in November; queue UI delete)",
                    flush=True,
                )
                return float(floored), "live_text+october_hard_purge"
            if db_age_days is not None and abs(float(text_age) - float(db_age_days)) >= 0.5:
                print(
                    f"  age conflict: live_text={text_age:.1f}d vs sync.db={db_age_days:.1f}d "
                    f"— using live platform state",
                    flush=True,
                )
                return float(text_age), f"live_text (db={db_age_days:.1f}d ignored)"
            return float(text_age), "live_text"

    # 2b) October date present but calendar/relative parse missed — still floor to 7d.
    floored_only, oct_only = apply_october_hard_purge_floor(None, blob, when=when)
    if oct_only and floored_only is not None:
        print(
            f"  October hard-purge: no numeric age → {floored_only:.1f}d "
            f"(10/XX in November; queue UI delete)",
            flush=True,
        )
        return float(floored_only), "october_hard_purge"

    # 3) Numeric live age (GraphQL) when no DOM date/text age.
    if live_age_days is not None:
        if db_age_days is not None and abs(float(live_age_days) - float(db_age_days)) >= 0.5:
            print(
                f"  age conflict: live={float(live_age_days):.1f}d vs sync.db={db_age_days:.1f}d "
                f"— using live platform state",
                flush=True,
            )
            return float(live_age_days), f"live (db={db_age_days:.1f}d ignored)"
        return float(live_age_days), "live"

    # 4) sync.db last resort.
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


def dom_text_has_october_date(text: str) -> bool:
    """True when DOM text shows an October shelf date (``10/XX``, ``XX/10``, Oct)."""
    scrubbed = _CLICKS_META_RE.sub(" ", text or "")
    blob = " ".join(scrubbed.lower().split())
    if not blob:
        return False
    if re.search(r"\b(?:oct(?:ober)?|octubre)\b", blob):
        return True
    # US MM/DD: 10/24, 10/28, 10/31
    for m in re.finditer(r"(?<!\d)10/(\d{1,2})(?:/\d{2,4})?(?!\d)", blob):
        if 1 <= int(m.group(1)) <= 31:
            return True
    # ES DD/MM: 24/10, 28/10, 31/10
    for m in re.finditer(r"(?<!\d)(\d{1,2})/10(?:/\d{2,4})?(?!\d)", blob):
        day = int(m.group(1))
        if day > 12:  # unambiguous day-first October
            return True
    return False


def apply_october_hard_purge_floor(
    age: float | None,
    text: str,
    *,
    when: datetime,
    min_days: float = 7.0,
) -> tuple[float | None, bool]:
    """In November, any October DOM date is treated as at least ``min_days`` old.

    FB often refreshes the visible date to ``10/31`` on cards originally listed
    Oct 24/28; raw math yields ~5d and would SKIP young. Floor to ≥7d so they
    enter the UI title-match delete queue.
    """
    when_utc = when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)
    if when_utc.astimezone(timezone.utc).month != 11:
        return age, False
    if not dom_text_has_october_date(text):
        return age, False
    floor = float(min_days)
    if age is None:
        return floor, True
    if float(age) >= floor:
        return float(age), True  # still flag as October rule context
    return floor, True


def extract_listed_on_snip(text: str) -> str:
    """Best-effort snippet of FB shelf date/age label for logs.

    Ignores ``Listed on Marketplace · N clicks on listing`` meta lines.
    """
    blob = _CLICKS_META_RE.sub(" ", text or "")
    m = re.search(
        r"(listed\s+on\s+(?!marketplace)\d{1,2}/\d{1,2}(?:/\d{2,4})?"
        r"|listed\s+on\s+(?!marketplace)[A-Za-z]+\s+\d{1,2}(?:,\s*\d{4})?"
        r"|publicado\s+(?:el|en)\s+(?!marketplace)\d{1,2}/\d{1,2}(?:/\d{2,4})?"
        r"|publicado\s+(?:el|en)\s+(?!marketplace)[^\n]{0,40}"
        r"|hace\s+\d+[^\n]{0,24}"
        r"|\d+\s+days?\s+ago"
        r"|\d+\s+d[ií]as?"
        r"|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b)",
        blob,
        re.I,
    )
    return m.group(0).strip() if m else ""


_VEHICLE_TITLE_RE = re.compile(
    r"\b((?:19|20)\d{2}\s+[A-Za-zÁÉÍÓÚÑáéíóúñ]"
    r"[A-Za-zÁÉÍÓÚÑáéíóúñ0-9\-\s]{2,60})"
)


def vehicle_title_from_card_text(text: str) -> str:
    """Pull ``2022 Mercedes-Benz`` / ``1957 Plymouth Savoy``-style titles."""
    blob = re.sub(r"\s+", " ", (text or "").strip())
    if not blob:
        return ""
    # Prefer chunk before clicks meta / Listed on / Publicado / bare date.
    head = re.split(
        r"(?i)\b(?:listed\s+on|publicado|clicks?\s+on|clics?\s+en)\b|"
        r"\b\d{1,2}/\d{1,2}\b",
        blob,
        maxsplit=1,
    )[0].strip()
    for candidate in (head, blob):
        m = _VEHICLE_TITLE_RE.search(candidate)
        if m:
            return m.group(1).strip()[:80]
    if 5 <= len(head) <= 80 and not _CLICKS_META_RE.search(head):
        return head[:80]
    return ""


# Prefer text content over fragile structural roles (Seller Hub DOM churns).
_LISTED_ON_TEXT_NEEDLES = (
    "Listed on",
    "Publicado el",
    "Publicado en",
    "Publicado",
)

# EN + ES labels and raw slash dates (10/24, 24/10, 10/28, 28/10).
_LISTED_ON_WAIT_RE = re.compile(
    r"Listed\s+on|Publicado(?:\s+el|\s+en)?|\b\d{1,2}/\d{1,2}\b",
    re.I,
)

# Seller Hub section for blocked / duplicate publishes (account_2 ES + EN).
# Defined early so shelf wait / discovery can see attention cards.
_REQUIEREN_ATENCION_SECTION_RE = re.compile(
    r"requieren\s+atenci[oó]n|requires?\s+attention|needs?\s+attention|"
    r"necesitan\s+atenci[oó]n",
    re.I,
)
_REQUIEREN_ATENCION_BANNER_RE = re.compile(
    r"no\s+se\s+public[oó]\s+porque\s+parece\s+un\s+duplicado|"
    r"parece\s+un\s+duplicado|"
    r"elim[ií]nala\s+y\s+vuelve\s+a\s+intentarlo|"
    r"this\s+looks\s+like\s+a\s+duplicate|"
    r"we\s+didn'?t\s+post\s+it|"
    r"delete\s+it\s+and\s+try\s+again|"
    r"delete\s+and\s+try\s+again",
    re.I,
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
    """Wait until Seller Hub shows EN/ES date labels, bare dates, or attention cards."""
    ms = int(timeout_ms if timeout_ms is not None else _shelf_wait_timeout_ms())
    deadline_errors: list[str] = []

    # 0) Requieren atención / Requires attention (duplicate-blocked cards).
    # Short probe — do not burn the full shelf timeout when the section is absent.
    try:
        page.get_by_text(_REQUIEREN_ATENCION_SECTION_RE).first.wait_for(
            state="visible", timeout=min(ms, 1_500)
        )
        print(
            "  shelf wait OK text=Requieren atención|Requires attention",
            flush=True,
        )
        return True
    except Exception as exc:
        deadline_errors.append(f"attention-section:{exc}")
    try:
        page.get_by_text(_REQUIEREN_ATENCION_BANNER_RE).first.wait_for(
            state="visible", timeout=min(ms, 1_200)
        )
        print(
            "  shelf wait OK text=duplicate-banner (attention cards)",
            flush=True,
        )
        return True
    except Exception as exc:
        deadline_errors.append(f"attention-banner:{exc}")

    # 1) Combined EN/ES + raw slash-date wait (Spanish accounts often omit labels).
    try:
        page.get_by_text(_LISTED_ON_WAIT_RE).first.wait_for(
            state="visible", timeout=ms
        )
        print(
            f"  shelf wait OK text=EN/ES|raw-date timeout_ms={ms}",
            flush=True,
        )
        return True
    except Exception as exc:
        deadline_errors.append(f"text:combined: {exc}")

    # 2) Explicit needle pass (Listed on / Publicado el / …).
    for needle in _LISTED_ON_TEXT_NEEDLES:
        try:
            page.get_by_text(needle, exact=False).first.wait_for(
                state="visible", timeout=max(3_000, ms // 2)
            )
            print(
                f"  shelf wait OK text={needle!r} timeout_ms={ms}",
                flush=True,
            )
            return True
        except Exception as exc:
            deadline_errors.append(f"text:{needle}: {exc}")
            continue

    # 3) Soft fallback: marketplace item anchors (older layouts).
    for sel in (
        'a[href*="/marketplace/item/"]',
        'xpath=//*[contains(text(),"Listed on") or contains(text(),"Publicado") '
        'or contains(text(),"/")]',
    ):
        try:
            page.wait_for_selector(sel, state="visible", timeout=max(3_000, ms // 2))
            print(
                f"  shelf wait OK selector={sel!r} timeout_ms={ms}",
                flush=True,
            )
            return True
        except Exception as exc:
            deadline_errors.append(f"{sel}: {exc}")
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
    """Find Seller Hub cards by date / clicks labels, climb to titled wrapper.

    Seller Hub often shows ``Listed on Marketplace · N clicks on listing`` instead
    of a direct item href. Treat that as a card anchor, walk up until the parent
    contains a vehicle heading (``2022 Mercedes-Benz…``), then pull title + a
    real date string (``10/24``, ``Listed on 10/24``, ``Publicado el 24/10``)
    from inside that same container — never treat the clicks line as the date.
    """
    try:
        raw = page.evaluate(
            r"""() => {
              const DATE_LABEL_RE = /Listed\s+on|Publicado(?:\s+el|\s+en)?/i;
              // Clicks / Marketplace meta line — useful as an anchor, NOT as a date.
              const CLICKS_RE =
                /Listed\s+on\s+Marketplace|Publicado\s+en\s+Marketplace|clicks?\s+on\s+listing|clics?\s+en\s+la\s+publicaci[oó]n|visualizaciones/i;
              const RAW_DATE_RE =
                /\b(?:0?[1-9]|1[0-2])\/(?:0?[1-9]|[12]\d|3[01])(?:\/\d{2,4})?\b|\b(?:0?[1-9]|[12]\d|3[01])\/(?:0?[1-9]|1[0-2])(?:\/\d{2,4})?\b/;
              // Real shelf dates only — exclude "Listed on Marketplace … clicks".
              const DATE_SNIP_RE =
                /Listed\s+on\s+(?!Marketplace)[A-Za-z]{3,9}\.?\s+\d{1,2}(?:,\s*\d{4})?|Listed\s+on\s+(?!Marketplace)\d{1,2}\/\d{1,2}(?:\/\d{2,4})?|Publicado(?:\s+el|\s+en)?\s+(?!Marketplace)\d{1,2}\/\d{1,2}(?:\/\d{2,4})?|Publicado(?:\s+el|\s+en)?\s+(?!Marketplace)\d{1,2}\s+de\s+[A-Za-záéíóúñ]+|\b(?:0?[1-9]|1[0-2])\/(?:0?[1-9]|[12]\d|3[01])(?:\/\d{2,4})?\b|\b(?:0?[1-9]|[12]\d|3[01])\/(?:0?[1-9]|1[0-2])(?:\/\d{2,4})?\b/i;
              const TITLE_RE =
                /\b((?:19|20)\d{2}\s+[A-Za-zÁÉÍÓÚÑáéíóúñ][A-Za-zÁÉÍÓÚÑáéíóúñ0-9\-\s]{2,60})/;
              const ID_RE = /\/marketplace\/item\/(\d{8,})/;
              const ANY_ID_RE =
                /(?:item_id|listing_id|for_sale_item_id|story_id)[=:\"'\s]+(\d{8,})/i;
              const out = [];
              const failed = [];
              const seen = new Set();
              const root = document.querySelector('[role="main"]') || document.body;

              const isVisible = (el) => {
                if (!el || !el.getBoundingClientRect) return false;
                const r = el.getBoundingClientRect();
                return r.width > 2 && r.height > 2;
              };

              const looksLikeAnchor = (t) => {
                if (!t) return false;
                if (CLICKS_RE.test(t)) return true;
                if (DATE_LABEL_RE.test(t) && !CLICKS_RE.test(t)) return true;
                if (t.length <= 48 && RAW_DATE_RE.test(t)) return true;
                if (/\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}\b/i.test(t))
                  return true;
                return false;
              };

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
                  const own = (el.childNodes && el.childNodes.length)
                    ? Array.from(el.childNodes)
                        .filter((c) => c.nodeType === 3)
                        .map((c) => (c.textContent || '').trim())
                        .join(' ')
                    : '';
                  const direct = own || ((el.children && el.children.length === 0)
                    ? (el.textContent || '').trim()
                    : '');
                  if (direct && looksLikeAnchor(direct) && direct.length < 160) {
                    candidates.push(el);
                  }
                }
                node = walk.nextNode();
              }
              if (!candidates.length) {
                root.querySelectorAll('span, div, a, p').forEach((el) => {
                  const t = (el.innerText || el.textContent || '').trim();
                  if (t && looksLikeAnchor(t) && t.length < 200 && isVisible(el)) {
                    candidates.push(el);
                  }
                });
              }

              // Climb until the wrapper holds a YEAR + brand/model heading.
              const climbCard = (el) => {
                let n = el;
                let withTitle = null;
                let preferred = null;
                let best = el;
                for (let depth = 1; depth <= 14 && n; depth++) {
                  n = n.parentElement;
                  if (!n) break;
                  // Stop before swallowing the whole Seller Hub main column.
                  if (n === root || n.getAttribute('role') === 'main') break;
                  const txt = (n.innerText || '').replace(/\s+/g, ' ').trim();
                  const len = txt.length;
                  if (len < 12 || len > 3200) continue;
                  best = n;
                  const hasTitle = TITLE_RE.test(txt);
                  const hasChrome = !!(
                    n.querySelector
                    && (n.querySelector('img')
                        || n.querySelector('a[href*="marketplace"]')
                        || n.querySelector('[aria-label*="Más"]')
                        || n.querySelector('[aria-label*="More"]')
                        || n.querySelector('[aria-haspopup="menu"]')
                        || n.querySelector('h1, h2, h3, [role="heading"]'))
                  );
                  if (hasTitle) {
                    withTitle = n;
                    // Prefer the smallest titled wrapper that also has chrome.
                    if (hasChrome || depth >= 2) return n;
                  }
                  if (depth >= 2 && depth <= 6) {
                    preferred = n;
                    if (hasTitle && (hasChrome || len >= 40)) return n;
                  }
                }
                return withTitle || preferred || best;
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

              const extractTitle = (cardText) => {
                // Header / first text block before clicks / date meta.
                const head = (cardText || '').split(
                  /Listed\s+on|Publicado|clicks?\s+on|clics?\s+en|\b\d{1,2}\/\d{1,2}\b/i
                )[0].trim();
                let m = head.match(TITLE_RE) || (cardText || '').match(TITLE_RE);
                if (m) return m[1].replace(/\s+/g, ' ').trim().slice(0, 80);
                // Heading-like first line.
                const firstLine = head.split(/\n|·|\|/)[0].trim();
                if (firstLine.length >= 5 && firstLine.length <= 80
                    && !CLICKS_RE.test(firstLine)) {
                  return firstLine.slice(0, 80);
                }
                return '';
              };

              const extractDateText = (labelText, cardText) => {
                // Never treat clicks / Marketplace meta as the listing date.
                const cleanedLabel = CLICKS_RE.test(labelText) ? '' : labelText;
                const sources = [cleanedLabel, cardText];
                for (const src of sources) {
                  if (!src) continue;
                  // Strip clicks segments before matching dates.
                  const scrubbed = src
                    .replace(CLICKS_RE, ' ')
                    .replace(/\s+/g, ' ')
                    .trim();
                  const m = scrubbed.match(DATE_SNIP_RE);
                  if (m) {
                    const snip = m[0].trim();
                    if (!CLICKS_RE.test(snip)) return snip;
                  }
                }
                // Bare slash date anywhere in the card.
                const bare = (cardText || '').match(RAW_DATE_RE);
                return bare ? bare[0] : '';
              };

              for (const labelEl of candidates) {
                if (!isVisible(labelEl)) continue;
                const card = climbCard(labelEl);
                if (!card) continue;
                const labelText = (labelEl.innerText || labelEl.textContent || '')
                  .replace(/\s+/g, ' ').trim();
                const cardText = (card.innerText || '').replace(/\s+/g, ' ').trim();
                const title = extractTitle(cardText);
                const date_text = extractDateText(labelText, cardText);
                const { item_id, href } = extractHrefId(card);
                const key = item_id
                  || (title + '|' + date_text + '|' + cardText.slice(0, 60));
                if (seen.has(key)) continue;
                seen.add(key);

                if (!item_id && !title) {
                  failed.push({
                    reason: 'no_item_id_or_title',
                    raw: cardText.slice(0, 400),
                    date_text,
                    label: labelText.slice(0, 120),
                  });
                  continue;
                }
                if (!date_text) {
                  failed.push({
                    reason: 'title_ok_but_no_date',
                    raw: cardText.slice(0, 400),
                    title,
                    label: labelText.slice(0, 120),
                  });
                  // Still emit — Python may age via relative phrases elsewhere.
                }
                out.push({
                  item_id: item_id || '',
                  href: href || (item_id
                    ? ('https://www.facebook.com/marketplace/item/' + item_id + '/')
                    : ''),
                  date_text,
                  title,
                  blob: ((title ? title + ' ' : '') + (date_text ? date_text + ' ' : '')
                    + cardText).replace(/\s+/g, ' ').trim().slice(0, 900),
                  parse_ok: !!(title && (item_id || date_text)),
                });
              }
              return { cards: out, failed, candidate_count: candidates.length };
            }"""
        )
    except Exception as exc:
        print(f"  WARNING: JS Listed-on card extract failed: {exc}", flush=True)
        return []

    rows: list[Any]
    failed_rows: list[Any] = []
    candidate_count = 0
    if isinstance(raw, dict):
        rows = list(raw.get("cards") or [])
        failed_rows = list(raw.get("failed") or [])
        try:
            candidate_count = int(raw.get("candidate_count") or 0)
        except (TypeError, ValueError):
            candidate_count = 0
    elif isinstance(raw, list):
        rows = raw
    else:
        return []

    cleaned: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item_id = str(row.get("item_id") or "").strip()
        href = str(row.get("href") or "").strip()
        blob = str(row.get("blob") or "").strip()
        date_text = str(row.get("date_text") or "").strip()
        title = str(row.get("title") or "").strip() or vehicle_title_from_card_text(
            blob
        )
        # Never keep clicks meta as the listing date.
        if date_text and _CLICKS_META_RE.search(date_text):
            date_text = ""
        if not date_text:
            date_text = extract_listed_on_snip(blob)
        if date_text and date_text not in blob:
            blob = f"{date_text} {blob}".strip()
        if title and title not in blob:
            blob = f"{title} {blob}".strip()
        parse_ok = bool(title) and bool(item_id or date_text)
        if not parse_ok:
            print(
                f"  DOM card parse weak/fail date={date_text!r} "
                f"title={title!r} raw={blob[:240]!r}",
                flush=True,
            )
            if not title:
                continue
        if item_id.isdigit() and len(item_id) >= 8:
            cleaned.append(
                {
                    "item_id": item_id,
                    "href": href,
                    "blob": blob,
                    "date_text": date_text,
                    "title": title,
                }
            )
        elif title:
            # Title cards (often from clicks-label climb) → UI delete queue.
            cleaned.append(
                {
                    "item_id": "",
                    "href": href,
                    "blob": blob,
                    "date_text": date_text,
                    "title": title,
                }
            )

    for fr in failed_rows[:12]:
        if not isinstance(fr, dict):
            continue
        print(
            f"  DOM card unparsed ({fr.get('reason', '?')}): "
            f"{str(fr.get('raw') or '')[:240]!r}",
            flush=True,
        )
    if candidate_count and not cleaned:
        print(
            f"  DOM date labels seen={candidate_count} but 0 cards parsed — "
            f"check raw logs above for UI variant",
            flush=True,
        )
    elif candidate_count:
        print(
            f"  DOM date-label candidates={candidate_count} "
            f"parsed_cards={len(cleaned)}",
            flush=True,
        )
    return cleaned


def _extract_selling_cards_via_text_locators(page: Page) -> list[dict[str, str]]:
    """Playwright locator fallback: get_by_text → titled parent → date + link."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    needles: list[str | re.Pattern[str]] = list(_LISTED_ON_TEXT_NEEDLES) + [
        "Listed on Marketplace",
        "Publicado en Marketplace",
        "clicks on listing",
        "clics en la publicación",
        _LISTED_ON_WAIT_RE,
    ]
    for needle in needles:
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
                date_text = needle if isinstance(needle, str) else ""
            # Climb to wrapper that contains a vehicle title heading.
            card = None
            for xpath in (
                "xpath=ancestor::div[2]",
                "xpath=ancestor::div[3]",
                "xpath=ancestor::div[4]",
                "xpath=ancestor::div[5]",
                "xpath=ancestor::div[6]",
                "xpath=ancestor::div[8]",
                "xpath=ancestor::div[.//a[contains(@href,'/marketplace/item/')]][1]",
                "xpath=ancestor::div[.//h2 or .//*[@role='heading']][1]",
            ):
                try:
                    cand = label.locator(xpath).first
                    if cand.count() == 0:
                        continue
                    try:
                        txt = (cand.inner_text(timeout=800) or "").strip()
                    except Exception:
                        txt = ""
                    if not (12 <= len(txt) <= 3200):
                        continue
                    card = cand
                    if vehicle_title_from_card_text(txt):
                        break
                except Exception:
                    continue
            if card is None:
                try:
                    raw_fail = (label.inner_text(timeout=500) or "").strip()
                except Exception:
                    raw_fail = str(date_text)
                print(
                    f"  DOM locator card climb miss raw={raw_fail[:200]!r}",
                    flush=True,
                )
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
            try:
                card_text = (card.inner_text(timeout=1_500) or "").strip()
            except Exception:
                card_text = str(date_text)
            title = vehicle_title_from_card_text(card_text)
            real_date = extract_listed_on_snip(card_text)
            if real_date and not _CLICKS_META_RE.search(real_date):
                date_text = real_date
            elif isinstance(date_text, str) and _CLICKS_META_RE.search(date_text):
                date_text = ""
            key = item_id or f"{title}|{date_text}|{card_text[:60]}"
            if not key or key in seen:
                continue
            seen.add(key)
            if not title:
                print(
                    f"  DOM locator parse fail (no title) "
                    f"label={date_text!r} raw={card_text[:240]!r}",
                    flush=True,
                )
                continue
            blob = f"{title} {date_text} {card_text}".strip()
            out.append(
                {
                    "item_id": item_id,
                    "href": href
                    or (
                        f"https://www.facebook.com/marketplace/item/{item_id}/"
                        if item_id
                        else ""
                    ),
                    "blob": blob[:900],
                    "date_text": str(date_text or ""),
                    "title": title,
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
        raw_id = str(row.get("item_id") or "").strip()
        href = str(row.get("href") or "").strip()
        blob = str(row.get("blob") or "").strip()
        date_text = str(row.get("date_text") or "").strip()
        title = str(row.get("title") or "").strip() or vehicle_title_from_card_text(
            blob
        )
        if date_text and date_text not in blob:
            blob = f"{date_text} {blob}".strip()
        if title and title not in blob:
            blob = f"{title} {blob}".strip()
        if raw_id.isdigit() and len(raw_id) >= 8:
            item_id = raw_id
            if not href:
                href = f"https://www.facebook.com/marketplace/item/{item_id}/"
        else:
            # Spanish Seller Hub: date + title, often no /marketplace/item/ href.
            if not (title or date_text or blob):
                return
            digest = hashlib.sha1(
                f"{title}|{date_text}|{blob[:96]}".encode("utf-8", "replace")
            ).hexdigest()[:16]
            item_id = f"dom-{digest}"
            # Drop synthetic if same title already ingested (numeric or prior).
            if title:
                for prev in found.values():
                    if title in (prev.card_text or ""):
                        return
        if item_id in found:
            return
        age = parse_listing_age_days(blob) or parse_listing_age_days(date_text)
        age_snip = extract_listed_on_snip(blob) or date_text
        found[item_id] = ShelfListing(
            item_id=item_id,
            href=href
            or (
                f"https://www.facebook.com/marketplace/item/{item_id}/"
                if item_id.isdigit()
                else ""
            ),
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
        # account_2: blocked duplicates under ``Requieren atención`` (no Listed-on).
        for row in extract_requieren_atencion_cards(page):
            _ingest(row)

        stagnant = 0
        for _ in range(max_scrolls):
            before = len(found)
            for row in _extract_selling_cards_via_js(page):
                _ingest(row)
            for row in _extract_selling_cards_via_text_locators(page):
                _ingest(row)
            for row in extract_requieren_atencion_cards(page):
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
# Literal strings for get_by_text / XPath (menu + banner) — prefer these over
# global role scans that can hit Seller Hub header chrome.
_DELETE_TEXT_LITERALS = (
    "Delete listing",
    "Eliminar publicación",
    "Delete this listing",
    "Eliminar esta publicación",
    "Delete and try again",
    "Elimínala y vuelve a intentarlo",
    "Elimínalo y vuelve a intentarlo",
    "Delete",
    "Eliminar",
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


def _wait_confirm_delete_modal(page: Page, *, timeout_ms: int = 5_000) -> bool:
    """Wait for secondary Delete/Eliminar modal, click it, wait until hidden."""
    from src.facebook.remover import _confirm_if_needed

    dialog_sel = '[role="dialog"], [role="alertdialog"]'
    try:
        page.locator(dialog_sel).first.wait_for(
            state="visible", timeout=max(1_500, timeout_ms)
        )
    except Exception:
        page.wait_for_timeout(500)
    confirmed = bool(_confirm_if_needed(page, prefer_delete=True))
    # Second attempt — attention confirm can lag the first click.
    if not confirmed:
        page.wait_for_timeout(700)
        confirmed = bool(_confirm_if_needed(page, prefer_delete=True))
    try:
        page.locator(dialog_sel).first.wait_for(state="hidden", timeout=5_000)
    except Exception:
        page.wait_for_timeout(1_500)
    _dom_settle(page, lo_ms=1_200, hi_ms=2_000)
    return confirmed


def _attention_title_still_visible(page: Page, title: str) -> bool:
    """True when the attention-card title text is still on /you/selling."""
    title = re.sub(r"\s+", " ", (title or "").strip())
    if len(title) < 4:
        return False
    try:
        loc = page.get_by_text(re.compile(re.escape(title), re.I))
        n = loc.count()
        if not isinstance(n, int) or n <= 0:
            return False
        for i in range(min(n, 6)):
            try:
                if loc.nth(i).is_visible():
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _wait_attention_title_gone(page: Page, title: str, *, timeout_ms: int = 6_000) -> bool:
    """Wait until the titled attention card disappears from the DOM."""
    title = re.sub(r"\s+", " ", (title or "").strip())
    if len(title) < 4:
        return True
    try:
        page.get_by_text(re.compile(re.escape(title), re.I)).first.wait_for(
            state="hidden", timeout=timeout_ms
        )
        return True
    except Exception:
        pass
    # Soft poll — FB sometimes keeps a stale text node briefly.
    deadline_ms = max(1_000, timeout_ms)
    steps = max(2, deadline_ms // 400)
    for _ in range(steps):
        if not _attention_title_still_visible(page, title):
            return True
        try:
            page.wait_for_timeout(400)
        except Exception:
            break
    return not _attention_title_still_visible(page, title)


def _click_delete_text_direct(page: Page, *, scope=None) -> bool:
    """Click Delete listing / Eliminar publicación / banner CTA via text+XPath.

    Prefer exact literals and XPath text nodes over global role scans that can
    hit Seller Hub header chrome instead of the open menu / attention card.
    """
    root = scope if scope is not None else page
    for label in _DELETE_TEXT_LITERALS:
        # 1) Playwright text (menu items often render as plain span text).
        for exact in (True, False):
            try:
                loc = root.get_by_text(label, exact=exact)
                n = loc.count()
            except Exception:
                continue
            if not isinstance(n, int) or n <= 0:
                continue
            for i in range(min(n, 8)):
                item = loc.nth(i)
                try:
                    if not item.is_visible():
                        continue
                    if _force_click(item, timeout=6_000):
                        print(
                            f"  Requieren atención: clicked text={label!r} "
                            f"exact={exact}",
                            flush=True,
                        )
                        return True
                except Exception:
                    continue
        # 2) XPath — menu/banner may not expose role=menuitem.
        try:
            xpath = (
                f"xpath=//*[self::span or self::div or self::a or self::button]"
                f"[contains(normalize-space(.), {label!r})]"
            )
            loc = root.locator(xpath)
            n = loc.count()
        except Exception:
            continue
        if not isinstance(n, int) or n <= 0:
            continue
        for i in range(min(n, 8)):
            item = loc.nth(i)
            try:
                if not item.is_visible():
                    continue
                txt = re.sub(r"\s+", " ", (item.inner_text(timeout=800) or "")).strip()
                # Avoid clicking a huge wrapper that merely contains the label.
                if len(txt) > 120 and label.lower() not in txt.lower()[:80]:
                    continue
                if _force_click(item, timeout=6_000):
                    print(
                        f"  Requieren atención: clicked xpath text={label!r}",
                        flush=True,
                    )
                    return True
            except Exception:
                continue
    return False


def _locate_attention_card_root(page: Page, title: str, *, timeout_ms: int = 5_000):
    """Return a Locator for the card wrapper under Requieren atención for ``title``.

    Prefer a container that owns both the vehicle title and a ⋯ / banner CTA —
    never the global Seller Hub header. Marks the node with a data attribute so
    Playwright can scope subsequent clicks to that card only.
    """
    title = re.sub(r"\s+", " ", (title or "").strip())
    if len(title) < 4:
        return None
    token = f"attn-{hashlib.sha1(title.encode('utf-8', 'replace')).hexdigest()[:12]}"
    try:
        found = page.evaluate(
            r"""({ title, token }) => {
              document.querySelectorAll('[data-autosell-attention-card]')
                .forEach((el) => el.removeAttribute('data-autosell-attention-card'));
              const TITLE = (title || '').replace(/\s+/g, ' ').trim().toLowerCase();
              const SECTION_RE =
                /requieren\s+atenci[oó]n|requires?\s+attention|needs?\s+attention/i;
              const BANNER_RE =
                /elim[ií]nala\s+y\s+vuelve|delete\s+and\s+try\s+again|parece\s+un\s+duplicado|looks\s+like\s+a\s+duplicate/i;
              const MORE_RE = /m[aá]s opciones|more options|options|opciones/i;
              const root = document.querySelector('[role="main"]') || document.body;
              const norm = (t) => (t || '').replace(/\s+/g, ' ').trim();
              const isVis = (el) => {
                if (!el || !el.getBoundingClientRect) return false;
                const r = el.getBoundingClientRect();
                return r.width > 2 && r.height > 2;
              };
              const candidates = [];
              const walker = document.createTreeWalker(
                root, NodeFilter.SHOW_ELEMENT, null
              );
              let node = walker.currentNode;
              while (node) {
                if (node.nodeType === 1 && isVis(node)) {
                  const own = norm(node.innerText || '');
                  if (own.toLowerCase().includes(TITLE)
                      && own.length < 2500
                      && own.length > TITLE.length) {
                    candidates.push(node);
                  }
                }
                node = walker.nextNode();
              }
              candidates.sort(
                (a, b) => norm(a.innerText).length - norm(b.innerText).length
              );
              for (const el of candidates) {
                let n = el;
                for (let d = 0; d < 10 && n && n !== root; d++) {
                  const txt = norm(n.innerText || '');
                  const hasMore = !!(
                    n.querySelector
                    && (
                      n.querySelector('[aria-haspopup="menu"]')
                      || Array.from(
                        n.querySelectorAll('[aria-label], [role="button"]')
                      ).some((b) => MORE_RE.test(
                        (b.getAttribute('aria-label') || b.innerText || '')
                      ))
                    )
                  );
                  const hasBanner = BANNER_RE.test(txt);
                  if ((hasMore || hasBanner)
                      && txt.toLowerCase().includes(TITLE)
                      && txt.length < 4000) {
                    if (txt.length > 3500 && !hasBanner) {
                      n = n.parentElement;
                      continue;
                    }
                    n.setAttribute('data-autosell-attention-card', token);
                    return true;
                  }
                  n = n.parentElement;
                }
              }
              return false;
            }""",
            {"title": title, "token": token},
        )
    except Exception as exc:
        print(
            f"  Requieren atención: card-root JS miss title={title!r}: {exc}",
            flush=True,
        )
        found = False
    if found:
        loc = page.locator(f'[data-autosell-attention-card="{token}"]').first
        try:
            if loc.count() > 0:
                return loc
        except Exception:
            pass
    # Fallback: title text → nearest ancestor with menu/button.
    try:
        title_loc = page.get_by_text(re.compile(re.escape(title), re.I)).first
        if title_loc.count() <= 0:
            return None
        title_loc.scroll_into_view_if_needed(timeout=timeout_ms)
        for xpath in (
            "xpath=ancestor::div[.//*[@aria-haspopup='menu']][1]",
            "xpath=ancestor::div[.//button[@aria-label]][1]",
            "xpath=ancestor::div[5]",
            "xpath=ancestor::div[4]",
        ):
            cand = title_loc.locator(xpath).first
            if cand.count() > 0:
                return cand
    except Exception as exc:
        print(
            f"  Requieren atención: card-root xpath miss title={title!r}: {exc}",
            flush=True,
        )
    return None


def _href_from_attention_card(card) -> tuple[str, str]:
    """Return ``(item_id, href)`` scraped from a card locator/element."""
    if card is None:
        return "", ""
    try:
        info = card.evaluate(
            """(el) => {
              const ID_RE = /\\/marketplace\\/item\\/(\\d{8,})/;
              const anchors = el.querySelectorAll
                ? el.querySelectorAll('a[href], [href]')
                : [];
              for (const a of anchors) {
                const h = a.href || a.getAttribute('href') || '';
                const m = h.match(ID_RE);
                if (m) return { item_id: m[1], href: h };
              }
              return { item_id: '', href: '' };
            }"""
        )
        if isinstance(info, dict):
            return str(info.get("item_id") or ""), str(info.get("href") or "")
    except Exception:
        pass
    return "", ""


_CARD_ACTION_SKIP = (
    "boost",
    "promote",
    "share",
    "compartir",
    "account",
    "cuenta",
    "messenger",
    "notification",
    "notificaci",
    "message",
    "mensaje",
    "chat",
    "like",
    "me gusta",
    "comment",
    "comentario",
    "follow",
    "seguir",
)


def _card_control_label(btn) -> str:
    try:
        parts = [
            btn.get_attribute("aria-label") or "",
            btn.get_attribute("title") or "",
            btn.inner_text(timeout=600) or "",
        ]
        return re.sub(r"\s+", " ", " ".join(parts)).strip().lower()
    except Exception:
        return ""


def _click_card_direct_delete(page: Page, card, *, timeout_ms: int = 6_000) -> bool:
    """Click Delete listing / Eliminar / banner CTA **inside the card** (no ⋯).

    Duplicate / Needs-attention cards often expose the delete action as a plain
    button or text link without a three-dots menu.
    """
    if card is None:
        return False
    try:
        card.scroll_into_view_if_needed(timeout=min(timeout_ms, 4_000))
        card.hover(timeout=timeout_ms)
    except Exception:
        pass
    # 1) Banner / delete literals scoped to the card.
    if _click_delete_text_direct(page, scope=card):
        _dom_settle(page, lo_ms=600, hi_ms=1_000)
        _wait_confirm_delete_modal(page)
        return True
    if _click_attention_banner_text(page, scope=card):
        _dom_settle(page, lo_ms=600, hi_ms=1_000)
        _wait_confirm_delete_modal(page)
        return True
    # 2) aria-label / role buttons that already say Delete / Eliminar.
    for pattern in (_MENU_DELETE_LABEL, _ATTENTION_DELETE_CTA):
        if _click_by_bilingual_label(page, pattern, scope=card):
            _dom_settle(page, lo_ms=600, hi_ms=1_000)
            _wait_confirm_delete_modal(page)
            return True
    # 3) Any visible button/link whose accessible name matches delete.
    try:
        buttons = card.locator(
            'button, [role="button"], a, [role="link"], div[tabindex="0"]'
        )
        n = buttons.count()
    except Exception:
        n = 0
    if isinstance(n, int) and n > 0:
        for i in range(min(n, 20)):
            btn = buttons.nth(i)
            try:
                if not btn.is_visible():
                    continue
                label = _card_control_label(btn)
                if not label or len(label) > 80:
                    continue
                if any(x in label for x in _CARD_ACTION_SKIP):
                    continue
                if not (
                    _MENU_DELETE_LABEL.search(label)
                    or _ATTENTION_DELETE_CTA.search(label)
                    or "delete" in label
                    or "eliminar" in label
                    or "borrar" in label
                ):
                    continue
                if _force_click(btn, timeout=timeout_ms):
                    print(
                        f"  Requieren atención: direct card delete "
                        f"label={label[:60]!r}",
                        flush=True,
                    )
                    _dom_settle(page, lo_ms=600, hi_ms=1_000)
                    _wait_confirm_delete_modal(page)
                    return True
            except Exception:
                continue
    return False


def _click_card_action_bar_more(page: Page, card, *, timeout_ms: int = 6_000) -> bool:
    """Click the horizontal ⋯ on the card action bar (next to Share / Compartir).

    Needs-attention cards put Share + ⋯ in a bottom button group. Prefer:
      • ``[aria-label*="More"]`` / ``[aria-label*="Más"]`` inside that bar
      • small icon button containing an SVG, to the right of Share/Compartir
    """
    if card is None:
        return False
    try:
        card.scroll_into_view_if_needed(timeout=min(timeout_ms, 4_000))
        card.hover(timeout=timeout_ms)
    except Exception:
        pass

    # 1) Explicit More/Más aria inside the card (action-bar ⋯).
    more_selectors = (
        '[aria-label*="More options" i]',
        '[aria-label*="Más opciones" i]',
        '[aria-label*="See more" i]',
        '[aria-label*="Ver más" i]',
        '[aria-label^="More" i]',
        '[aria-label^="Más" i]',
        '[aria-label*="More" i]',
        '[aria-label*="Más" i]',
        '[aria-haspopup="menu"]',
    )
    for sel in more_selectors:
        try:
            loc = card.locator(sel)
            n = loc.count()
        except Exception:
            continue
        if not isinstance(n, int) or n <= 0:
            continue
        # Prefer the rightmost match (⋯ sits right of Share).
        for i in range(min(n, 8) - 1, -1, -1):
            btn = loc.nth(i)
            try:
                if not btn.is_visible():
                    continue
                label = _card_control_label(btn)
                if any(x in label for x in _CARD_ACTION_SKIP):
                    continue
                if label and (
                    _MENU_DELETE_LABEL.search(label)
                    or _ATTENTION_DELETE_CTA.search(label)
                ):
                    continue
                # Skip huge "Más información" style links.
                if "inform" in label or "information" in label or "about" in label:
                    continue
                if _force_click(btn, timeout=timeout_ms):
                    print(
                        f"  Requieren atención: opened action-bar ⋯ "
                        f"sel={sel!r} aria={label[:60]!r}",
                        flush=True,
                    )
                    return True
            except Exception as exc:
                print(
                    f"  Requieren atención: action-bar ⋯ fail sel={sel!r}: {exc}",
                    flush=True,
                )
                continue

    # 2) JS: find Share/Compartir in the bottom action bar, click ⋯ to its right.
    try:
        clicked = card.evaluate(
            """(el) => {
              const SKIP = /boost|promote|account|cuenta|messenger|notification|message|mensaje|like|comment|follow|seguir/i;
              const SHARE = /^(share|compartir)$|\\bshare\\b|\\bcompartir\\b/i;
              const MORE = /more options|m[aá]s opciones|see more|ver m[aá]s|^more$|^m[aá]s$|options|opciones|acciones|actions/i;
              const controls = Array.from(
                el.querySelectorAll(
                  'button, [role="button"], [aria-haspopup="menu"], div[tabindex="0"], span[role="button"], a[role="button"]'
                )
              ).filter((n) => {
                const r = n.getBoundingClientRect();
                return r.width >= 6 && r.height >= 6 && r.width <= 160 && r.height <= 80;
              });
              if (!controls.length) return '';

              const labelOf = (n) => (
                (n.getAttribute('aria-label') || '') + ' ' +
                (n.getAttribute('title') || '') + ' ' +
                (n.innerText || '')
              ).replace(/\\s+/g, ' ').trim();

              // Bottom-most action row: controls near the card bottom.
              const cardBox = el.getBoundingClientRect();
              const bottomBand = controls.filter((n) => {
                const r = n.getBoundingClientRect();
                return r.top >= cardBox.top + cardBox.height * 0.45;
              });
              const pool = bottomBand.length ? bottomBand : controls;

              // Prefer a More/Más control in that band.
              const moreHits = pool.filter((n) => {
                const lab = labelOf(n);
                if (SKIP.test(lab) || SHARE.test(lab)) return false;
                if (MORE.test(lab)) return true;
                // Icon-only ⋯: tiny square with SVG, empty/short text.
                const hasSvg = !!n.querySelector('svg');
                const r = n.getBoundingClientRect();
                return hasSvg && r.width <= 56 && r.height <= 56 && lab.length < 24;
              });
              moreHits.sort((a, b) =>
                b.getBoundingClientRect().right - a.getBoundingClientRect().right
              );
              let target = moreHits[0] || null;

              // Else: control immediately to the right of Share/Compartir.
              if (!target) {
                const share = pool.find((n) => SHARE.test(labelOf(n)));
                if (share) {
                  const sr = share.getBoundingClientRect();
                  const rightOf = pool
                    .filter((n) => {
                      if (n === share) return false;
                      const lab = labelOf(n);
                      if (SKIP.test(lab) || SHARE.test(lab)) return false;
                      const r = n.getBoundingClientRect();
                      return r.left >= sr.right - 8
                        && Math.abs(r.top - sr.top) < 40;
                    })
                    .sort((a, b) =>
                      a.getBoundingClientRect().left - b.getBoundingClientRect().left
                    );
                  target = rightOf[0] || null;
                }
              }

              // Last: rightmost small icon in the bottom band.
              if (!target) {
                const icons = pool
                  .filter((n) => {
                    const lab = labelOf(n);
                    if (SKIP.test(lab) || SHARE.test(lab)) return false;
                    const r = n.getBoundingClientRect();
                    return r.width <= 56 && r.height <= 56;
                  })
                  .sort((a, b) =>
                    b.getBoundingClientRect().right - a.getBoundingClientRect().right
                  );
                target = icons[0] || null;
              }

              if (!target) return '';
              target.click();
              return (labelOf(target) || '⋯').slice(0, 60);
            }"""
        )
        if clicked:
            print(
                f"  Requieren atención: opened action-bar ⋯ via JS "
                f"label={clicked!r}",
                flush=True,
            )
            return True
    except Exception as exc:
        print(
            f"  Requieren atención: action-bar ⋯ JS miss: {exc}",
            flush=True,
        )

    print(
        "  Requieren atención: no action-bar ⋯ (next to Share/Compartir)",
        flush=True,
    )
    return False


def _wait_dropdown_then_click_delete(page: Page, *, timeout_ms: int = 5_000) -> bool:
    """After ⋯ click: wait for dropdown, then Delete listing / Eliminar publicación."""
    # Wait for menu portal / menuitem to appear.
    try:
        page.locator(
            '[role="menu"], [role="menuitem"], [role="dialog"]'
        ).first.wait_for(state="visible", timeout=timeout_ms)
    except Exception:
        page.wait_for_timeout(800)
    if _click_menu_delete_bilingual(page):
        return True
    if _click_delete_text_direct(page):
        _wait_confirm_delete_modal(page)
        return True
    return False


def _click_card_scoped_more(page: Page, card, *, timeout_ms: int = 6_000) -> bool:
    """Click the Needs-attention card action-bar ⋯ (next to Share), else fallbacks."""
    if card is None:
        return False
    if _click_card_action_bar_more(page, card, timeout_ms=timeout_ms):
        return True

    # Legacy fallbacks: title-scoped Más opciones / haspopup elsewhere on card.
    try:
        card.scroll_into_view_if_needed(timeout=min(timeout_ms, 4_000))
        card.hover(timeout=timeout_ms)
    except Exception:
        pass
    selectors = (
        '[aria-label*="Más opciones para" i]',
        '[aria-label*="More options for" i]',
        '[aria-label*="Administrar" i]',
        '[aria-label*="Manage" i]',
        '[aria-haspopup="menu"]',
    )
    for sel in selectors:
        try:
            loc = card.locator(sel)
            n = loc.count()
        except Exception:
            continue
        if not isinstance(n, int) or n <= 0:
            continue
        for i in range(min(n, 8)):
            btn = loc.nth(i)
            try:
                if not btn.is_visible():
                    continue
                label = _card_control_label(btn)
                if any(x in label for x in _CARD_ACTION_SKIP):
                    continue
                if label and (
                    _MENU_DELETE_LABEL.search(label)
                    or _ATTENTION_DELETE_CTA.search(label)
                ):
                    continue
                if _force_click(btn, timeout=timeout_ms):
                    print(
                        f"  Requieren atención: opened card menu fallback "
                        f"sel={sel!r} aria={label[:60]!r}",
                        flush=True,
                    )
                    return True
            except Exception:
                continue

    print(
        "  Requieren atención: no interactive action/menu inside card wrapper",
        flush=True,
    )
    return False


def _click_attention_banner_text(page: Page, *, scope=None) -> bool:
    """Click only the red duplicate banner CTA (not generic Delete/Eliminar)."""
    root = scope if scope is not None else page
    for label in (
        "Elimínala y vuelve a intentarlo",
        "Elimínalo y vuelve a intentarlo",
        "Delete and try again",
        "Eliminar y volver a intentar",
    ):
        for exact in (False, True):
            try:
                loc = root.get_by_text(label, exact=exact)
                n = loc.count()
            except Exception:
                continue
            if not isinstance(n, int) or n <= 0:
                continue
            for i in range(min(n, 6)):
                item = loc.nth(i)
                try:
                    if not item.is_visible():
                        continue
                    if _force_click(item, timeout=6_000):
                        print(
                            f"  Requieren atención: clicked banner CTA={label!r}",
                            flush=True,
                        )
                        return True
                except Exception:
                    continue
    return False


def _click_attention_delete_cta(page: Page, *, scope=None) -> bool:
    """Needs attention / Duplicates: 'Delete and try again' / 'Elimínala…'."""
    if _click_attention_banner_text(page, scope=scope):
        _dom_settle(page, lo_ms=800, hi_ms=1_200)
        _wait_confirm_delete_modal(page)
        return True
    if not _click_by_bilingual_label(page, _ATTENTION_DELETE_CTA, scope=scope):
        return False
    _dom_settle(page, lo_ms=800, hi_ms=1_200)
    _wait_confirm_delete_modal(page)
    return True


def _click_menu_delete_bilingual(page: Page) -> bool:
    """Menu: Eliminar publicación / Eliminar / Delete listing / Delete + confirm."""
    # Direct text first — role=menuitem is often missing on attention menus.
    if _click_delete_text_direct(page):
        _dom_settle(page, lo_ms=600, hi_ms=1_000)
        _wait_confirm_delete_modal(page)
        return True
    if _click_by_bilingual_label(page, _MENU_DELETE_LABEL):
        _dom_settle(page, lo_ms=600, hi_ms=1_000)
        _wait_confirm_delete_modal(page)
        return True
    # Shared remover patterns (also EN+ES, includes attention CTA).
    if _click_menu_action(page, action="delete"):
        _dom_settle(page, lo_ms=600, hi_ms=1_000)
        _wait_confirm_delete_modal(page)
        return True
    print(
        "  Requieren atención: Delete/Eliminar text not found after ⋯ open",
        flush=True,
    )
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


_REQUIEREN_ATENCION_MORE_SEL = (
    '[aria-label="More options"], [aria-label="Más opciones"], '
    '[aria-label="More"], [aria-label="Más"], '
    '[aria-label*="More options" i], [aria-label*="Más opciones" i], '
    '[aria-label*="More options for" i], [aria-label*="Más opciones para" i], '
    '[aria-label*="Options" i], [aria-label*="Opciones" i], '
    '[aria-haspopup="menu"]'
)


def extract_requieren_atencion_cards(page: Page) -> list[dict[str, str]]:
    """Parse title cards inside the ``Requieren atención`` / Requires attention block.

    account_2 (ES) parks GraphQL-stuck duplicates under that Seller Hub section
    with banner ``No se publicó porque parece un duplicado…``. Standard
    Listed-on discovery misses these; GraphQL delete may return OK while the
    card stays stuck here.
    """
    try:
        raw = page.evaluate(
            r"""() => {
              const SECTION_RE =
                /requieren\s+atenci[oó]n|requires?\s+attention|needs?\s+attention|necesitan\s+atenci[oó]n/i;
              const BANNER_RE =
                /no\s+se\s+public[oó]\s+porque\s+parece\s+un\s+duplicado|parece\s+un\s+duplicado|elim[ií]nala\s+y\s+vuelve\s+a\s+intentarlo|this\s+looks\s+like\s+a\s+duplicate|we\s+didn'?t\s+post\s+it|delete\s+(?:it\s+and\s+)?try\s+again/i;
              const TITLE_RE =
                /\b((?:19|20)\d{2}\s+[A-Za-zÁÉÍÓÚÑáéíóúñ][A-Za-zÁÉÍÓÚÑáéíóúñ0-9\-\s]{2,60})/;
              const ID_RE = /\/marketplace\/item\/(\d{8,})/;
              const root = document.querySelector('[role="main"]') || document.body;
              const out = [];
              const seen = new Set();

              const norm = (t) => (t || '').replace(/\s+/g, ' ').trim();
              const isVisible = (el) => {
                if (!el || !el.getBoundingClientRect) return false;
                const r = el.getBoundingClientRect();
                return r.width > 2 && r.height > 2;
              };

              // Find the section heading node, then climb to its content container.
              let heading = null;
              const walk = document.createTreeWalker(
                root, NodeFilter.SHOW_ELEMENT, null
              );
              let node = walk.currentNode;
              while (node) {
                if (node.nodeType === 1) {
                  const own = Array.from(node.childNodes || [])
                    .filter((c) => c.nodeType === 3)
                    .map((c) => (c.textContent || '').trim())
                    .join(' ');
                  const direct = own || (
                    (node.children && node.children.length === 0)
                      ? (node.textContent || '').trim()
                      : ''
                  );
                  const short = norm(direct || '');
                  if (short && short.length < 80 && SECTION_RE.test(short)
                      && isVisible(node)) {
                    heading = node;
                    break;
                  }
                  // Heading roles / aria often carry the section label.
                  const role = (node.getAttribute && node.getAttribute('role')) || '';
                  const al = (node.getAttribute && node.getAttribute('aria-label')) || '';
                  if ((role === 'heading' || /^h[1-6]$/i.test(node.tagName || ''))
                      && SECTION_RE.test(norm(node.innerText || al))
                      && isVisible(node)) {
                    heading = node;
                    break;
                  }
                }
                node = walk.nextNode();
              }

              let section = null;
              if (heading) {
                let n = heading;
                for (let d = 0; d < 10 && n; d++) {
                  const parent = n.parentElement;
                  if (!parent || parent === root) break;
                  const txt = norm(parent.innerText || '');
                  // Prefer the smallest wrapper that holds the heading + at least
                  // one duplicate banner / titled card (not the whole Seller Hub).
                  if (txt.length > 40 && txt.length < 12000
                      && (BANNER_RE.test(txt) || TITLE_RE.test(txt))) {
                    section = parent;
                    if (txt.length < 4000) break;
                  }
                  n = parent;
                }
              }
              // Fallback: any main subtree that contains the duplicate banner.
              if (!section) {
                const banners = [];
                root.querySelectorAll('span, div, p, button').forEach((el) => {
                  const t = norm(el.innerText || el.textContent || '');
                  if (t && t.length < 220 && BANNER_RE.test(t) && isVisible(el)) {
                    banners.push(el);
                  }
                });
                if (banners.length) {
                  let n = banners[0];
                  for (let d = 0; d < 12 && n; d++) {
                    n = n.parentElement;
                    if (!n || n === root) break;
                    const txt = norm(n.innerText || '');
                    if (txt.length > 40 && txt.length < 12000
                        && TITLE_RE.test(txt)) {
                      section = n;
                      if (SECTION_RE.test(txt) || txt.length < 5000) break;
                    }
                  }
                }
              }
              if (!section) {
                return { cards: out, section_found: false, heading: '' };
              }

              const pushCard = (cardEl) => {
                if (!cardEl || !isVisible(cardEl)) return;
                const cardText = norm(cardEl.innerText || '');
                if (cardText.length < 8 || cardText.length > 2500) return;
                if (!TITLE_RE.test(cardText) && !BANNER_RE.test(cardText)) return;
                const titleMatch = cardText.match(TITLE_RE);
                const title = titleMatch
                  ? titleMatch[1].replace(/\s+/g, ' ').trim().slice(0, 80)
                  : '';
                if (!title) return;
                let item_id = '';
                let href = '';
                const anchors = cardEl.querySelectorAll
                  ? cardEl.querySelectorAll('a[href], [href]')
                  : [];
                for (const a of anchors) {
                  const h = a.href || a.getAttribute('href') || '';
                  const m = h.match(ID_RE);
                  if (m) {
                    item_id = m[1];
                    href = h;
                    break;
                  }
                }
                const key = item_id || title.toLowerCase();
                if (seen.has(key)) return;
                seen.add(key);
                out.push({
                  item_id: item_id || '',
                  href: href || '',
                  title,
                  blob: cardText.slice(0, 900),
                  has_duplicate_banner: BANNER_RE.test(cardText) ? '1' : '0',
                  in_attention_section: '1',
                });
              };

              // Prefer discrete cards: wrappers with title + more-menu / banner.
              const candidates = [];
              section.querySelectorAll('div, li, article').forEach((el) => {
                if (!isVisible(el)) return;
                const t = norm(el.innerText || '');
                if (t.length < 12 || t.length > 1800) return;
                if (!TITLE_RE.test(t)) return;
                const hasChrome = !!(
                  el.querySelector('[aria-haspopup="menu"]')
                  || el.querySelector('[aria-label*="Más"]')
                  || el.querySelector('[aria-label*="More"]')
                  || el.querySelector('img')
                  || BANNER_RE.test(t)
                );
                if (!hasChrome) return;
                candidates.push(el);
              });
              // Smallest-first so nested wrappers don't swallow siblings.
              candidates.sort((a, b) =>
                norm(a.innerText || '').length - norm(b.innerText || '').length
              );
              for (const el of candidates) {
                // Skip if an already-pushed ancestor would duplicate.
                let skip = false;
                for (const prev of out) {
                  if (prev.title && norm(el.innerText || '').includes(prev.title)
                      && norm(el.innerText || '').length
                        > (prev.blob || '').length + 40) {
                    // larger wrapper containing a prior card — skip
                    skip = true;
                    break;
                  }
                }
                if (!skip) pushCard(el);
              }
              // Banner-anchored climb when structure is flat.
              if (!out.length) {
                section.querySelectorAll('span, div, p, button').forEach((el) => {
                  const t = norm(el.innerText || el.textContent || '');
                  if (!(t && t.length < 220 && BANNER_RE.test(t))) return;
                  let n = el;
                  for (let d = 0; d < 8 && n; d++) {
                    n = n.parentElement;
                    if (!n || n === section) break;
                    const txt = norm(n.innerText || '');
                    if (TITLE_RE.test(txt) && txt.length < 2000) {
                      pushCard(n);
                      break;
                    }
                  }
                });
              }
              return {
                cards: out,
                section_found: true,
                heading: norm(heading ? (heading.innerText || '') : 'Requieren atención'),
              };
            }"""
        )
    except Exception as exc:
        print(f"  WARNING: Requieren atención extract failed: {exc}", flush=True)
        return []

    if not isinstance(raw, dict):
        return []
    section_found = bool(raw.get("section_found"))
    heading = str(raw.get("heading") or "").strip()
    rows = list(raw.get("cards") or [])
    if section_found:
        print(
            f"  Requieren atención section found"
            + (f" ({heading!r})" if heading else "")
            + f" — {len(rows)} title card(s)",
            flush=True,
        )
    elif rows:
        print(
            f"  Requieren atención: {len(rows)} duplicate-banner card(s) "
            f"(section header not labeled)",
            flush=True,
        )
    cleaned: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip() or vehicle_title_from_card_text(
            str(row.get("blob") or "")
        )
        if not title:
            continue
        cleaned.append(
            {
                "item_id": str(row.get("item_id") or "").strip(),
                "href": str(row.get("href") or "").strip(),
                "title": title,
                "blob": str(row.get("blob") or title)[:900],
                "has_duplicate_banner": str(row.get("has_duplicate_banner") or "0"),
                "in_attention_section": "1",
                "date_text": "",
            }
        )
    return cleaned


def _title_matches_needles(
    title: str,
    needles: list[str],
    *,
    require_year: bool = False,
) -> bool:
    """True when ``title`` fuzzy-matches any purge-queue needle."""
    from src.facebook.remover import _normalize_title_needles

    hay = re.sub(r"\s+", " ", (title or "").strip().lower())
    if not hay:
        return False
    hay_year = re.search(r"\b((?:19|20)\d{2})\b", hay)
    for needle in _normalize_title_needles(needles, require_year=require_year):
        n = re.sub(r"\s+", " ", (needle or "").strip().lower())
        if len(n) < 4:
            continue
        n_year = re.search(r"\b((?:19|20)\d{2})\b", n)
        if require_year and hay_year and n_year and hay_year.group(1) != n_year.group(1):
            continue
        if n in hay or hay in n:
            return True
        # Token overlap: year + ≥2 significant tokens.
        hay_toks = {t for t in re.split(r"[^a-z0-9áéíóúñ]+", hay) if len(t) >= 2}
        needle_toks = {t for t in re.split(r"[^a-z0-9áéíóúñ]+", n) if len(t) >= 2}
        if not needle_toks:
            continue
        overlap = hay_toks & needle_toks
        if len(overlap) >= min(2, len(needle_toks)) and (
            not require_year
            or (n_year and n_year.group(1) in overlap)
            or not n_year
        ):
            return True
    return False


def _delete_attention_via_item_url(
    page: Page,
    *,
    item_id: str = "",
    href: str = "",
    title: str = "",
) -> bool:
    """Fallback: open listing detail / edit URL and hard-delete from there."""
    from src.facebook.remover import remove_vehicle_listing

    url = (href or "").strip()
    if url and not url.startswith("http"):
        url = f"https://www.facebook.com{url}"
    if not url and item_id and str(item_id).isdigit():
        url = f"https://www.facebook.com/marketplace/item/{item_id}/"
    if not url:
        return False
    try:
        print(
            f"  Requieren atención: detail/URL fallback title={title!r} url={url}",
            flush=True,
        )
        ok = bool(
            remove_vehicle_listing(
                page,
                url,
                autosell_id=title or item_id or "attention",
                removal_action="delete",
                log_dir=Path("data/logs/facebook/purge"),
                require_verified=False,
                skip_graphql=True,
                title_needles=[title] if title else None,
            )
        )
        if ok:
            return True
    except Exception as exc:
        print(f"  Requieren atención URL fallback fail: {exc}", flush=True)
    # Direct owner delete chrome on the detail page.
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(1_500)
        if _click_menu_delete_bilingual(page) or _click_attention_delete_cta(page):
            _wait_confirm_delete_modal(page)
            return True
        # ⋮ on detail → Delete → confirm.
        more = page.locator(_REQUIEREN_ATENCION_MORE_SEL)
        if more.count() > 0 and _force_click(more.first, timeout=5_000):
            _dom_settle(page)
            if _click_menu_delete_bilingual(page):
                return True
    except Exception as exc:
        print(f"  Requieren atención detail chrome fail: {exc}", flush=True)
    return False


def force_delete_requieren_atencion_card(
    page: Page,
    title: str,
    *,
    timeout_ms: int = 8_000,
    item_id: str = "",
    href: str = "",
) -> bool:
    """Force-delete one attention-block card (duplicate / Needs attention).

    Strategy (account_1 EN + account_2 ES):
      1. Locate the card wrapper under ``Requieren atención`` for ``title``
      2. Click red banner CTA / Delete text **inside the card** (no dropdown)
      3. Else click any card-scoped action/menu icon → Delete listing /
         Eliminar publicación via text → confirm modal
      4. On miss: scrape listing id / href and navigate to item URL, or re-click
         the red warning banner
      5. Last resort: global ``remove_from_selling_by_title``
    """
    title = re.sub(r"\s+", " ", (title or "").strip())
    if len(title) < 4:
        return False
    ms = max(2_000, int(timeout_ms))
    resolved_id = (item_id or "").strip()
    resolved_href = (href or "").strip()

    def _verified_gone(how: str) -> bool:
        if _wait_attention_title_gone(page, title, timeout_ms=6_000):
            try:
                page.goto(SELLING_URL, wait_until="domcontentloaded", timeout=90_000)
                page.wait_for_timeout(2_000)
            except Exception:
                pass
            if not _attention_title_still_visible(page, title):
                print(
                    f"  Requieren atención: deleted via {how} title={title!r}",
                    flush=True,
                )
                return True
            print(
                f"  Requieren atención: {how} clicked but title still visible "
                f"title={title!r} — trying next strategy",
                flush=True,
            )
            return False
        print(
            f"  Requieren atención: {how} clicked; title still in DOM "
            f"title={title!r} — trying next strategy",
            flush=True,
        )
        return False

    def _try_url_fallback(reason: str) -> bool:
        nonlocal resolved_id, resolved_href
        print(
            f"  Requieren atención: {reason} — URL/banner fallback "
            f"title={title!r} item_id={resolved_id!r} href={resolved_href!r}",
            flush=True,
        )
        if _delete_attention_via_item_url(
            page,
            item_id=resolved_id,
            href=resolved_href,
            title=title,
        ):
            try:
                page.goto(SELLING_URL, wait_until="domcontentloaded", timeout=90_000)
                page.wait_for_timeout(2_000)
            except Exception:
                pass
            if not _attention_title_still_visible(page, title):
                print(
                    f"  Requieren atención: deleted via item URL title={title!r}",
                    flush=True,
                )
                return True
            print(
                f"  Requieren atención: URL delete ran but title still visible "
                f"title={title!r}",
                flush=True,
            )
        return False

    try:
        page.goto(SELLING_URL, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(1_500)
    except Exception:
        pass
    try:
        hydrate_selling_shelf_scrolls(page, scrolls=2)
    except Exception:
        pass

    card = _locate_attention_card_root(page, title, timeout_ms=ms)
    if card is None:
        print(
            f"  Requieren atención: card wrapper not found title={title!r}",
            flush=True,
        )
    else:
        scraped_id, scraped_href = _href_from_attention_card(card)
        if scraped_id and not resolved_id:
            resolved_id = scraped_id
        if scraped_href and not resolved_href:
            resolved_href = scraped_href
        print(
            f"  Requieren atención: card root ok title={title!r} "
            f"item_id={resolved_id!r}",
            flush=True,
        )

        # 1) Direct Delete / banner CTA inside this card (no dropdown needed).
        try:
            if _click_card_direct_delete(page, card, timeout_ms=ms):
                if _verified_gone("card direct Delete/banner"):
                    return True
        except Exception as exc:
            print(
                f"  Requieren atención: direct delete error title={title!r}: {exc}",
                flush=True,
            )
        try:
            if _click_attention_delete_cta(page, scope=card):
                if _verified_gone("card banner CTA"):
                    return True
        except Exception as exc:
            print(
                f"  Requieren atención: banner CTA error title={title!r}: {exc}",
                flush=True,
            )

        # 2) Action-bar ⋯ (next to Share/Compartir) → wait dropdown → Delete.
        menu_opened = False
        try:
            menu_opened = _click_card_action_bar_more(
                page, card, timeout_ms=ms
            ) or _click_card_scoped_more(page, card, timeout_ms=ms)
        except Exception as exc:
            print(
                f"  Requieren atención: action-bar ⋯ open error title={title!r}: {exc}",
                flush=True,
            )
        if menu_opened:
            _dom_settle(page, lo_ms=500, hi_ms=900)
            deleted = _wait_dropdown_then_click_delete(page, timeout_ms=5_000)
            if deleted:
                # Explicit confirmation wait: card must leave the DOM.
                page.wait_for_timeout(1_200)
                if _verified_gone("action-bar ⋯ → Delete listing"):
                    return True
            print(
                f"  Requieren atención: Delete/Eliminar not clicked "
                f"after action-bar ⋯ title={title!r}",
                flush=True,
            )
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            if _click_attention_banner_text(page, scope=card) or _click_attention_banner_text(
                page
            ):
                _wait_confirm_delete_modal(page)
                if _verified_gone("banner after menu miss"):
                    return True
            if _try_url_fallback("menu Delete miss after action-bar ⋯"):
                return True
        else:
            print(
                f"  Requieren atención: no action-bar ⋯ title={title!r} "
                f"— trying banner/URL",
                flush=True,
            )
            if _click_attention_banner_text(page, scope=card) or _click_attention_banner_text(
                page
            ):
                _wait_confirm_delete_modal(page)
                if _verified_gone("banner after no action-bar ⋯"):
                    return True
            if _try_url_fallback("no action-bar ⋯ inside card"):
                return True

    # 3) Direct page-level banner text (still better than global header ⋯).
    try:
        page.goto(SELLING_URL, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(1_200)
        hydrate_selling_shelf_scrolls(page, scrolls=1)
    except Exception:
        pass
    # Scroll the title into view then click nearby banner literal.
    try:
        title_loc = page.get_by_text(re.compile(re.escape(title), re.I)).first
        if title_loc.count() > 0:
            title_loc.scroll_into_view_if_needed(timeout=ms)
            title_loc.hover(timeout=ms)
    except Exception:
        pass
    for label in (
        "Elimínala y vuelve a intentarlo",
        "Elimínalo y vuelve a intentarlo",
        "Delete and try again",
    ):
        try:
            loc = page.get_by_text(label, exact=False)
            n = loc.count()
        except Exception:
            continue
        if not isinstance(n, int) or n <= 0:
            continue
        for i in range(min(n, 6)):
            item = loc.nth(i)
            try:
                if not item.is_visible():
                    continue
                if _force_click(item, timeout=ms):
                    _wait_confirm_delete_modal(page)
                    if _verified_gone(f"banner text {label!r}"):
                        return True
            except Exception as exc:
                print(
                    f"  Requieren atención: banner text click fail "
                    f"label={label!r}: {exc}",
                    flush=True,
                )

    # 4) URL / edit navigation fallback (scrape again if needed).
    if not resolved_href and not resolved_id:
        card2 = _locate_attention_card_root(page, title, timeout_ms=ms)
        if card2 is not None:
            scraped_id, scraped_href = _href_from_attention_card(card2)
            resolved_id = resolved_id or scraped_id
            resolved_href = resolved_href or scraped_href
    if _try_url_fallback("pre-title-menu fallback"):
        return True

    # 5) Last resort: global title-menu helper (may hit wrong ⋯ — logged).
    try:
        print(
            f"  Requieren atención: last-resort remove_from_selling_by_title "
            f"title={title!r}",
            flush=True,
        )
        if remove_from_selling_by_title(
            page, [title], action="delete", require_year=True
        ):
            print(
                f"  Requieren atención: deleted via title-menu title={title!r}",
                flush=True,
            )
            return True
        print(
            f"  Requieren atención: title-menu unconfirmed title={title!r}",
            flush=True,
        )
    except Exception as exc:
        print(
            f"  Requieren atención title-menu failed title={title!r}: {exc}",
            flush=True,
        )

    if _try_url_fallback("post-title-menu fallback"):
        return True
    return False


def purge_requieren_atencion_matches(
    page: Page,
    *,
    account_id: str,
    queue_titles: list[str],
    dry_run: bool = False,
    delay_min_sec: float = 1.5,
    delay_max_sec: float = 3.0,
    also_purge_duplicate_banners: bool = True,
) -> PurgeStats:
    """Force-delete attention-block cards whose titles match the purge queue.

    GraphQL delete often returns success while account_2 cards remain under
    ``Requieren atención``. This pass uses Playwright ⋯ → Eliminar publicación.
    """
    stats = PurgeStats()
    needles = [t for t in queue_titles if t and len(str(t).strip()) >= 4]
    try:
        page.goto(SELLING_URL, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(2_000)
    except Exception as exc:
        print(
            f"  [{account_id}] Requieren atención: cannot open selling ({exc})",
            flush=True,
        )
        return stats
    try:
        wait_for_selling_shelf_cards(page, timeout_ms=8_000)
    except Exception:
        pass
    try:
        hydrate_selling_shelf_scrolls(page)
    except Exception:
        pass

    cards = extract_requieren_atencion_cards(page)
    stats.found = len(cards)
    if not cards:
        print(
            f"  [{account_id}] Requieren atención: 0 blocked card(s)",
            flush=True,
        )
        return stats

    targets: list[dict[str, str]] = []
    for card in cards:
        title = str(card.get("title") or "").strip()
        banner = str(card.get("has_duplicate_banner") or "") == "1"
        matched = bool(needles) and _title_matches_needles(title, needles)
        # Match purge-queue titles; also clear unmatched duplicate banners when
        # also_purge_duplicate_banners (GraphQL OK leaves them stuck with no id).
        if matched or (also_purge_duplicate_banners and banner):
            targets.append(card)

    # De-dupe by title.
    seen_t: set[str] = set()
    uniq: list[dict[str, str]] = []
    for card in targets:
        key = str(card.get("title") or "").strip().lower()
        if not key or key in seen_t:
            continue
        seen_t.add(key)
        uniq.append(card)
    targets = uniq

    print(
        f"  [{account_id}] Requieren atención: {stats.found} card(s), "
        f"{len(targets)} match purge queue / duplicate banner — "
        f"{'dry-run' if dry_run else 'force-deleting via ⋯'}",
        flush=True,
    )
    for card in targets:
        title = str(card.get("title") or "").strip()
        print(
            f"  [{account_id}] ATTENTION PURGE title={title!r} "
            f"banner={card.get('has_duplicate_banner')}",
            flush=True,
        )
        if dry_run:
            stats.dry_run_candidates += 1
            continue
        ok = force_delete_requieren_atencion_card(
            page,
            title,
            item_id=str(card.get("item_id") or ""),
            href=str(card.get("href") or ""),
        )
        if ok:
            stats.deleted += 1
            stats.ui_fallback_deletes += 1
            random_delay(delay_min_sec, delay_max_sec)
        else:
            stats.failed += 1
            print(
                f"  [{account_id}] ATTENTION PURGE FAIL title={title!r}",
                flush=True,
            )
    return stats


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
        title_hint = vehicle_title_from_card_text(
            card.card_text or card.age_text or ""
        )
        # sync.db fallbacks: year-lock so 2019 Porsche ≠ 2023 Porsche Cayenne.
        strict_year = used_db_fallback or bool(
            (autosell_id or "").startswith("obj")
            or bool(re.search(r"\b(?:19|20)\d{2}\b", title_hint or card.card_text or ""))
        )
        needles = _title_needles_for_purge(
            store=store,
            account_id=account_id,
            item_id=card.item_id,
            autosell_id=autosell_id,
            vehicle_title=title_hint,
            card_text=card.card_text or card.age_text or "",
            require_year=strict_year,
        )
        # Catalog needles only (no detail-page navigation mid-shelf purge).
        vehicle = catalog.get(autosell_id) if autosell_id else None
        if vehicle is not None:
            try:
                from src.facebook.remover import _expand_partial_title_needles

                merged = list(needles) + list(_match_needles(vehicle))
                needles = _expand_partial_title_needles(
                    merged, require_year=strict_year
                )
            except Exception:
                pass

        # Synthetic DOM ids (title cards from clicks-label climb) → UI title delete.
        title_only = str(card.item_id).startswith("dom-") or not str(
            card.item_id
        ).isdigit()

        # Empty shelf DOM / DB fallback / title-only: Más opciones by title.
        if used_db_fallback or title_only:
            if not needles:
                # Last resort for DB-only rows: may open detail once for title.
                raw = _title_needles_for_row(
                    store, account_id, card, catalog, page
                )
                from src.facebook.remover import _expand_partial_title_needles

                needles = _expand_partial_title_needles(
                    raw, require_year=strict_year
                )
            print(
                f"  title-menu delete needles={needles[:3]!r} "
                f"autosell_id={autosell_id} title_only={title_only} "
                f"require_year={strict_year}",
                flush=True,
            )
            if needles:
                ok = remove_from_selling_by_title(
                    page,
                    needles,
                    action="delete",
                    require_year=strict_year,
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
                    f"needles={needles[:3]!r} require_year={strict_year}",
                    flush=True,
                )
                ok = remove_from_selling_by_title(
                    page,
                    needles,
                    action="delete",
                    require_year=strict_year,
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

        # Dead sync.db row (esp. DB fallback): 404 + year-title absent → clear, no FAIL.
        if not ok and (
            used_db_fallback
            or (autosell_id or "").startswith("obj")
        ):
            if _db_listing_already_gone_on_facebook(
                page, listing_url=listing_url, needles=needles
            ):
                _mark_db_row_already_removed(
                    store,
                    account_id,
                    autosell_id=autosell_id,
                    item_id=card.item_id,
                    reason="404/gone + year-title absent on shelf",
                )
                ok = True

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
