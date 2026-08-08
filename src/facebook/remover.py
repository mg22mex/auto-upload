from __future__ import annotations

import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from playwright.sync_api import Page

from src.facebook.errors import FacebookPostingError
from src.facebook.poster import _save_debug

RemovalAction = Literal["delete", "mark_sold"]

SELLING_URL = "https://www.facebook.com/marketplace/you/selling"

_ALREADY_GONE_PHRASES = (
    "isn't available",
    "no longer available",
    "content isn't available",
    "no está disponible",
    "contenido no está disponible",
    "this listing is unavailable",
    "esta publicación no está disponible",
    "listing unavailable",
    "publicación no disponible",
    "has been deleted",
    "ha sido eliminada",
    "was deleted",
)

_MARK_AVAILABLE = re.compile(
    r"mark as available|marcar como disponible",
    re.I,
)
_MARK_SOLD = re.compile(
    r"mark as sold|marcar como vendido|marcar vendido|mark sold",
    re.I,
)
# Prefer specific "delete listing / eliminar publicación" before bare "delete"
_DELETE_MENU = re.compile(
    r"delete listing|eliminar publicación|delete this listing|"
    r"eliminar esta publicación|eliminar anuncio",
    re.I,
)
_DELETE_LOOSE = re.compile(r"^\s*delete\s*$|^\s*eliminar\s*$", re.I)
_MORE_MENU = re.compile(
    r"more options|more|más opciones|opciones|manage|administrar|"
    r"see more|ver más",
    re.I,
)
_CONFIRM = re.compile(
    r"confirm|confirmar|delete listing|eliminar publicación|"
    r"delete|eliminar|mark as sold|marcar como vendido|"
    r"yes|sí|ok|aceptar|continuar",
    re.I,
)


