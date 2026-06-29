from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from src.facebook.errors import FacebookPostingError
from src.facebook.network import MarketplaceItemCapture
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
from src.facebook.util import (
    mileage_for_listing,
    parse_mxn_price,
    vehicle_description,
)
from src.models import Vehicle


ITEM_URL_PATTERN = re.compile(r"/marketplace/item/(\d+)")


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
    capture = MarketplaceItemCapture()
    capture.attach(page)
    try:
        _upload_photos(page, photo_paths, log_dir, vehicle.autosell_id)
        filled_names = _fill_vehicle_form(page, vehicle, fb_config, log_dir, vehicle.autosell_id)
        print(f"Form fields filled: {len(filled_names)} ({', '.join(sorted(filled_names))})")
        required = {"year", "price", "make"}
        missing = required - filled_names
        if missing:
            _save_debug(page, log_dir, vehicle.autosell_id, "form_incomplete")
            raise FacebookPostingError(
                f"Required vehicle fields missing: {', '.join(sorted(missing))}"
            )
        if "mileage" not in filled_names:
            km = mileage_for_listing(vehicle.mileage)
            print(f"  WARN mileage input not found on FB form; using {km} km in description only")
        listing_url = _publish_and_capture_url(
            page, vehicle, log_dir, vehicle.autosell_id, capture=capture
        )
        if not listing_url:
            raise FacebookPostingError(
                "Publish flow finished but listing URL was not found. "
                "Check Marketplace → Your listings; run scripts/fb_find_listing.py if needed."
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
    """Handle category pickers only when not already on the vehicle composer."""
    if re.search(r"/marketplace/create/(vehicle|item)", page.url, re.I):
        return
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


def _fill_vehicle_form(
    page: Page,
    vehicle: Vehicle,
    fb_config: dict,
    log_dir: Path,
    autosell_id: str,
) -> set[str]:
    page.wait_for_timeout(2_000)
    dismiss_overlays(page)
    log_page_state(page, "vehicle_form")
    _save_debug(page, log_dir, autosell_id, "vehicle_form")

    filled_names: set[str] = set()
    city = fb_config.get("location_city", "Chihuahua")

    fields: list[tuple[str, str, tuple[str, ...], str]] = [
        ("location", city, ("Location", "Ubicación", "Ciudad"), "text"),
        ("year", vehicle.year, ("Year", "Año", "Model year", "Año del modelo"), "combobox"),
        ("make", vehicle.brand, ("Make", "Marca"), "combobox"),
        ("model", vehicle.title, ("Model", "Modelo"), "combobox"),
        ("mileage", mileage_for_listing(vehicle.mileage), (
            "Mileage", "Kilometraje", "Odometer", "Odometro", "Odómetro",
            "Kilometers", "Kilómetros", "Kilometros", "Vehicle mileage",
        ), "mileage"),
        ("price", parse_mxn_price(vehicle.price), ("Price", "Precio"), "text"),
        ("description", vehicle_description(vehicle), ("Description", "Descripción"), "multiline"),
    ]

    for name, value, labels, mode in fields:
        if not value:
            print(f"  SKIP {name} (empty)")
            continue
        ok = False
        if mode == "combobox":
            ok = _fill_combobox(page, labels, value)
        elif mode == "numeric":
            ok = _fill_numeric_field(page, labels, value)
        elif mode == "mileage":
            ok = _fill_mileage(page, value)
        elif mode == "multiline":
            ok = _fill_vehicle_field(page, labels, value, multiline=True)
        else:
            ok = _fill_vehicle_field(page, labels, value)
        if ok:
            filled_names.add(name)
            print(f"  filled {name}")
        else:
            print(f"  MISSING {name}")

    click_labeled_action(page, NEXT_LABELS, timeout_ms=60_000)
    log_page_state(page, "after_form_next")
    _save_debug(page, log_dir, autosell_id, "before_publish")
    return filled_names


def _publish_and_capture_url(
    page: Page,
    vehicle: Vehicle,
    log_dir: Path,
    autosell_id: str,
    *,
    capture: MarketplaceItemCapture,
) -> str | None:
    _dismiss_vehicle_category_prompts(page)

    try:
        click_labeled_action(page, PUBLISH_LABELS, timeout_ms=30_000)
    except FacebookPostingError:
        click_labeled_action(page, PUBLISH_LABELS, timeout_ms=30_000, allow_force=True)

    try:
        page.wait_for_url(re.compile(r"facebook\.com/marketplace"), timeout=45_000)
    except PlaywrightTimeoutError:
        pass

    for wait_sec in (5, 10, 15):
        page.wait_for_timeout(wait_sec * 1000)
        _dismiss_vehicle_category_prompts(page)
        listing_url = capture.latest_url()
        if listing_url:
            print(f"Captured listing from network: {listing_url}")
            return listing_url
        if _publish_succeeded(page):
            break

    log_page_state(page, "after_publish")
    _save_debug(page, log_dir, autosell_id, "after_publish")
    _log_publish_errors(page)

    if capture.item_ids:
        print(f"Network saw item ids: {', '.join(capture.item_ids)}")

    listing_url = _capture_listing_url(page, vehicle)
    if listing_url:
        return listing_url

    for listing_page in (
        "https://www.facebook.com/marketplace/you/selling",
        "https://www.facebook.com/marketplace/you/dashboard",
    ):
        for attempt in range(3):
            page.goto(listing_page, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(4_000)
            _dismiss_vehicle_category_prompts(page)
            listing_url = _capture_listing_url(page, vehicle, scroll=True)
            if listing_url:
                print(f"Found listing on {listing_page}: {listing_url}")
                return listing_url
            listing_url = _find_top_matching_listing(page, vehicle)
            if listing_url:
                print(f"Found top matching listing on {listing_page}: {listing_url}")
                return listing_url
            page.wait_for_timeout(5_000)

    listing_url = _open_listing_by_clicking_card(page, vehicle)
    if listing_url:
        print(f"Found listing by clicking card: {listing_url}")
        return listing_url

    if capture.item_ids:
        url = capture.latest_url()
        print(f"Using network item id without page verification: {url}")
        return url

    return None


def _publish_succeeded(page: Page) -> bool:
    try:
        body = page.locator("body").inner_text(timeout=5_000).lower()
    except Exception:
        return False
    phrases = (
        "listing is live",
        "has been listed",
        "your listing",
        "publicación publicada",
        "publicación está activa",
        "se publicó",
        "view listing",
        "ver anuncio",
    )
    return any(phrase in body for phrase in phrases)


def _dismiss_vehicle_category_prompts(page: Page) -> None:
    for pattern in (
        re.compile(r"list in vehicles", re.I),
        re.compile(r"listar en veh[ií]culos", re.I),
        re.compile(r"did you mean to list", re.I),
    ):
        try:
            link = page.get_by_text(pattern)
            if link.count() and link.first.is_visible():
                link.first.click(timeout=3_000)
                page.wait_for_timeout(1_500)
        except Exception:
            continue


def _find_top_matching_listing(page: Page, vehicle: Vehicle) -> str | None:
    price_digits = parse_mxn_price(vehicle.price)
    needles = [
        vehicle.brand.lower(),
        vehicle.year,
        price_digits,
        vehicle.marketplace_title.lower(),
    ]
    for entry in _collect_marketplace_links(page)[:15]:
        haystack = entry.get("text", "").lower()
        hits = sum(1 for n in needles if n and n in haystack)
        if hits >= 2 or (vehicle.brand.lower() in haystack and vehicle.year in haystack):
            normalized = _normalize_fb_url(entry.get("href", ""))
            if normalized:
                return normalized
    links = _collect_marketplace_links(page)
    if len(links) == 1:
        return _normalize_fb_url(links[0].get("href", ""))
    return None


def _capture_listing_url(page: Page, vehicle: Vehicle, *, scroll: bool = False) -> str | None:
    listing_url = _extract_item_url_from_page(page)
    if listing_url:
        return listing_url

    listing_url = _find_view_listing_link(page)
    if listing_url:
        return listing_url

    listing_url = _find_item_url_in_html(page, vehicle, single_only=True)
    if listing_url:
        return listing_url

    listing_url = _find_listing_url_by_vehicle(page, vehicle)
    if listing_url:
        return listing_url

    if scroll:
        for _ in range(10):
            page.mouse.wheel(0, 2_000)
            page.wait_for_timeout(1_500)
            listing_url = _find_listing_url_by_vehicle(page, vehicle)
            if listing_url:
                return listing_url
            listing_url = _find_item_url_near_text(page, vehicle.brand)
            if listing_url:
                return listing_url

    listing_url = _find_item_url_near_text(page, vehicle.brand)
    if listing_url:
        return listing_url

    return _find_item_url_in_html(page, vehicle, single_only=False)


def _find_view_listing_link(page: Page) -> str | None:
    for label in (
        "View listing",
        "View your listing",
        "See listing",
        "Ver anuncio",
        "Ver publicación",
        "Ver tu publicación",
    ):
        for locator in (
            page.locator(f'a[aria-label="{label}"]'),
            page.locator(f'[role="link"][aria-label="{label}"]'),
            page.get_by_role("link", name=label),
        ):
            try:
                if locator.count() and locator.first.is_visible():
                    href = locator.first.get_attribute("href") or ""
                    normalized = _normalize_fb_url(href)
                    if normalized:
                        return normalized
                    locator.first.click(timeout=5_000)
                    page.wait_for_timeout(3_000)
                    if "/marketplace/item/" in page.url:
                        return page.url.split("?")[0]
            except Exception:
                continue
    return None


def _find_item_url_in_html(page: Page, vehicle: Vehicle, *, single_only: bool) -> str | None:
    try:
        html = page.content()
    except Exception:
        return None

    item_ids = []
    for match in ITEM_URL_PATTERN.finditer(html):
        item_id = match.group(1)
        if item_id not in item_ids:
            item_ids.append(item_id)

    if single_only and len(item_ids) == 1:
        return f"https://www.facebook.com/marketplace/item/{item_ids[0]}/"

    if not item_ids:
        return None

    price_digits = parse_mxn_price(vehicle.price)
    needles = [
        vehicle.marketplace_title.lower(),
        vehicle.brand.lower(),
        vehicle.year,
        price_digits,
        vehicle.price.lower(),
    ]
    html_lower = html.lower()
    for item_id in item_ids:
        window_start = max(0, html_lower.find(f"/marketplace/item/{item_id}") - 400)
        window_end = min(len(html_lower), html_lower.find(f"/marketplace/item/{item_id}") + 400)
        window = html_lower[window_start:window_end]
        if any(needle in window for needle in needles if needle):
            return f"https://www.facebook.com/marketplace/item/{item_id}/"

    return None


def _find_item_url_near_text(page: Page, text: str) -> str | None:
    if not text:
        return None
    try:
        href = page.evaluate(
            """(needle) => {
                const lower = needle.toLowerCase();
                const links = [...document.querySelectorAll(
                  'a[href*="/marketplace/item/"], [role="link"][href*="/marketplace/item/"]'
                )];
                for (const link of links) {
                  const href = link.href || link.getAttribute('href') || '';
                  if (!href.includes('/marketplace/item/')) continue;
                  const block = (link.innerText || link.getAttribute('aria-label') || '').toLowerCase();
                  if (block.includes(lower)) return href.split('?')[0];
                }
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                while (walker.nextNode()) {
                  const node = walker.currentNode;
                  if (!node.textContent || !node.textContent.toLowerCase().includes(lower)) continue;
                  let el = node.parentElement;
                  for (let i = 0; i < 10 && el; i++) {
                    const link = el.querySelector('a[href*="/marketplace/item/"]');
                    if (link) {
                      const href = link.href || link.getAttribute('href') || '';
                      return href.split('?')[0];
                    }
                    el = el.parentElement;
                  }
                }
                return null;
            }""",
            text,
        )
    except Exception:
        return None
    return _normalize_fb_url(href or "")


def _extract_item_url_from_page(page: Page) -> str | None:
    if "/marketplace/item/" in page.url:
        return page.url.split("?")[0]

    for selector in (
        'a[href*="/marketplace/item/"]',
        '[role="link"][href*="/marketplace/item/"]',
    ):
        try:
            links = page.locator(selector).all()[:30]
        except Exception:
            continue
        for link in links:
            try:
                href = link.get_attribute("href") or ""
                normalized = _normalize_fb_url(href)
                if normalized:
                    return normalized
            except Exception:
                continue
    return None


def _collect_marketplace_links(page: Page) -> list[dict[str, str]]:
    try:
        return page.evaluate(
            """() => {
                const out = [];
                const seen = new Set();
                const nodes = document.querySelectorAll(
                  'a[href*="/marketplace/item/"], [role="link"][href*="/marketplace/item/"]'
                );
                for (const node of nodes) {
                  const href = (node.href || node.getAttribute('href') || '').split('?')[0];
                  if (!href || !href.includes('/marketplace/item/') || seen.has(href)) continue;
                  seen.add(href);
                  out.push({
                    href,
                    text: (node.innerText || node.getAttribute('aria-label') || '').trim(),
                  });
                }
                return out;
            }"""
        )
    except Exception:
        return []


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

    for entry in _collect_marketplace_links(page):
        haystack = entry.get("text", "").lower()
        if any(needle.lower() in haystack for needle in needles):
            normalized = _normalize_fb_url(entry.get("href", ""))
            if normalized:
                return normalized

    item_links = page.locator('a[href*="/marketplace/item/"], [role="link"][href*="/marketplace/item/"]')
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


def _fill_combobox(page: Page, labels: tuple[str, ...], value: str) -> bool:
    for label in labels:
        for locator in (
            page.get_by_role("combobox", name=label),
            page.get_by_role("combobox", name=re.compile(re.escape(label), re.I)),
            page.locator(f'[role="combobox"][aria-label="{label}"]'),
            page.locator(f'[role="combobox"][aria-label*="{label}" i]'),
            page.locator(f'[aria-haspopup="listbox"][aria-label*="{label}" i]'),
        ):
            try:
                if locator.count() == 0:
                    continue
                box = locator.first
                if not box.is_visible():
                    continue
                box.click()
                page.wait_for_timeout(600)
                page.keyboard.press("Control+a")
                page.keyboard.type(value, delay=40)
                page.wait_for_timeout(1_200)
                option = page.get_by_role("option", name=re.compile(rf"^{re.escape(value)}$", re.I))
                if option.count() and option.first.is_visible():
                    option.first.click()
                else:
                    loose = page.locator(f'[role="option"]:has-text("{value}")')
                    if loose.count() and loose.first.is_visible():
                        loose.first.click()
                    else:
                        page.keyboard.press("Enter")
                page.wait_for_timeout(600)
                return True
            except Exception:
                continue
    return _fill_vehicle_field(page, labels, value)


def _fill_numeric_field(page: Page, labels: tuple[str, ...], value: str) -> bool:
    digits = re.sub(r"[^\d]", "", value)
    if not digits:
        digits = "12345"
    for label in labels:
        for locator in (
            page.get_by_role("spinbutton", name=label),
            page.locator(f'input[inputmode="numeric"][aria-label*="{label}" i]'),
            page.locator(f'input[type="text"][aria-label*="{label}" i]'),
            page.locator(f'[role="spinbutton"][aria-label*="{label}" i]'),
        ):
            try:
                if locator.count() == 0:
                    continue
                target = locator.first
                if not target.is_visible():
                    continue
                target.click()
                target.fill(digits)
                return True
            except Exception:
                continue

    for label in labels:
        try:
            labels_loc = page.get_by_text(re.compile(label, re.I))
            if labels_loc.count() == 0:
                continue
            label_node = labels_loc.first
            if not label_node.is_visible():
                continue
            for xpath in (
                "xpath=following::input[1]",
                "xpath=ancestor::div[1]//input",
                "xpath=ancestor::label[1]//input",
            ):
                field = label_node.locator(xpath)
                if field.count() and field.first.is_visible():
                    field.first.click()
                    field.first.fill(digits)
                    return True
        except Exception:
            continue

    return _fill_vehicle_field(page, labels, digits)


def _fill_mileage(page: Page, value: str) -> bool:
    digits = re.sub(r"[^\d]", "", value) or "12345"
    labels = (
        "Mileage", "Kilometraje", "Odometer", "Vehicle mileage",
        "Kilometers", "Kilómetros",
    )

    if _fill_numeric_field(page, labels, digits):
        return True
    if _fill_mileage_via_js(page, digits):
        return True
    if _fill_mileage_after_model(page, digits):
        return True
    if _fill_mileage_empty_input_scan(page, digits):
        return True

    return _fill_vehicle_field(page, labels, digits)


def _fill_mileage_empty_input_scan(page: Page, digits: str) -> bool:
    try:
        result = page.evaluate(
            """(digits) => {
                const setter = Object.getOwnPropertyDescriptor(
                  window.HTMLInputElement.prototype, 'value'
                )?.set;
                const skip = (al) => {
                  al = (al || '').toLowerCase();
                  return (
                    al.includes('search') || al.includes('location') ||
                    al.includes('ubic') || al.includes('price') ||
                    al.includes('precio') || al.includes('year') ||
                    al.includes('año') || al.includes('make') ||
                    al.includes('marca') || al.includes('model') ||
                    al.includes('modelo')
                  );
                };
                const inputs = [...document.querySelectorAll('input')].filter((el) => {
                  const r = el.getBoundingClientRect();
                  if (r.width < 50 || r.height < 10) return false;
                  const type = (el.type || '').toLowerCase();
                  if (type === 'file' || type === 'hidden' || type === 'checkbox') return false;
                  if (skip(el.getAttribute('aria-label'))) return false;
                  const val = (el.value || '').trim();
                  return val.length === 0;
                });
                inputs.sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
                for (const input of inputs) {
                  input.focus();
                  if (setter) setter.call(input, digits);
                  else input.value = digits;
                  input.dispatchEvent(new Event('input', { bubbles: true }));
                  input.dispatchEvent(new Event('change', { bubbles: true }));
                  return {
                    ok: true,
                    ariaLabel: input.getAttribute('aria-label') || '',
                    id: input.id || '',
                  };
                }
                return { ok: false };
            }""",
            digits,
        )
    except Exception:
        return False
    if result and result.get("ok"):
        print(
            f"  mileage via empty-input scan "
            f"(id={result.get('id', '?')}, label={result.get('ariaLabel', '')!r})"
        )
        return True
    return False


def _fill_mileage_via_js(page: Page, digits: str) -> bool:
    try:
        result = page.evaluate(
            """(digits) => {
                const keywords = [
                  'kilometraje', 'mileage', 'odometer', 'kilometers',
                  'kilómetros', 'kilometros', 'vehicle mileage',
                ];
                const setter = Object.getOwnPropertyDescriptor(
                  window.HTMLInputElement.prototype, 'value'
                )?.set;
                const visible = (el) => {
                  const r = el.getBoundingClientRect();
                  return r.width > 20 && r.height > 10;
                };
                const apply = (el) => {
                  el.focus();
                  if (setter && el instanceof HTMLInputElement) {
                    setter.call(el, digits);
                  } else {
                    el.value = digits;
                  }
                  el.dispatchEvent(new Event('input', { bubbles: true }));
                  el.dispatchEvent(new Event('change', { bubbles: true }));
                  return true;
                };
                const inputs = [
                  ...document.querySelectorAll('input, [role="spinbutton"]'),
                ];
                for (const input of inputs) {
                  if (!visible(input)) continue;
                  let node = input.parentElement;
                  for (let depth = 0; depth < 14 && node; depth++) {
                    const text = (node.textContent || '').toLowerCase().slice(0, 800);
                    if (keywords.some((k) => text.includes(k))) {
                      apply(input);
                      return { ok: true, id: input.id || '' };
                    }
                    node = node.parentElement;
                  }
                }
                for (const label of document.querySelectorAll('label, span, div')) {
                  const raw = (label.textContent || '').trim().toLowerCase();
                  if (!raw || raw.length > 40) continue;
                  if (!keywords.some((k) => raw === k || raw.startsWith(k))) continue;
                  const container = label.closest('div');
                  if (!container) continue;
                  const input = container.querySelector('input, [role="spinbutton"]');
                  if (input && visible(input)) {
                    apply(input);
                    return { ok: true, id: input.id || '' };
                  }
                }
                return { ok: false };
            }""",
            digits,
        )
    except Exception:
        return False
    if result and result.get("ok"):
        print(f"  mileage via JS (id={result.get('id', '?')})")
        return True
    return False


def _fill_mileage_after_model(page: Page, digits: str) -> bool:
    """Tab from the model combobox into the next field (often mileage)."""
    for label in ("Model", "Modelo"):
        try:
            box = page.get_by_role("combobox", name=label)
            if box.count() == 0:
                box = page.locator(f'[role="combobox"][aria-label*="{label}" i]')
            if box.count() == 0 or not box.first.is_visible():
                continue
            box.first.click()
            page.wait_for_timeout(300)
            page.keyboard.press("Tab")
            page.wait_for_timeout(300)
            page.keyboard.type(digits, delay=30)
            page.keyboard.press("Tab")
            page.wait_for_timeout(300)
            print("  mileage via Tab after model")
            return True
        except Exception:
            continue
    return False


def _fill_vehicle_field(
    page: Page,
    labels: tuple[str, ...],
    value: str,
    *,
    multiline: bool = False,
) -> bool:
    if not value:
        return False

    tag = "textarea" if multiline else "input"
    for label in labels:
        for locator in (
            page.get_by_label(label),
            page.get_by_placeholder(label),
            page.locator(f'{tag}[aria-label="{label}"]'),
            page.locator(f'{tag}[aria-label*="{label}" i]'),
            page.locator(f'[contenteditable="true"][aria-label*="{label}" i]'),
        ):
            try:
                if locator.count() == 0:
                    continue
                target = locator.first
                if not target.is_visible():
                    continue
                target.click()
                target.fill(value)
                return True
            except Exception:
                continue

    pattern = re.compile("|".join(re.escape(label) for label in labels), re.I)
    return _fill_text(page, pattern, value, multiline=multiline)


def _log_publish_errors(page: Page) -> None:
    try:
        body = page.locator("body").inner_text(timeout=3_000).lower()
    except Exception:
        return
    for phrase in (
        "something went wrong",
        "algo salió mal",
        "try again",
        "inténtalo de nuevo",
        "required",
        "obligatorio",
        "couldn't publish",
        "no se pudo publicar",
    ):
        if phrase in body:
            print(f"  FB page message detected: {phrase!r}")


def _open_listing_by_clicking_card(page: Page, vehicle: Vehicle) -> str | None:
    for term in (vehicle.marketplace_title, f"{vehicle.year} {vehicle.brand}", vehicle.brand):
        if not term:
            continue
        locator = page.get_by_text(term, exact=False)
        try:
            if locator.count() == 0:
                continue
            target = locator.first
            if not target.is_visible():
                continue
            target.click(timeout=5_000)
            page.wait_for_timeout(4_000)
            if "/marketplace/item/" in page.url:
                return page.url.split("?")[0]
        except Exception:
            continue
    return None


def _fill_text(
    page: Page,
    label_pattern: re.Pattern[str],
    value: str,
    *,
    multiline: bool = False,
) -> bool:
    if not value:
        return False

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
            return True
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue
    return False


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
