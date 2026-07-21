from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import Page

from src.facebook.errors import FacebookPostingError
from src.facebook.poster import _save_debug

_ALREADY_GONE_PHRASES = (
    "isn't available",
    "no longer available",
    "content isn't available",
    "no está disponible",
    "contenido no está disponible",
    "this listing is unavailable",
    "esta publicación no está disponible",
)

_MARK_AVAILABLE = re.compile(r"mark as available|marcar como disponible", re.I)
_MARK_SOLD = re.compile(r"mark as sold|marcar como vendido", re.I)
_DELETE = re.compile(r"delete listing|delete|eliminar publicación|eliminar", re.I)


def remove_vehicle_listing(
    page: Page,
    listing_url: str,
    *,
    autosell_id: str,
    removal_action: str,
    log_dir: Path,
) -> None:
    page.goto(listing_url, wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_timeout(2_000)

    if _listing_already_gone(page):
        print(f"  {autosell_id}: listing already sold/unavailable — treating as removed")
        return

    try:
        if removal_action == "delete":
            _delete_listing(page)
        else:
            _mark_sold(page)
    except Exception as exc:
        if _listing_already_gone(page):
            print(f"  {autosell_id}: listing already sold/unavailable after attempt — treating as removed")
            return
        _save_debug(page, log_dir, autosell_id, "remove_failed")
        raise FacebookPostingError(f"Remove failed for {autosell_id}: {exc}") from exc


def _listing_already_gone(page: Page) -> bool:
    """True when the FB listing is already sold or unavailable (nothing left to remove)."""
    available = page.get_by_role("button", name=_MARK_AVAILABLE)
    try:
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
    sold = page.get_by_role("menuitem", name=_MARK_SOLD)
    if sold.count() and sold.first.is_visible():
        sold.first.click()
        page.wait_for_timeout(2_000)
        _confirm_if_needed(page)
        return

    button = page.get_by_role("button", name=_MARK_SOLD)
    if button.count() and button.first.is_visible():
        button.first.click()
        page.wait_for_timeout(2_000)
        _confirm_if_needed(page)
        return

    # Some Marketplace UIs expose the action as a link / text control.
    text_ctrl = page.get_by_text(_MARK_SOLD)
    if text_ctrl.count() and text_ctrl.first.is_visible():
        text_ctrl.first.click()
        page.wait_for_timeout(2_000)
        _confirm_if_needed(page)
        return

    if _listing_already_gone(page):
        return

    raise FacebookPostingError("Mark-as-sold control not found")


def _delete_listing(page: Page) -> None:
    _open_listing_menu(page)
    delete_item = page.get_by_role("menuitem", name=_DELETE)
    if delete_item.count() and delete_item.first.is_visible():
        delete_item.first.click()
        page.wait_for_timeout(1_500)
        _confirm_if_needed(page)
        return

    if _listing_already_gone(page):
        return

    raise FacebookPostingError("Delete control not found")


def _open_listing_menu(page: Page) -> None:
    for pattern in (
        re.compile(r"more options|more|más opciones|más", re.I),
        re.compile(r"manage|administrar", re.I),
    ):
        button = page.get_by_role("button", name=pattern)
        if button.count() and button.first.is_visible():
            button.first.click()
            page.wait_for_timeout(1_500)
            return


def _confirm_if_needed(page: Page) -> None:
    for pattern in (
        re.compile(r"confirm|confirmar", re.I),
        re.compile(r"delete|eliminar", re.I),
        re.compile(r"mark as sold|marcar como vendido", re.I),
        re.compile(r"yes|sí", re.I),
    ):
        button = page.get_by_role("button", name=pattern)
        if button.count() and button.first.is_visible():
            button.first.click()
            page.wait_for_timeout(2_000)
            return
