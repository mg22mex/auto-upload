from __future__ import annotations

import re
import time
from pathlib import Path

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from src.facebook.errors import FacebookPostingError
from src.facebook.photos import download_vehicle_photos
from src.facebook.util import parse_mileage_km, parse_mxn_price, vehicle_description
from src.models import Vehicle

NEXT_PATTERN = re.compile(r"^\s*(next|siguiente)\s*$", re.I)
PUBLISH_PATTERN = re.compile(r"publish|publicar|list item|publicar art[ií]culo", re.I)


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
    _dismiss_overlays(page)

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
            page.get_by_role("button", name=re.compile(r"add photos|agregar fotos|a[nñ]adir fotos", re.I)),
            page.get_by_text(re.compile(r"add photos|agregar fotos|a[nñ]adir fotos", re.I)),
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

    print(f"Uploaded {len(photo_paths)} photo(s); waiting for Facebook to process...")
    _click_action_button(page, NEXT_PATTERN, timeout_ms=180_000)


def _fill_vehicle_form(page: Page, vehicle: Vehicle, fb_config: dict) -> None:
    page.wait_for_timeout(2_000)
    _dismiss_overlays(page)

    _fill_text(page, re.compile(r"title|t[ií]tulo", re.I), vehicle.marketplace_title)
    _fill_text(page, re.compile(r"price|precio", re.I), parse_mxn_price(vehicle.price))

    city = fb_config.get("location_city", "Chihuahua")
    _fill_text(page, re.compile(r"location|ubicaci[oó]n|ciudad", re.I), city)

    _fill_text(page, re.compile(r"year|a[nñ]o|model year", re.I), vehicle.year)
    _fill_text(page, re.compile(r"make|marca", re.I), vehicle.brand)
    _fill_text(page, re.compile(r"model|modelo", re.I), vehicle.title)

    mileage = parse_mileage_km(vehicle.mileage)
    _fill_text(page, re.compile(r"mileage|kilometraje|odometer", re.I), mileage)

    description = vehicle_description(vehicle)
    _fill_text(page, re.compile(r"description|descripci[oó]n", re.I), description, multiline=True)

    _click_action_button(page, NEXT_PATTERN, timeout_ms=30_000)


def _publish_and_capture_url(page: Page) -> str | None:
    _click_action_button(page, PUBLISH_PATTERN, timeout_ms=60_000)
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


def _click_action_button(page: Page, name_pattern: re.Pattern[str], *, timeout_ms: int) -> None:
    deadline = time.monotonic() + (timeout_ms / 1000)
    last_error = "button not found"

    while time.monotonic() < deadline:
        for locator in _button_candidates(page, name_pattern):
            try:
                if locator.count() == 0:
                    continue
                for index in range(locator.count()):
                    button = locator.nth(index)
                    if not button.is_visible():
                        continue
                    if button.get_attribute("aria-disabled") == "true":
                        continue
                    if not button.is_enabled():
                        continue
                    button.scroll_into_view_if_needed()
                    button.click(timeout=5_000)
                    page.wait_for_timeout(1_500)
                    return
            except PlaywrightTimeoutError as exc:
                last_error = str(exc)
            except Exception as exc:
                last_error = str(exc)
        page.wait_for_timeout(2_000)

    raise FacebookPostingError(
        f"Timed out waiting for enabled button matching {name_pattern.pattern!r}: {last_error}"
    )


def _button_candidates(page: Page, name_pattern: re.Pattern[str]) -> list[Locator]:
    return [
        page.get_by_role("button", name=name_pattern),
        page.locator('[role="button"]').filter(has_text=name_pattern),
        page.get_by_text(name_pattern),
    ]


def _fill_text(page: Page, label_pattern: re.Pattern[str], value: str, *, multiline: bool = False) -> None:
    if not value:
        return

    candidates: list[Locator] = [
        page.get_by_label(label_pattern),
        page.get_by_placeholder(label_pattern),
        page.locator("input").filter(has=page.get_by_text(label_pattern)),
        page.locator("textarea").filter(has=page.get_by_text(label_pattern)),
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
            return
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue


def _dismiss_overlays(page: Page) -> None:
    for pattern in (
        re.compile(r"allow all cookies|permitir todas", re.I),
        re.compile(r"^accept$|^aceptar$", re.I),
    ):
        button = page.get_by_role("button", name=pattern)
        try:
            if button.count() and button.first.is_visible():
                button.first.click(timeout=3_000)
                page.wait_for_timeout(1_000)
        except Exception:
            continue


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
