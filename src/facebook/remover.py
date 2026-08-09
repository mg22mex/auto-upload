from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import urlparse

from playwright.sync_api import Locator, Page

from src.facebook.errors import FacebookPostingError
from src.facebook.poster import _save_debug

RemovalAction = Literal["delete", "mark_sold"]

SELLING_URL = "https://www.facebook.com/marketplace/you/selling"
DASHBOARD_URL = "https://www.facebook.com/marketplace/you/dashboard"

_ALREADY_GONE_PHRASES = (
    "isn't available",
    "is not available",
    "isn't available right now",
    "content isn't available",
    "this content isn't available",
    "this content is no longer available",
    "no longer available",
    "no está disponible",
    "no esta disponible",
    "contenido no está disponible",
    "contenido no esta disponible",
    "este contenido no está disponible",
    "this listing is unavailable",
    "listing is unavailable",
    "listing unavailable",
    "esta publicación no está disponible",
    "publicación no disponible",
    "item not available",
    "not available right now",
    "has been deleted",
    "ha sido eliminada",
    "ha sido eliminado",
    "was deleted",
    "se eliminó",
    "se elimino",
    "page isn't available",
    "la página no está disponible",
    "sorry, this content isn't available",
    "lo sentimos, este contenido no está disponible",
)

_MARK_AVAILABLE = re.compile(
    r"mark as available|marcar como disponible",
    re.I,
)
_MARK_SOLD = re.compile(
    r"mark as sold|marcar como vendido|marcar vendido|mark sold",
    re.I,
)
_DELETE_PATTERNS = (
    re.compile(
        r"delete listing|eliminar publicación|delete this listing|"
        r"eliminar esta publicación|eliminar anuncio|delete item|"
        r"eliminar artículo|remove listing",
        re.I,
    ),
    re.compile(r"^\s*delete\s*$|^\s*eliminar\s*$", re.I),
)
_MORE_MENU = re.compile(
    r"more options|more|más opciones|opciones|manage|administrar|"
    r"see more|ver más|actions|acciones",
    re.I,
)
_MORE_ARIA_SELECTORS = (
    '[aria-label="Más opciones"]',
    '[aria-label="More options"]',
    '[aria-label="Más"]',
    '[aria-label="More"]',
    '[aria-label*="Más opciones" i]',
    '[aria-label*="More options" i]',
    '[aria-label*="Opciones" i]',
    '[aria-label*="Options" i]',
)


def extract_item_id(listing_url: str) -> str | None:
    """Pull Marketplace item id from a listing URL."""
    if not listing_url:
        return None
    match = re.search(r"/item/(\d+)", listing_url)
    if match:
        return match.group(1)
    path = urlparse(listing_url).path.rstrip("/")
    tail = path.split("/")[-1] if path else ""
    return tail if tail.isdigit() else None


