from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import Page

from src.facebook.errors import FacebookPostingError
from src.facebook.poster import _save_debug


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

    try:
        if removal_action == "delete":
            _delete_listing(page)
        else:
            _mark_sold(page)
    except Exception as exc:
        _save_debug(page, log_dir, autosell_id, "remove_failed")
        raise FacebookPostingError(f"Remove failed for {autosell_id}: {exc}") from exc


def _mark_sold(page: Page) -> None:
    _open_listing_menu(page)
    sold = page.get_by_role("menuitem", name=re.compile(r"mark as sold|marcar como vendido", re.I))
    if sold.count() and sold.first.is_visible():
        sold.first.click()
        page.wait_for_timeout(2_000)
        _confirm_if_needed(page)
        return

    button = page.get_by_role("button", name=re.compile(r"mark as sold|marcar como vendido", re.I))
    if button.count() and button.first.is_visible():
        button.first.click()
        page.wait_for_timeout(2_000)
        _confirm_if_needed(page)
        return

    raise FacebookPostingError("Mark-as-sold control not found")


def _delete_listing(page: Page) -> None:
    _open_listing_menu(page)
    delete_item = page.get_by_role("menuitem", name=re.compile(r"delete|eliminar", re.I))
    if delete_item.count() and delete_item.first.is_visible():
        delete_item.first.click()
        page.wait_for_timeout(1_500)
        _confirm_if_needed(page)
        return

    raise FacebookPostingError("Delete control not found")


def _open_listing_menu(page: Page) -> None:
    for pattern in (
        re.compile(r"more|más", re.I),
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
        re.compile(r"yes|sí", re.I),
    ):
        button = page.get_by_role("button", name=pattern)
        if button.count() and button.first.is_visible():
            button.first.click()
            page.wait_for_timeout(2_000)
            return