def extract_item_id(listing_url: str) -> str | None:
    """Pull Marketplace item id from a listing URL."""
    if not listing_url:
        return None
    match = re.search(r"/item/(\d+)", listing_url)
    if match:
        return match.group(1)
    # path tail fallback
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
    """
    action = (removal_action or "mark_sold").strip().lower()
    if action not in ("delete", "mark_sold"):
        action = "mark_sold"

    page.goto(listing_url, wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_timeout(2_500)

    if _listing_already_gone(page):
        print(f"  {autosell_id}: listing already sold/unavailable — treating as removed")
        if require_verified:
            _assert_removed_or_raise(page, listing_url, autosell_id, log_dir)
        return True

    try:
        if action == "delete":
            _delete_listing(page)
        else:
            _mark_sold(page)
        page.wait_for_timeout(2_000)
        # Second chance: if mark_sold left a menu, try hard delete for reposts
        if require_verified and action == "delete" and not _listing_already_gone(page):
            try:
                _delete_listing(page)
            except FacebookPostingError:
                pass
    except Exception as exc:
        if _listing_already_gone(page):
            print(
                f"  {autosell_id}: listing already sold/unavailable after attempt "
                "— treating as removed"
            )
            if require_verified:
                _assert_removed_or_raise(page, listing_url, autosell_id, log_dir)
            return True
        _save_debug(page, log_dir, autosell_id, "remove_failed")
        raise FacebookPostingError(f"Remove failed for {autosell_id}: {exc}") from exc

    if require_verified:
        _assert_removed_or_raise(page, listing_url, autosell_id, log_dir)
        return True

    if _listing_already_gone(page):
        return True
    # Soft path (catalog remove): best-effort; do not invent success for repost.
    return _listing_already_gone(page)


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
    try:
        page.goto(listing_url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(2_500)
    except Exception:
        # Navigation failure on deleted item is often success
        return True

    if _listing_already_gone(page):
        return True

    item_id = extract_item_id(listing_url)
    if item_id and not _item_visible_on_selling(page, item_id):
        # Detail page may still show shells; selling shelf is ground truth for live stock
        # If detail still looks live (has Mark as sold), fail.
        if not _looks_like_live_listing(page):
            return True
        return False

    if not _looks_like_live_listing(page) and not item_id:
        return _listing_already_gone(page)

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
        return True  # fail closed: treat as still visible → verification fails

    try:
        links = page.locator(f'a[href*="/marketplace/item/{item_id}"]')
        count = links.count()
        if count == 0:
            return False
        for i in range(min(count, 8)):
            try:
                if links.nth(i).is_visible():
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return True


def _listing_already_gone(page: Page) -> bool:
    """True when the FB listing is already sold or unavailable (nothing left to remove)."""
    try:
        available = page.get_by_role("button", name=_MARK_AVAILABLE)
        if available.count() and available.first.is_visible():
            return True
    except Exception:
        pass

    try:
        body = page.locator("body").inner_text(timeout=5_000).lower()
    except Exception:
        return False
    return any(phrase in body for phrase in _ALREADY_GONE_PHRASES)


def _mark_sold(page: Page) -> None:
    _open_listing_menu(page)
    for getter in (
        lambda: page.get_by_role("menuitem", name=_MARK_SOLD),
        lambda: page.get_by_role("button", name=_MARK_SOLD),
        lambda: page.get_by_text(_MARK_SOLD),
    ):
        try:
            target = getter()
            if target.count() and target.first.is_visible():
                target.first.click(timeout=8_000)
                page.wait_for_timeout(2_000)
                _confirm_if_needed(page)
                return
        except Exception:
            continue

    if _listing_already_gone(page):
        return
    raise FacebookPostingError(
        "Mark-as-sold control not found "
        "(expected 'Marcar como vendido' / 'Mark as sold')"
    )


def _delete_listing(page: Page) -> None:
    _open_listing_menu(page)
    for pattern in (_DELETE_MENU, _DELETE_LOOSE):
        for role in ("menuitem", "button"):
            try:
                item = page.get_by_role(role, name=pattern)
                if item.count() and item.first.is_visible():
                    item.first.click(timeout=8_000)
                    page.wait_for_timeout(1_500)
                    _confirm_if_needed(page, prefer_delete=True)
                    return
            except Exception:
                continue
        try:
            text = page.get_by_text(pattern)
            if text.count() and text.first.is_visible():
                text.first.click(timeout=8_000)
                page.wait_for_timeout(1_500)
                _confirm_if_needed(page, prefer_delete=True)
                return
        except Exception:
            continue

    if _listing_already_gone(page):
        return
    raise FacebookPostingError(
        "Delete control not found "
        "(expected 'Eliminar publicación' / 'Delete listing')"
    )


def _open_listing_menu(page: Page) -> None:
    # Prefer accessible names, then aria-label, then common junk drawer.
    candidates = [
        lambda: page.get_by_role("button", name=_MORE_MENU),
        lambda: page.get_by_label(_MORE_MENU),
        lambda: page.locator(
            '[aria-label*="More" i], [aria-label*="Más" i], '
            '[aria-label*="Options" i], [aria-label*="Opciones" i]'
        ),
    ]
    for getter in candidates:
        try:
            button = getter()
            if button.count() == 0:
                continue
            for i in range(min(button.count(), 6)):
                btn = button.nth(i)
                try:
                    if not btn.is_visible():
                        continue
                    btn.click(timeout=5_000)
                    page.wait_for_timeout(1_500)
                    # Menu open if any menuitem with sold/delete appears
                    if (
                        page.get_by_role("menuitem", name=_MARK_SOLD).count()
                        or page.get_by_role("menuitem", name=_DELETE_MENU).count()
                        or page.get_by_role("menuitem", name=_DELETE_LOOSE).count()
                    ):
                        return
                except Exception:
                    continue
        except Exception:
            continue


def _confirm_if_needed(page: Page, *, prefer_delete: bool = False) -> None:
    patterns = (
        [
            re.compile(r"delete listing|eliminar publicación|delete|eliminar", re.I),
            re.compile(r"confirm|confirmar", re.I),
            re.compile(r"yes|sí|ok|aceptar", re.I),
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