def remove_vehicle_listing(
    page: Page,
    listing_url: str,
    *,
    autosell_id: str,
    removal_action: str,
    log_dir: Path,
    require_verified: bool = False,
) -> bool:
    """Remove a Marketplace listing (delete or mark sold).

    Returns True when the listing is confirmed gone/sold.

    When ``require_verified`` is True (repost path), failure to remove or to
    verify removal raises ``FacebookPostingError`` and must block create.

    Strategies (in order):
    1. Open ``listing_url`` — treat 404 / unavailable as already removed
    2. Delete / mark-sold via listing detail menu (ES/EN + three-dot aria-labels)
    3. Fallback: find item on ``/marketplace/you/selling`` and delete from there
    4. Optional mark-sold if primary action was delete but only sold controls appear
    """
    action = (removal_action or "mark_sold").strip().lower()
    if action not in ("delete", "mark_sold"):
        action = "mark_sold"

    item_id = extract_item_id(listing_url)
    print(
        f"  {autosell_id}: remove start action={action} "
        f"item_id={item_id or '?'} url={listing_url}"
    )

    # --- Primary: listing detail URL ---
    try:
        page.goto(listing_url, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(2_500)
    except Exception as exc:
        print(f"  {autosell_id}: listing URL navigation failed ({exc}) — checking if gone")

    if _listing_already_gone(page, listing_url=listing_url, item_id=item_id):
        print(f"  {autosell_id}: listing already sold/unavailable — treating as removed")
        if require_verified:
            _assert_removed_or_raise(page, listing_url, autosell_id, log_dir)
        return True

    last_error: Exception | None = None
    try:
        _perform_removal_on_current_page(page, action=action)
        page.wait_for_timeout(2_000)
    except Exception as exc:
        last_error = exc
        print(f"  {autosell_id}: detail-page remove failed: {exc}")

    if _listing_already_gone(page, listing_url=listing_url, item_id=item_id):
        print(f"  {autosell_id}: listing gone after detail remove")
        if require_verified:
            _assert_removed_or_raise(page, listing_url, autosell_id, log_dir)
        return True

    # --- Fallback: selling shelf ---
    if item_id:
        try:
            print(f"  {autosell_id}: trying selling-shelf removal for item {item_id}")
            if _remove_from_selling_shelf(page, item_id, action=action):
                page.wait_for_timeout(2_000)
        except Exception as exc:
            last_error = exc
            print(f"  {autosell_id}: selling-shelf remove failed: {exc}")

    if _listing_already_gone(page, listing_url=listing_url, item_id=item_id):
        print(f"  {autosell_id}: listing gone after selling-shelf remove")
        if require_verified:
            _assert_removed_or_raise(page, listing_url, autosell_id, log_dir)
        return True

    # Detail again after opening from selling
    try:
        page.goto(listing_url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(2_000)
        if _listing_already_gone(page, listing_url=listing_url, item_id=item_id):
            if require_verified:
                _assert_removed_or_raise(page, listing_url, autosell_id, log_dir)
            return True
        # Last ditch: opposite action (delete ↔ sell) if still live
        alt = "mark_sold" if action == "delete" else "delete"
        try:
            print(f"  {autosell_id}: fallback action={alt}")
            _perform_removal_on_current_page(page, action=alt)
            page.wait_for_timeout(2_000)
        except Exception as exc:
            last_error = exc
    except Exception as exc:
        last_error = exc

    if require_verified:
        if _verify_listing_removed(page, listing_url):
            print(f"  {autosell_id}: removal verified (listing gone/sold)")
            return True
        _save_debug(page, log_dir, autosell_id, "remove_failed")
        detail = f": {last_error}" if last_error else ""
        raise FacebookPostingError(
            f"Remove failed for {autosell_id}: Delete control not found "
            f"(expected 'Eliminar publicación' / 'Delete listing') "
            f"and listing still active{detail}"
        )

    if _listing_already_gone(page, listing_url=listing_url, item_id=item_id):
        return True
    if last_error is not None:
        _save_debug(page, log_dir, autosell_id, "remove_failed")
        raise FacebookPostingError(
            f"Remove failed for {autosell_id}: {last_error}"
        ) from last_error
    return _listing_already_gone(page, listing_url=listing_url, item_id=item_id)


def _perform_removal_on_current_page(page: Page, *, action: str) -> None:
    if action == "delete":
        _delete_listing(page)
    else:
        _mark_sold(page)


def _remove_from_selling_shelf(page: Page, item_id: str, *, action: str) -> bool:
    """Locate listing on selling dashboard and delete/mark-sold from the card menu."""
    for shelf in (SELLING_URL, DASHBOARD_URL):
        page.goto(shelf, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(3_000)
        link = _find_item_link(page, item_id)
        if link is None:
            continue

        try:
            link.scroll_into_view_if_needed(timeout=5_000)
        except Exception:
            pass
        page.wait_for_timeout(500)

        # Prefer card-local ⋮ then listing open + detail remove
        if _click_more_near_locator(page, link):
            if _click_menu_action(page, action=action):
                _confirm_if_needed(page, prefer_delete=(action == "delete"))
                return True

        # Open listing from shelf and run detail removal
        try:
            link.click(timeout=8_000)
            page.wait_for_timeout(2_500)
            _perform_removal_on_current_page(page, action=action)
            return True
        except Exception:
            continue
    return False


def _find_item_link(page: Page, item_id: str) -> Locator | None:
    try:
        links = page.locator(f'a[href*="/marketplace/item/{item_id}"]')
        count = links.count()
        if count == 0:
            return None
        for i in range(min(count, 12)):
            loc = links.nth(i)
            try:
                if loc.is_visible():
                    return loc
            except Exception:
                continue
        return links.first
    except Exception:
        return None


def _click_more_near_locator(page: Page, anchor: Locator) -> bool:
    """Open a ⋮ / Más opciones control in the same card as ``anchor``."""
    # Climb to a reasonable card ancestor and look for more-buttons inside
    for xpath in (
        "xpath=ancestor::div[.//*[@aria-label='Más opciones' or "
        "@aria-label='More options' or @aria-label='Más' or "
        "@aria-label='More']][1]",
        "xpath=ancestor::div[4]",
        "xpath=ancestor::div[6]",
        "xpath=ancestor::div[8]",
    ):
        try:
            card = anchor.locator(xpath)
            if card.count() == 0:
                continue
            root = card.first
            for selector in _MORE_ARIA_SELECTORS:
                btn = root.locator(selector)
                if btn.count() == 0:
                    continue
                try:
                    target = btn.first
                    if target.is_visible():
                        target.click(timeout=5_000)
                        page.wait_for_timeout(1_200)
                        if _menu_open(page):
                            return True
                except Exception:
                    continue
            # Role/name based inside card
            named = root.get_by_role("button", name=_MORE_MENU)
            for i in range(min(named.count() or 0, 4)):
                try:
                    b = named.nth(i)
                    if b.is_visible():
                        b.click(timeout=5_000)
                        page.wait_for_timeout(1_200)
                        if _menu_open(page):
                            return True
                except Exception:
                    continue
        except Exception:
            continue
    return _open_listing_menu(page)


def _menu_open(page: Page) -> bool:
    try:
        if page.get_by_role("menuitem").count() > 0:
            return True
        if page.get_by_role("menu").count() > 0:
            return True
    except Exception:
        pass
    return False


def _assert_removed_or_raise(
    page: Page,
    listing_url: str,
    autosell_id: str,
    log_dir: Path,
) -> None:
    if _verify_listing_removed(page, listing_url):
        print(f"  {autosell_id}: removal verified (listing gone/sold)")
        return
    _save_debug(page, log_dir, autosell_id, "remove_unverified")
    raise FacebookPostingError(
        f"Remove not verified for {autosell_id}: listing still appears active "
        f"at {listing_url}. Skipping create to avoid Facebook duplicate warning."
    )


def _verify_listing_removed(page: Page, listing_url: str) -> bool:
    """Reload listing URL and selling shelf; True only when fully gone/sold."""
    page.wait_for_timeout(1_500)
    item_id = extract_item_id(listing_url)
    try:
        page.goto(listing_url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(2_500)
    except Exception:
        return True

    if _listing_already_gone(page, listing_url=listing_url, item_id=item_id):
        return True

    if item_id and not _item_visible_on_selling(page, item_id):
        # Re-check detail — if not live controls, treat as gone
        try:
            page.goto(listing_url, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(2_000)
        except Exception:
            return True
        if not _looks_like_live_listing(page):
            return True
        return _listing_already_gone(page, listing_url=listing_url, item_id=item_id)

    return False


def _looks_like_live_listing(page: Page) -> bool:
    """Heuristics: sold listings show 'Mark as available'; live ones show sold/delete."""
    try:
        if page.get_by_role("button", name=_MARK_AVAILABLE).count():
            return False
    except Exception:
        pass
    try:
        if page.get_by_role("button", name=_MARK_SOLD).count():
            return True
        if page.get_by_role("menuitem", name=_MARK_SOLD).count():
            return True
    except Exception:
        pass
    return not _listing_already_gone(page)


def _item_visible_on_selling(page: Page, item_id: str) -> bool:
    """True if item id still links from your selling dashboard."""
    try:
        page.goto(SELLING_URL, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(3_000)
    except Exception:
        return True  # fail closed

    return _find_item_link(page, item_id) is not None


def _listing_already_gone(
    page: Page,
    *,
    listing_url: str | None = None,
    item_id: str | None = None,
) -> bool:
    """True when the FB listing is already sold, deleted, or unavailable."""
    try:
        available = page.get_by_role("button", name=_MARK_AVAILABLE)
        if available.count() and available.first.is_visible():
            return True
    except Exception:
        pass

    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""

    # Redirected off the item detail (deleted / 404 shell) without login wall
    if url and "login" not in url and "checkpoint" not in url:
        on_item = bool(re.search(r"/marketplace/item/\d+", url))
        if listing_url and item_id:
            # Landed somewhere else (dashboard, home, error)
            if not on_item and ("marketplace" in url or "facebook.com" in url):
                # Only if body also lacks live listing affordances
                if not _has_live_controls(page):
                    return True
        if "unavailable" in url or "error" in url:
            return True

    try:
        body = page.locator("body").inner_text(timeout=5_000).lower()
    except Exception:
        body = ""

    if body and any(phrase in body for phrase in _ALREADY_GONE_PHRASES):
        return True

    # Empty-ish error pages often have very little text + no listing actions
    if body and len(body) < 400 and not _has_live_controls(page):
        if any(token in body for token in ("not found", "no encontrada", "404", "unavailable")):
            return True

    return False


def _has_live_controls(page: Page) -> bool:
    try:
        if page.get_by_role("button", name=_MARK_SOLD).count():
            return True
        if page.get_by_role("menuitem", name=_MARK_SOLD).count():
            return True
        for pattern in _DELETE_PATTERNS:
            if page.get_by_role("menuitem", name=pattern).count():
                return True
            if page.get_by_role("button", name=pattern).count():
                return True
    except Exception:
        pass
    return False


def _mark_sold(page: Page) -> None:
    _open_listing_menu(page)
    if _click_menu_action(page, action="mark_sold"):
        _confirm_if_needed(page)
        return
    if _listing_already_gone(page):
        return
    raise FacebookPostingError(
        "Mark-as-sold control not found "
        "(expected 'Marcar como vendido' / 'Mark as sold')"
    )


def _delete_listing(page: Page) -> None:
    _open_listing_menu(page)
    if _click_menu_action(page, action="delete"):
        _confirm_if_needed(page, prefer_delete=True)
        return
    if _listing_already_gone(page):
        return
    raise FacebookPostingError(
        "Delete control not found "
        "(expected 'Eliminar publicación' / 'Delete listing')"
    )


def _click_menu_action(page: Page, *, action: str) -> bool:
    """Click delete or mark-sold from an open menu or page-level control."""
    patterns = _DELETE_PATTERNS if action == "delete" else (_MARK_SOLD,)
    getters: list[Callable[[], Locator]] = []
    for pattern in patterns:
        getters.extend(
            [
                lambda p=pattern: page.get_by_role("menuitem", name=p),
                lambda p=pattern: page.get_by_role("button", name=p),
                lambda p=pattern: page.get_by_role("link", name=p),
                lambda p=pattern: page.locator('[role="menuitem"]').filter(has_text=p),
                lambda p=pattern: page.locator('[role="button"]').filter(has_text=p),
                lambda p=pattern: page.get_by_text(p),
            ]
        )

    for getter in getters:
        try:
            target = getter()
            count = target.count()
            if count == 0:
                continue
            for i in range(min(count, 6)):
                item = target.nth(i)
                try:
                    if not item.is_visible():
                        continue
                    item.scroll_into_view_if_needed(timeout=2_000)
                    item.click(timeout=8_000)
                    page.wait_for_timeout(1_500)
                    return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


def _open_listing_menu(page: Page) -> bool:
    """Open listing ⋮ / Más opciones menu. Returns True if a menu appears open."""
    # Exact aria-labels first (common on es-MX Marketplace)
    for selector in _MORE_ARIA_SELECTORS:
        try:
            buttons = page.locator(selector)
            for i in range(min(buttons.count() or 0, 8)):
                btn = buttons.nth(i)
                try:
                    if not btn.is_visible():
                        continue
                    btn.click(timeout=5_000)
                    page.wait_for_timeout(1_200)
                    if (
                        page.get_by_role("menuitem").count()
                        or page.locator('[role="menuitem"]').count()
                    ):
                        return True
                except Exception:
                    continue
        except Exception:
            continue

    candidates = [
        lambda: page.get_by_role("button", name=_MORE_MENU),
        lambda: page.get_by_label(_MORE_MENU),
        lambda: page.locator(
            '[aria-label*="More" i], [aria-label*="Más" i], '
            '[aria-label*="Options" i], [aria-label*="Opciones" i], '
            '[aria-label*="actions" i], [aria-label*="Acciones" i]'
        ),
    ]
    for getter in candidates:
        try:
            button = getter()
            if button.count() == 0:
                continue
            for i in range(min(button.count(), 10)):
                btn = button.nth(i)
                try:
                    if not btn.is_visible():
                        continue
                    btn.click(timeout=5_000)
                    page.wait_for_timeout(1_200)
                    if page.get_by_role("menuitem").count() or page.locator(
                        '[role="menuitem"]'
                    ).count():
                        return True
                except Exception:
                    continue
        except Exception:
            continue
    return _menu_open(page)


def _confirm_if_needed(page: Page, *, prefer_delete: bool = False) -> None:
    patterns = (
        [
            re.compile(
                r"delete listing|eliminar publicación|delete|eliminar|remove",
                re.I,
            ),
            re.compile(r"confirm|confirmar", re.I),
            re.compile(r"yes|sí|ok|aceptar|continuar", re.I),
        ]
        if prefer_delete
        else [
            re.compile(r"confirm|confirmar", re.I),
            re.compile(r"mark as sold|marcar como vendido", re.I),
            re.compile(r"delete listing|eliminar publicación|delete|eliminar", re.I),
            re.compile(r"yes|sí|ok|aceptar", re.I),
        ]
    )
    for pattern in patterns:
        try:
            button = page.get_by_role("button", name=pattern)
            if button.count() and button.first.is_visible():
                button.first.click(timeout=5_000)
                page.wait_for_timeout(2_000)
                return
        except Exception:
            continue
        try:
            alt = page.locator('[role="button"]').filter(has_text=pattern)
            if alt.count() and alt.first.is_visible():
                alt.first.click(timeout=5_000)
                page.wait_for_timeout(2_000)
                return
        except Exception:
            continue
