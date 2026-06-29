from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import Page

from src.facebook.errors import FacebookPostingError
from src.facebook.poster import _fill_text, _save_debug
from src.facebook.util import parse_mxn_price, vehicle_description
from src.models import Vehicle


def update_vehicle_listing(
    page: Page,
    listing_url: str,
    vehicle: Vehicle,
    *,
    log_dir: Path,
) -> None:
    page.goto(listing_url, wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_timeout(2_000)

    try:
        _open_edit_flow(page)
        _fill_text(page, re.compile(r"price|precio", re.I), parse_mxn_price(vehicle.price))
        _fill_text(
            page,
            re.compile(r"description|descripción", re.I),
            vehicle_description(vehicle),
            multiline=True,
        )
        _save_listing(page)
    except Exception as exc:
        _save_debug(page, log_dir, vehicle.autosell_id, "update_failed")
        raise FacebookPostingError(f"Update failed for {vehicle.autosell_id}: {exc}") from exc


def _open_edit_flow(page: Page) -> None:
    for pattern in (
        re.compile(r"edit|editar", re.I),
        re.compile(r"manage|administrar", re.I),
    ):
        button = page.get_by_role("button", name=pattern)
        if button.count() and button.first.is_visible():
            button.first.click()
            page.wait_for_timeout(1_500)
            break

    edit_listing = page.get_by_role("menuitem", name=re.compile(r"edit listing|editar", re.I))
    if edit_listing.count() and edit_listing.first.is_visible():
        edit_listing.first.click()
        page.wait_for_timeout(2_000)


def _save_listing(page: Page) -> None:
    for pattern in (
        re.compile(r"save|guardar", re.I),
        re.compile(r"update|actualizar", re.I),
    ):
        button = page.get_by_role("button", name=pattern)
        if button.count() and button.first.is_visible():
            button.first.click()
            page.wait_for_timeout(3_000)
            return
