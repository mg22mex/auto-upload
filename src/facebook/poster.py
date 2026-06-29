from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from src.facebook.errors import FacebookPostingError
from src.facebook.photos import download_vehicle_photos
from src.facebook.util import parse_mileage_km, parse_mxn_price, vehicle_description
from src.models import Vehicle


def create_vehicle_listing(
    page: Page,
    vehicle: Vehicle,
    *,
    fb_config: dict,
    max_photos: int,
    log_dir: Path,
) -> str:
    create_url = fb_config.get("create_url", "https://www.facebook.com/marketplace/create/vehicle")
    page.goto(create_url, wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_timeout(3_000)

    photo_paths = download_vehicle_photos(vehicle, max_photos=max_photos)
    try:
        _upload_photos(page, photo_paths)
        _fill_vehicle_form(page, vehicle, fb_config)
        listing_url = _publish_and_capture_url(page)
        if not listing_url:
            raise FacebookPostingError("Published but could not capture listing URL")
        return listing_url
    except Exception as exc:
        _save_debug(page, log_dir, vehicle.autosell_id, "create_failed")
        raise FacebookPostingError(f"Create failed for {vehicle.autosell_id}: {exc}") from exc
    finally:
        for path in photo_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        if photo_paths:
            try:
                photo_paths[0].parent.rmdir()
            except OSError:
                pass


def _upload_photos(page: Page, photo_paths: list[Path]) -> None:
    file_input = page.locator('input[type="file"]').first
    if file_input.count() == 0:
        add_photos = _first_visible(
            page.get_by_role("button", name=re.compile(r"add photos|agregar fotos|añadir fotos", re.I)),
            page.get_by_text(re.compile(r"add photos|agregar fotos|añadir fotos", re.I)),
        )
        if add_photos:
            with page.expect_file_chooser(timeout=30_000) as fc_info:
                add_photos.click()
            file_chooser = fc_info.value
            file_chooser.set_files([str(p) for p in photo_paths])
        else:
            raise FacebookPostingError("Photo upload control not found")
    else:
        file_input.set_input_files([str(p) for p in photo_paths])

    page.wait_for_timeout(4_000)
    _click_if_visible(page, re.compile(r"^next$|^siguiente$", re.I), timeout_ms=20_000)


def _fill_vehicle_form(page: Page, vehicle: Vehicle, fb_config: dict) -> None:
    _fill_text(page, re.compile(r"title|título", re.I), vehicle.marketplace_title)
    _fill_text(page, re.compile(r"price|precio", re.I), parse_mxn_price(vehicle.price))

    city = fb_config.get("location_city", "Chihuahua")
    _fill_text(page, re.compile(r"location|ubicación|ciudad", re.I), city)

    _fill_text(page, re.compile(r"year|año|model year", re.I), vehicle.year)
    _fill_text(page, re.compile(r"make|marca", re.I), vehicle.brand)
    _fill_text(page, re.compile(r"model|modelo", re.I), vehicle.title)

    mileage = parse_mileage_km(vehicle.mileage)
    _fill_text(page, re.compile(r"mileage|kilometraje|odometer", re.I), mileage)

    description = vehicle_description(vehicle)
    _fill_text(page, re.compile(r"description|descripción", re.I), description, multiline=True)

    _click_if_visible(page, re.compile(r"^next$|^siguiente$", re.I), timeout_ms=15_000)


def _publish_and_capture_url(page: Page) -> str | None:
    _click_if_visible(page, re.compile(r"publish|publicar|list item|publicar artículo", re.I), timeout_ms=30_000)
    page.wait_for_timeout(5_000)

    if "/marketplace/item/" in page.url:
        return page.url.split("?")[0]

    link = page.locator('a[href*="/marketplace/item/"]').first
    try:
        if link.count():
            href = link.get_attribute("href") or ""
            if href.startswith("/"):
                return f"https://www.facebook.com{href.split('?')[0]}"
            return href.split("?")[0]
    except PlaywrightTimeoutError:
        pass

    selling_url = "https://www.facebook.com/marketplace/you/selling"
    page.goto(selling_url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(3_000)
    first_item = page.locator('a[href*="/marketplace/item/"]').first
    if first_item.count():
        href = first_item.get_attribute("href") or ""
        if href.startswith("/"):
            return f"https://www.facebook.com{href.split('?')[0]}"
        return href.split("?")[0]
    return None


def _fill_text(page: Page, label_pattern: re.Pattern[str], value: str, *, multiline: bool = False) -> None:
    if not value:
        return

    candidates: list[Locator] = [
        page.get_by_label(label_pattern),
        page.get_by_placeholder(label_pattern),
        page.locator(f'label:has-text("{label_pattern.pattern}") + input'),
        page.locator(f'label:has-text("{label_pattern.pattern}") + textarea'),
    ]

    for locator in candidates:
        try:
            if locator.count() == 0:
                continue
            target = locator.first
            if not target.is_visible():
                continue
            target.click()
            target.fill(value)
            if multiline:
                return
            return
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue


def _click_if_visible(page: Page, name_pattern: re.Pattern[str], *, timeout_ms: int) -> None:
    button = page.get_by_role("button", name=name_pattern)
    try:
        if button.count() and button.first.is_visible():
            button.first.click(timeout=timeout_ms)
            page.wait_for_timeout(1_500)
            return
    except PlaywrightTimeoutError:
        pass

    text = page.get_by_text(name_pattern)
    if text.count():
        text.first.click(timeout=timeout_ms)
        page.wait_for_timeout(1_500)


def _first_visible(*locators: Locator) -> Locator | None:
    for locator in locators:
        try:
            if locator.count() and locator.first.is_visible():
                return locator.first
        except Exception:
            continue
    return None


def _save_debug(page: Page, log_dir: Path, autosell_id: str, tag: str) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{autosell_id}_{tag}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        print(f"Saved debug screenshot: {path}")
    except Exception:
        pass
