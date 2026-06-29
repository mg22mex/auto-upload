from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from src.facebook.errors import FacebookPostingError
from src.facebook.photos import download_vehicle_photos
from src.facebook.ui import (
    PUBLISH_LABELS,
    NEXT_LABELS,
    advance_past_photo_step,
    click_labeled_action,
    dismiss_overlays,
    log_page_state,
    wait_for_photo_previews,
)
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
    dismiss_overlays(page)
    log_page_state(page, "create_opened")
    _ensure_vehicle_create_flow(page)

    photo_paths = download_vehicle_photos(vehicle, max_photos=max_photos)
    try:
        _upload_photos(page, photo_paths, log_dir, vehicle.autosell_id)
        _fill_vehicle_form(page, vehicle, fb_config)
        listing_url = _publish_and_capture_url(page, vehicle, log_dir, vehicle.autosell_id)
        if not listing_url:
            raise FacebookPostingError(
                "Publish flow finished but listing URL was not found. "
                "Check Marketplace → Your listings for the new post."
            )
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


def _ensure_vehicle_create_flow(page: Page) -> None:
    """Handle category pickers if FB does not land directly on the vehicle form."""
    for label in (
        "Vehículo",
        "Vehicle",
        "Vehículos",
        "Vehicles",
        "Carro",
        "Car",
        "Camioneta",
        "Truck",
    ):
        option = page.locator(f'[aria-label="{label}"]').first
        try:
            if option.count() and option.is_visible():
                option.click(timeout=3_000)
                page.wait_for_timeout(1_500)
                log_page_state(page, f"selected_{label}")
                return
        except Exception:
            continue

    for pattern in (
        re.compile(r"veh[ií]culo", re.I),
        re.compile(r"vehicle", re.I),
    ):
        text = page.get_by_text(pattern)
        try:
            if text.count() and text.first.is_visible():
                text.first.click(timeout=3_000)
                page.wait_for_timeout(1_500)
                log_page_state(page, "selected_vehicle_text")
                return
        except Exception:
            continue


def _upload_photos(page: Page, photo_paths: list[Path], log_dir: Path, autosell_id: str) -> None:
    file_input = page.locator('input[type="file"]').first
    if file_input.count() == 0:
        add_photos = _first_visible(
            page.get_by_role("button", name=re.compile(r"add photos|agregar fotos|a[nñ]adir fotos", re.I)),
            page.locator('[aria-label*="fotos" i], [aria-label*="photos" i]'),
            page.get_by_text(re.compile(r"add photos|agregar fotos|a[nñ]adir fotos", re.I)),
        )
        if add_photos:
            with page.expect_file_chooser(timeout=30_000) as fc_info:
                add_photos.click()
            file_chooser = fc_info.value
            file_chooser.set_files([str(p) for p in photo_paths])
        else:
            _save_debug(page, log_dir, autosell_id, "no_file_input")
            raise FacebookPostingError("Photo upload control not found")
    else:
        file_input.set_input_files([str(p) for p in photo_paths])

    print(f"Uploaded {len(photo_paths)} photo(s); waiting for previews...")
    wait_for_photo_previews(page, min_count=1, timeout_ms=120_000)
    _save_debug(page, log_dir, autosell_id, "after_upload")
    log_page_state(page, "photos_uploaded")

    advance_past_photo_step(page, timeout_ms=90_000)
    log_page_state(page, "after_photo_next")


def _fill_vehicle_form(page: Page, vehicle: Vehicle, fb_config: dict) -> None:
    page.wait_for_timeout(2_000)
    dismiss_overlays(page)
    log_page_state(page, "vehicle_form")

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

    click_labeled_action(page, NEXT_LABELS, timeout_ms=60_000)
    log_page_state(page, "after_form_next")


def _publish_and_capture_url(
    page: Page,
    vehicle: Vehicle,
    log_dir: Path,
    autosell_id: str,
) -> str | None:
    try:
        click_labeled_action(page, PUBLISH_LABELS, timeout_ms=30_000)
    except FacebookPostingError:
        click_labeled_action(page, PUBLISH_LABELS, timeout_ms=30_000, allow_force=True)

    try:
        page.wait_for_url(re.compile(r"facebook\.com/marketplace"), timeout=30_000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(8_000)
    log_page_state(page, "after_publish")
    _save_debug(page, log_dir, autosell_id, "after_publish")

    listing_url = _extract_item_url_from_page(page)
    if listing_url:
        return listing_url

    for listing_page in (
        "https://www.facebook.com/marketplace/you/dashboard",
        "https://www.facebook.com/marketplace/you/selling",
    ):
        page.goto(listing_page, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(4_000)
        listing_url = _find_listing_url_by_vehicle(page, vehicle)
        if listing_url:
            print(f"Found listing on {listing_page}: {listing_url}")
            return listing_url

    return None


def _extract_item_url_from_page(page: Page) -> str | None:
    if "/marketplace/item/" in page.url:
        return page.url.split("?")[0]

    for link in page.locator('a[href*="/marketplace/item/"]').all()[:20]:
        try:
            href = link.get_attribute("href") or ""
            normalized = _normalize_fb_url(href)
            if normalized:
                return normalized
        except Exception:
            continue
    return None


def _find_listing_url_by_vehicle(page: Page, vehicle: Vehicle) -> str | None:
    price_digits = parse_mxn_price(vehicle.price)
    needles = [
        vehicle.marketplace_title,
        f"{vehicle.year} {vehicle.brand}",
        vehicle.brand,
        vehicle.title,
        vehicle.price,
        price_digits,
    ]
    needles = [n.strip() for n in needles if n and n.strip()]

    item_links = page.locator('a[href*="/marketplace/item/"]')
    try:
        count = min(item_links.count(), 80)
    except Exception:
        return None

    for index in range(count):
        link = item_links.nth(index)
        try:
            href = link.get_attribute("href") or ""
            normalized = _normalize_fb_url(href)
            if not normalized:
                continue
            texts = [
                link.inner_text(timeout=1_000) or "",
                link.get_attribute("aria-label") or "",
            ]
            try:
                texts.append(
                    link.locator("xpath=ancestor::div[position()<=3]").first.inner_text(timeout=1_000)
                )
            except Exception:
                pass
            haystack = " ".join(texts).lower()
            if any(needle.lower() in haystack for needle in needles):
                return normalized
        except Exception:
            continue

    title_link = page.get_by_role("link", name=re.compile(re.escape(vehicle.brand), re.I))
    try:
        if title_link.count():
            for index in range(min(title_link.count(), 10)):
                link = title_link.nth(index)
                href = link.get_attribute("href") or ""
                if "/marketplace/item/" in href:
                    return _normalize_fb_url(href)
    except Exception:
        pass

    return None


def _normalize_fb_url(href: str) -> str | None:
    if not href or "/marketplace/item/" not in href:
        return None
    if href.startswith("/"):
        return f"https://www.facebook.com{href.split('?')[0]}"
    return href.split("?")[0]


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
