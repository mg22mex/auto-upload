from __future__ import annotations

import re
import time
from pathlib import Path

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from src.facebook.errors import FacebookPostingError, FacebookSessionError
from src.facebook.session import page_shows_login_form
from src.facebook.network import MarketplaceItemCapture
from src.facebook.photos import download_vehicle_photos
from src.facebook.ui import (
    PUBLISH_LABELS,
    NEXT_LABELS,
    advance_composer_next,
    advance_past_photo_step,
    click_labeled_action,
    disable_promote_listing,
    dismiss_overlays,
    log_page_state,
    wait_for_composer_next_enabled,
    wait_for_photo_previews,
    human_pause_before_next,
)
from src.facebook.categorize import (
    ListingAttributes,
    categorize_vehicle,
    fb_make_candidates,
    fb_model_name,
    is_unknown_fb_make,
    spanish_candidates,
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
    _wait_for_photo_file_input(page, timeout_ms=15_000)
    dismiss_overlays(page)
    log_page_state(page, "create_opened")
    if page_shows_login_form(page):
        raise FacebookSessionError(
            "Facebook session expired — log in again with scripts/fb_login.py"
        )
    _ensure_vehicle_create_flow(page, create_url=create_url)
    if page_shows_login_form(page):
        raise FacebookSessionError(
            "Facebook session expired — log in again with scripts/fb_login.py"
        )
    if not re.search(r"/marketplace/create/(vehicle|item)", page.url, re.I):
        raise FacebookPostingError(
            f"Not on vehicle composer after opening create URL (at {page.url})"
        )

    # Hard gate: any matching shelf card (any tab) must be deleted first.
    from src.facebook.remover import ensure_no_matching_shelf_listings

    if not ensure_no_matching_shelf_listings(
        page, vehicle, autosell_id=vehicle.autosell_id
    ):
        raise FacebookPostingError(
            f"SKIP_CREATE: matching listing still on selling shelf for "
            f"{vehicle.autosell_id} — refusing duplicate publish"
        )

    # Shelf scan navigates to /you/selling* — reopen composer and wait for DOM.
    _reopen_vehicle_composer(page, create_url=create_url)

    photo_paths = download_vehicle_photos(vehicle, max_photos=max_photos)
    capture = MarketplaceItemCapture()
    capture.attach(page)
    try:
        _upload_photos(page, photo_paths, log_dir, vehicle.autosell_id)
        filled_names = _fill_vehicle_form(
            page, vehicle, fb_config, log_dir, vehicle.autosell_id,
            photo_count=len(photo_paths),
        )
        print(f"Form fields filled: {len(filled_names)} ({', '.join(sorted(filled_names))})")
        required = {"year", "price", "make", "model"}
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
        if not _verify_listing_url(page, listing_url, vehicle):
            _save_debug(page, log_dir, vehicle.autosell_id, "verify_failed")
            raise FacebookPostingError(
                f"Listing URL did not verify as live: {listing_url}. "
                "Publish likely did not complete — check obj969_after_publish.png."
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


def _ensure_vehicle_create_flow(page: Page, *, create_url: str) -> None:
    """Handle category pickers only when not already on the vehicle composer."""
    if re.search(r"/marketplace/create/(vehicle|item)", page.url, re.I):
        return
    for label in (
        "Vehículo",
        "Vehicle",
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
                if re.search(r"/marketplace/create/(vehicle|item)", page.url, re.I):
                    return
        except Exception:
            continue

    page.goto(create_url, wait_until="domcontentloaded", timeout=90_000)
    _wait_for_photo_file_input(page, timeout_ms=15_000)
    dismiss_overlays(page)
    log_page_state(page, "create_retry")


PHOTO_ADD_RE = re.compile(
    r"add photos|agregar fotos|a[nñ]adir fotos|add pictures|agregar im[aá]genes",
    re.I,
)


def _reopen_vehicle_composer(page: Page, *, create_url: str) -> None:
    dismiss_overlays(page)
    page.goto(create_url, wait_until="domcontentloaded", timeout=90_000)
    _wait_for_photo_file_input(page, timeout_ms=20_000)
    dismiss_overlays(page)
    log_page_state(page, "create_composer_ready")
    if page_shows_login_form(page):
        raise FacebookSessionError(
            "Facebook session expired — log in again with scripts/fb_login.py"
        )


def _wait_for_photo_file_input(page: Page, *, timeout_ms: int = 20_000) -> bool:
    """Wait until the hidden file input (or photo step) is in the DOM."""
    try:
        page.wait_for_selector('input[type="file"]', state="attached", timeout=timeout_ms)
        page.wait_for_timeout(1_500)
        return True
    except PlaywrightTimeoutError:
        print("  WARN: input[type=file] not attached; settling then retrying via Add Photos")
        page.wait_for_timeout(3_000)
        return False


def _add_photos_locator(page: Page) -> Locator | None:
    return _first_visible(
        page.get_by_role("button", name=PHOTO_ADD_RE),
        page.locator('[aria-label*="fotos" i], [aria-label*="photos" i]'),
        page.get_by_text(PHOTO_ADD_RE),
        page.locator('[role="button"]').filter(has_text=PHOTO_ADD_RE),
    )


def _try_attach_photos(page: Page, photo_paths: list[Path]) -> bool:
    paths = [str(p) for p in photo_paths]
    file_input = page.locator('input[type="file"]').first
    try:
        if file_input.count() == 0:
            page.wait_for_selector('input[type="file"]', state="attached", timeout=8_000)
            file_input = page.locator('input[type="file"]').first
    except PlaywrightTimeoutError:
        pass
    except Exception:
        pass
    if file_input.count():
        file_input.set_input_files(paths)
        return True
    add_photos = _add_photos_locator(page)
    if not add_photos:
        return False
    with page.expect_file_chooser(timeout=30_000) as fc_info:
        add_photos.click()
    fc_info.value.set_files(paths)
    return True


def _upload_photos(page: Page, photo_paths: list[Path], log_dir: Path, autosell_id: str) -> None:
    attached = False
    last_exc: Exception | None = None
    for attempt in range(1, 4):
        dismiss_overlays(page)
        if attempt > 1:
            print(f"  photo upload control retry {attempt}/3 (2s backoff)")
            page.wait_for_timeout(2_000)
            _wait_for_photo_file_input(page, timeout_ms=8_000)
        try:
            if _try_attach_photos(page, photo_paths):
                attached = True
                break
        except Exception as exc:
            last_exc = exc
            print(f"  photo upload attempt {attempt}/3 failed: {exc}")
    if not attached:
        _save_debug(page, log_dir, autosell_id, "no_file_input")
        detail = f": {last_exc}" if last_exc else ""
        raise FacebookPostingError("Photo upload control not found" + detail)

    n = len(photo_paths)
    print(f"Uploaded {n} photo(s); waiting for previews...")
    preview_timeout = max(120_000, n * 8_000)
    wait_for_photo_previews(page, min_count=n, timeout_ms=preview_timeout)
    # Poll Siguiente/Next — no fixed 80–100s sleep after large uploads.
    next_timeout = max(30_000, min(n * 5_000, 120_000))
    print(f"  waiting for Siguiente after {n} photo preview(s) (up to {next_timeout}ms)")
    next_ready = _wait_for_next_enabled_polling(page, timeout_ms=next_timeout)
    if not next_ready:
        print("  Siguiente still disabled after previews — will force-click")
    _save_debug(page, log_dir, autosell_id, "after_upload")
    log_page_state(page, "photos_uploaded")

    advance_past_photo_step(
        page,
        timeout_ms=max(60_000, n * 4_000),
        photos_already_ready=True,
        next_enable_polled=next_ready,
    )
    log_page_state(page, "after_photo_next")


def _fill_vehicle_form(
    page: Page,
    vehicle: Vehicle,
    fb_config: dict,
    log_dir: Path,
    autosell_id: str,
    *,
    photo_count: int = 1,
) -> set[str]:
    page.wait_for_timeout(2_000)
    dismiss_overlays(page)
    log_page_state(page, "vehicle_form")
    _save_debug(page, log_dir, autosell_id, "vehicle_form")

    filled_names: set[str] = set()
    city = fb_config.get("location_city", "Chihuahua")
    attrs = categorize_vehicle(vehicle)
    print(f"  categorized: {attrs.summary()}")

    # Match FB composer order from manual flow:
    # vehicle type -> location/year/make/model/mileage/price ->
    # appearance -> vehicle details -> description
    _fill_vehicle_type(page, attrs)
    _scroll_composer_sidebar(page)

    # Match FB composer order: type -> year/make/model -> mileage -> location -> price
    core_fields: list[tuple[str, str, tuple[str, ...], str]] = [
        ("year", vehicle.year, ("Año", "Year", "Año del modelo", "Model year"), "listbox"),
        ("make", vehicle.brand, ("Marca", "Make"), "make"),
        ("model", attrs.model, ("Modelo", "Model"), "text"),
        ("mileage", attrs.mileage_km, (
            "Kilometraje", "Mileage", "Odómetro", "Odometro", "Odometer",
            "Kilómetros", "Kilometros", "Kilometers", "Vehicle mileage",
        ), "mileage"),
        ("location", city, ("Ubicación", "Location", "Ciudad"), "location"),
        ("price", parse_mxn_price(vehicle.price), ("Precio", "Price"), "numeric"),
    ]

    free_text_make = attrs.vehicle_type == "Powersport"

    for name, value, labels, mode in core_fields:
        if not value:
            print(f"  SKIP {name} (empty)")
            continue
        ok = False
        if mode == "make":
            # Todoterreno / Powersport: Marca is free text (any brand). Cars: dropdown.
            ok = _fill_make_field(page, value, free_text=free_text_make)
        elif mode == "location":
            ok = _fill_location(page, value)
            if ok and not _location_is_filled(page, value):
                ok = False
        elif mode == "listbox":
            ok = _select_from_combobox_list(page, labels, (value,))
        elif mode == "mileage":
            ok = _fill_mileage(page, value)
        elif mode == "numeric":
            ok = _fill_price(page, value) if name == "price" else _fill_numeric_field(page, labels, value)
            if name == "price" and ok and not _price_is_filled(page, value):
                ok = False
        else:
            ok = _fill_vehicle_field(page, labels, value)
        if ok:
            filled_names.add(name)
            print(f"  filled {name}")
        else:
            print(f"  MISSING {name}")

    price_digits = parse_mxn_price(vehicle.price)
    if "price" not in filled_names:
        _scroll_composer_sidebar(page)
        if _fill_price(page, price_digits) and _price_is_filled(page, price_digits):
            filled_names.add("price")
            print("  filled price (retry)")
        elif price_digits:
            print("  MISSING price (all fill strategies failed)")

    # Price JS can accidentally target the wrong input — restore location if needed.
    if not _location_is_filled(page, city):
        if _fill_location(page, city) and _location_is_filled(page, city):
            filled_names.add("location")
            print("  re-filled location after price")

    _ensure_required_comboboxes(page, vehicle, attrs)
    if _make_is_filled(page, vehicle.brand, free_text=free_text_make):
        filled_names.add("make")
    model_label = attrs.model
    if is_unknown_fb_make(vehicle.brand) and _make_other_selected(page):
        model_label = f"{vehicle.brand} {attrs.model}".strip()
    if _text_field_has_value(page, ("Modelo", "Model"), model_label) or _text_field_has_value(
        page, ("Modelo", "Model"), attrs.model
    ):
        filled_names.add("model")
    if _combobox_has_value(page, ("Año", "Year", "Año del modelo", "Model year"), vehicle.year):
        filled_names.add("year")

    _scroll_composer_sidebar(page)
    _fill_appearance_fields(page, attrs)
    _scroll_composer_sidebar(page)
    _fill_vehicle_detail_fields(page, attrs)

    description = vehicle_description(vehicle)
    if _fill_vehicle_field(page, ("Descripción", "Description"), description, multiline=True):
        filled_names.add("description")
        print("  filled description")
    else:
        print("  MISSING description")

    _scroll_composer_sidebar(page)
    # Second pass for fields that only appear after scrolling / prior fills
    _fill_appearance_fields(page, attrs)
    _fill_vehicle_detail_fields(page, attrs)

    if not _location_is_filled(page, city):
        if _fill_location(page, city) and _location_is_filled(page, city):
            print("  re-filled location (final pass)")

    if _form_inputs_complete(page, vehicle, attrs, city):
        print("  form inputs complete — waiting for Siguiente to enable")
        _wait_for_next_enabled_polling(
            page,
            timeout_ms=max(30_000, min(photo_count * 5_000, 120_000)),
        )
        print("  advancing past vehicle form")
        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(2_000)
        try:
            click_labeled_action(page, NEXT_LABELS, timeout_ms=30_000, allow_force=True)
        except FacebookPostingError:
            pass
        from src.facebook.ui import _js_click_next
        for attempt in range(10):
            _js_click_next(page)
            page.wait_for_timeout(3_000)
            if _on_review_or_past_form(page):
                log_page_state(page, "after_form_next")
                _save_debug(page, log_dir, autosell_id, "before_publish")
                return filled_names
            if attempt == 4:
                page.locator("body").click(position={"x": 400, "y": 400})
                page.wait_for_timeout(2_000)
                _wait_for_next_enabled_polling(page, timeout_ms=30_000)
        if _on_review_or_past_form(page):
            log_page_state(page, "after_form_next")
            _save_debug(page, log_dir, autosell_id, "before_publish")
            return filled_names

    try:
        wait_for_composer_next_enabled(page, timeout_ms=60_000)
        human_pause_before_next(page)
    except FacebookPostingError:
        _scroll_composer_sidebar(page)
        _fill_appearance_fields(page, attrs)
        _fill_vehicle_detail_fields(page, attrs)
        page.locator("body").click(position={"x": 400, "y": 400})
        page.wait_for_timeout(2_000)
        try:
            wait_for_composer_next_enabled(page, timeout_ms=30_000)
            human_pause_before_next(page)
        except FacebookPostingError:
            _save_debug(page, log_dir, autosell_id, "next_disabled_pre_review")
            _log_composer_comboboxes(page)
            _log_composer_inputs(page)
            if _refill_price_and_mileage_keyboard(page, vehicle, attrs):
                print("  recovered price/mileage via keyboard — rechecking Siguiente")
                page.wait_for_timeout(2_000)
                if _composer_next_enabled(page) or _on_review_or_past_form(page):
                    try:
                        click_labeled_action(page, NEXT_LABELS, timeout_ms=30_000, allow_force=True)
                    except FacebookPostingError:
                        pass
                    if _on_review_or_past_form(page):
                        log_page_state(page, "after_form_next")
                        _save_debug(page, log_dir, autosell_id, "before_publish")
                        return filled_names
            missing_hint = _missing_required_field_hint(page, vehicle, attrs, city)
            if _form_inputs_complete(page, vehicle, attrs, city):
                print("  form inputs complete — force-clicking Next")
                try:
                    page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(2_000)
                    click_labeled_action(page, NEXT_LABELS, timeout_ms=30_000, allow_force=True)
                    page.wait_for_timeout(3_000)
                    if not _on_review_or_past_form(page):
                        from src.facebook.ui import _js_click_next
                        for _ in range(3):
                            _js_click_next(page)
                            page.wait_for_timeout(2_000)
                            if _on_review_or_past_form(page):
                                break
                    if _on_review_or_past_form(page):
                        log_page_state(page, "after_form_next")
                        _save_debug(page, log_dir, autosell_id, "before_publish")
                        return filled_names
                except FacebookPostingError:
                    pass
            raise FacebookPostingError(
                f"Composer Next stayed disabled — {missing_hint}"
            )

    click_labeled_action(page, NEXT_LABELS, timeout_ms=60_000)
    log_page_state(page, "after_form_next")
    _save_debug(page, log_dir, autosell_id, "before_publish")
    return filled_names


def _wait_for_next_enabled_polling(page: Page, *, timeout_ms: int = 120_000) -> bool:
    """Poll until composer Next/Siguiente is enabled; return early when ready."""
    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        try:
            enabled = page.evaluate(
                """() => {
                    for (const label of ['Next', 'Siguiente']) {
                      for (const node of document.querySelectorAll(
                        `[aria-label="${label}"]`
                      )) {
                        if (node.getAttribute('aria-disabled') === 'true') continue;
                        const r = node.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) return true;
                      }
                    }
                    return false;
                }"""
            )
            if enabled:
                print("  Siguiente enabled")
                human_pause_before_next(page)
                return True
        except Exception:
            pass
        page.wait_for_timeout(1_000)
    print("  Siguiente still disabled after wait — will force-click")
    return False


def _normalize_odometer_digits(value: str) -> str:
    digits = re.sub(r"[^\d]", "", value or "")
    if not digits or digits == "0":
        return "12345"
    return digits


def _composer_next_enabled(page: Page) -> bool:
    try:
        return bool(
            page.evaluate(
                """() => {
                    for (const label of ['Next', 'Siguiente']) {
                      for (const node of document.querySelectorAll(
                        `[aria-label="${label}"]`
                      )) {
                        if (node.getAttribute('aria-disabled') === 'true') continue;
                        const r = node.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) return true;
                      }
                    }
                    return false;
                }"""
            )
        )
    except Exception:
        return False


def _price_is_filled(page: Page, digits: str) -> bool:
    needle = re.sub(r"[^\d]", "", digits)
    if not needle:
        return False
    try:
        inputs = page.evaluate(
            """() => [...document.querySelectorAll('input, [role="spinbutton"]')]
              .map(el => el.value || el.textContent || '')"""
        )
        return any(needle in re.sub(r"[^\d]", "", val) for val in inputs)
    except Exception:
        return False


def _missing_required_field_hint(
    page: Page, vehicle: Vehicle, attrs: ListingAttributes, city: str
) -> str:
    price_digits = parse_mxn_price(vehicle.price)
    parts: list[str] = []
    if not _price_is_filled(page, price_digits):
        parts.append("Precio")
    if not _location_is_filled(page, city):
        parts.append("Ubicación")
    if not _combobox_has_value(page, ("Año", "Year"), vehicle.year):
        parts.append("Año")
    if not _make_is_filled(page, vehicle.brand, free_text=attrs.vehicle_type == "Powersport") and not _make_other_selected(page):
        parts.append("Marca")
    if not _text_field_has_value(page, ("Modelo", "Model"), attrs.model):
        parts.append("Modelo")
    mileage_km = _normalize_odometer_digits(attrs.mileage_km)
    try:
        inputs = page.evaluate(
            """() => [...document.querySelectorAll('input')].map(el => el.value || '')"""
        )
        if not any(
            mileage_km in re.sub(r"[^\d]", "", val) and re.sub(r"[^\d]", "", val) not in ("", "0")
            for val in inputs
        ):
            parts.append("Kilometraje")
    except Exception:
        parts.append("Kilometraje")
    if parts:
        return f"required fields still empty ({', '.join(parts)})"
    return (
        "required fields appear filled but Siguiente is still disabled "
        "(check Carrocería / Color del exterior / Estado del vehículo)"
    )


def _form_inputs_complete(
    page: Page, vehicle: Vehicle, attrs: ListingAttributes, city: str
) -> bool:
    price_digits = parse_mxn_price(vehicle.price)
    checks = [
        _location_is_filled(page, city),
        _combobox_has_value(page, ("Año", "Year"), vehicle.year),
        _make_is_filled(page, vehicle.brand, free_text=attrs.vehicle_type == "Powersport"),
        _text_field_has_value(page, ("Modelo", "Model"), attrs.model),
        _price_is_filled(page, price_digits),
    ]
    try:
        inputs = page.evaluate(
            """() => [...document.querySelectorAll('input')].map(el => el.value || '')"""
        )
        mileage_km = _normalize_odometer_digits(attrs.mileage_km)
        has_mileage = any(
            mileage_km in re.sub(r"[^\d]", "", val) and re.sub(r"[^\d]", "", val) not in ("", "0")
            for val in inputs
        )
        checks.append(has_mileage)
    except Exception:
        return False
    return all(checks)


def _on_review_or_past_form(page: Page) -> bool:
    url = page.url.lower()
    if "step=audience" in url or "step=review" in url:
        return True
    try:
        body = page.locator("body").inner_text(timeout=3_000).lower()
    except Exception:
        body = ""
    if re.search(r"step 2 of 2|paso 2 de 2", body):
        return True
    if "promote listing" in body or "promocionar" in body:
        return True
    if "publicar" in body and "precio" not in body:
        return True
    return "/marketplace/create/vehicle" not in page.url


def _publish_and_capture_url(
    page: Page,
    vehicle: Vehicle,
    log_dir: Path,
    autosell_id: str,
    *,
    capture: MarketplaceItemCapture,
) -> str | None:
    _dismiss_vehicle_category_prompts(page)
    _complete_review_step(page, vehicle, log_dir, autosell_id)

    if _still_on_review_page(page):
        _save_debug(page, log_dir, autosell_id, "stuck_on_review")
        hint = _review_blocker_hint(page)
        raise FacebookPostingError(
            f"Still on review page before Publish — {hint}"
        )

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
        if _publish_succeeded(page):
            break

    log_page_state(page, "after_publish")
    _save_debug(page, log_dir, autosell_id, "after_publish")
    _log_publish_errors(page)

    if capture.item_ids:
        print(f"Network saw item ids: {', '.join(capture.item_ids)}")

    # Prefer dashboard/selling match by vehicle identity — not the first /item/ link on page.
    for listing_page in (
        "https://www.facebook.com/marketplace/you/selling",
        "https://www.facebook.com/marketplace/you/dashboard",
    ):
        page.goto(listing_page, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(4_000)
        _dismiss_vehicle_category_prompts(page)
        listing_url = _find_top_matching_listing(page, vehicle)
        if listing_url and _verify_listing_url(page, listing_url, vehicle):
            print(f"Found listing on {listing_page}: {listing_url}")
            return listing_url
        listing_url = _capture_listing_url(page, vehicle, scroll=True)
        if listing_url and _verify_listing_url(page, listing_url, vehicle):
            print(f"Found listing on {listing_page}: {listing_url}")
            return listing_url

    listing_url = _capture_listing_url(page, vehicle)
    if listing_url and _verify_listing_url(page, listing_url, vehicle):
        return listing_url

    for listing_page in (
        "https://www.facebook.com/marketplace/you/selling",
        "https://www.facebook.com/marketplace/you/dashboard",
    ):
        for attempt in range(2):
            page.goto(listing_page, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(4_000)
            _dismiss_vehicle_category_prompts(page)
            listing_url = _find_top_matching_listing(page, vehicle)
            if listing_url and _verify_listing_url(page, listing_url, vehicle):
                print(f"Found listing on {listing_page}: {listing_url}")
                return listing_url
            page.wait_for_timeout(5_000)

    listing_url = _open_listing_by_clicking_card(page, vehicle)
    if listing_url and _verify_listing_url(page, listing_url, vehicle):
        print(f"Found listing by clicking card: {listing_url}")
        return listing_url

    for item_id in capture.item_ids:
        url = f"https://www.facebook.com/marketplace/item/{item_id}/"
        if _verify_listing_url(page, url, vehicle):
            print(f"Verified network item id: {url}")
            return url

    return None


def _verify_listing_url(page: Page, url: str, vehicle: Vehicle) -> bool:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(3_000)
    except Exception:
        return False

    if "/marketplace/item/" not in page.url:
        return False

    try:
        body = page.locator("body").inner_text(timeout=5_000).lower()
    except Exception:
        return False

    unavailable = (
        "isn't available",
        "no longer available",
        "no está disponible",
        "content isn't available",
        "contenido no está disponible",
    )
    if any(phrase in body for phrase in unavailable):
        return False

    brand = vehicle.brand.strip().lower()
    if not brand or brand not in body:
        return False

    price_digits = parse_mxn_price(vehicle.price)
    has_price = price_digits in re.sub(r"[^\d]", "", body)

    model = fb_model_name(vehicle.title).lower()
    has_model = bool(model) and model.replace(" ", "") in body.replace(" ", "")

    title = vehicle.marketplace_title.lower()
    has_title = title in body or f"{vehicle.year} {brand}" in body

    # Year alone is NOT enough ("Joined Facebook in 2020" appears on every listing).
    return has_price or has_model or has_title


def _complete_review_step(page: Page, vehicle: Vehicle, log_dir: Path, autosell_id: str) -> None:
    """Review page: vehicle type, promote toggle, then Next."""
    attrs = categorize_vehicle(vehicle)
    disable_promote_listing(page)
    page.wait_for_timeout(1_000)

    if not _still_on_review_page(page):
        return

    log_page_state(page, "review_page")
    _save_debug(page, log_dir, autosell_id, "review_page")

    if _fill_vehicle_type(page, attrs):
        print("  filled vehicle_type")
        page.wait_for_timeout(1_000)

    _scroll_composer_sidebar(page)
    _fill_appearance_fields(page, attrs)
    _fill_vehicle_detail_fields(page, attrs)

    disable_promote_listing(page)
    page.wait_for_timeout(1_500)

    try:
        wait_for_composer_next_enabled(page, timeout_ms=45_000)
    except FacebookPostingError:
        _fill_appearance_fields(page, attrs)
        _fill_vehicle_detail_fields(page, attrs)
        disable_promote_listing(page)
        try:
            wait_for_composer_next_enabled(page, timeout_ms=15_000)
        except FacebookPostingError:
            print("  WARN review Next disabled; force-clicking")
            click_labeled_action(page, NEXT_LABELS, timeout_ms=15_000, allow_force=True)

    disable_promote_listing(page)
    page.wait_for_timeout(500)

    try:
        page.wait_for_function(
            """() => {
                const labels = ['Next', 'Siguiente'];
                for (const label of labels) {
                  for (const node of document.querySelectorAll(`[aria-label="${label}"]`)) {
                    if (node.getAttribute('aria-disabled') === 'true') continue;
                    const r = node.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) return true;
                  }
                }
                return false;
            }""",
            timeout=5_000,
        )
    except Exception:
        disable_promote_listing(page)

    advance_composer_next(page, timeout_ms=60_000)
    log_page_state(page, "after_review_next")
    page.wait_for_timeout(2_000)
    _save_debug(page, log_dir, autosell_id, "after_review_next")


def _still_on_review_page(page: Page) -> bool:
    try:
        body = page.locator("body").inner_text(timeout=5_000).lower()
    except Exception:
        return False

    final_review_markers = (
        "promote listing after publish",
        "promocionar el anuncio después",
        "promocionar anuncio después",
        "promover anuncio después",
    )
    if any(marker in body for marker in final_review_markers):
        return True

    if "choose a vehicle category" in body or "elige una categor" in body:
        return True

    return False


def _review_blocker_hint(page: Page) -> str:
    try:
        body = page.locator("body").inner_text(timeout=5_000).lower()
    except Exception:
        return "complete the review step manually."

    if "choose a vehicle category" in body or "elige una categor" in body:
        return "Vehicle type is required — pick Sedan/SUV/etc. on the review page."
    if "promote listing" in body or "promocionar" in body:
        return "turn off Promote listing and click Next."
    return "complete the review step manually."


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
    brand = vehicle.brand.lower()
    model = fb_model_name(vehicle.title).lower()
    best_url: str | None = None
    best_score = 0

    for entry in _collect_marketplace_links(page)[:25]:
        haystack = entry.get("text", "").lower()
        if brand not in haystack:
            continue
        score = 1
        if price_digits in re.sub(r"[^\d]", "", haystack):
            score += 2
        if model and model.replace(" ", "") in haystack.replace(" ", ""):
            score += 2
        if vehicle.year in haystack:
            score += 1
        if vehicle.marketplace_title.lower() in haystack:
            score += 2
        if score > best_score:
            normalized = _normalize_fb_url(entry.get("href", ""))
            if normalized:
                best_score = score
                best_url = normalized

    if best_url and best_score >= 3:
        return best_url
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


def _fill_vehicle_type(page: Page, attrs: ListingAttributes) -> bool:
    # Live es-MX: Can-Am / UTV → "Todoterreno"; cars → "Auto/camioneta"
    labels = ("Tipo de vehículo", "Vehicle type", "Tipo")
    candidates = spanish_candidates("vehicle_type", attrs.vehicle_type)
    # Powersport must not keep FB's default Auto/camioneta
    if attrs.vehicle_type == "Powersport":
        box = _find_combobox(page, labels)
        if box is not None and _combobox_contains_value(box, "Auto/camioneta"):
            try:
                box.click()
                page.wait_for_timeout(700)
                if _pick_option_from_list(page, "Todoterreno"):
                    page.wait_for_timeout(800)
                    print("  set vehicle_type=Todoterreno")
                    return True
                page.keyboard.press("Escape")
            except Exception:
                pass
    ok = _select_from_combobox_list(page, labels, candidates)
    if ok:
        print(f"  filled vehicle_type ({candidates[0] if candidates else '?'})")
    return ok


def _fill_appearance_fields(page: Page, attrs: ListingAttributes) -> None:
    """Vehicle appearance: body style, exterior color, interior color."""
    # Labels from live es-MX dump: Carrocería, Color del exterior, Color del interior
    appearance: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
        (
            (
                "Carrocería",
                "Estilo de carrocería",
                "Estilo",
                "Body style",
                "Body Style",
            ),
            spanish_candidates("body_style", attrs.body_style),
        ),
        (
            (
                "Color del exterior",
                "Color exterior",
                "Exterior color",
                "Exterior Color",
            ),
            spanish_candidates("exterior_color", attrs.exterior_color),
        ),
        (
            (
                "Color del interior",
                "Color interior",
                "Interior color",
                "Interior Color",
            ),
            spanish_candidates("interior_color", attrs.interior_color),
        ),
    ]
    for labels, candidates in appearance:
        if _select_from_combobox_list(page, labels, candidates):
            print(f"  filled {labels[0]}")
        elif _fill_labeled_combobox(page, labels, candidates):
            print(f"  filled {labels[0]} (labeled combobox)")
        else:
            print(f"  MISSING {labels[0]}")


def _fill_vehicle_detail_fields(page: Page, attrs: ListingAttributes) -> None:
    """Vehicle details: title checkbox, condition, fuel, transmission."""
    if _check_clean_title(page):
        print("  checked clean_title")

    # Live es-MX: Estado del vehículo (not Condición del vehículo)
    details: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
        (
            (
                "Estado del vehículo",
                "Condición del vehículo",
                "Condición",
                "Vehicle condition",
                "Condition",
            ),
            spanish_candidates("condition", attrs.condition),
        ),
        (
            ("Tipo de combustible", "Combustible", "Fuel type", "Fuel Type"),
            spanish_candidates("fuel_type", attrs.fuel_type),
        ),
        (
            ("Transmisión", "Tipo de transmisión", "Transmission"),
            spanish_candidates("transmission", attrs.transmission),
        ),
    ]
    for labels, candidates in details:
        if _select_from_combobox_list(page, labels, candidates):
            print(f"  filled {labels[0]}")
        elif any("estado" in label.lower() or "condici" in label.lower() or "condition" in label.lower() for label in labels):
            if _fill_vehicle_condition(page):
                print("  filled Vehicle condition (direct)")
            else:
                print(f"  MISSING {labels[0]}")
        else:
            print(f"  MISSING {labels[0]}")


def _fill_about_vehicle_fields(page: Page, attrs: ListingAttributes) -> None:
    _fill_appearance_fields(page, attrs)
    _fill_vehicle_detail_fields(page, attrs)


def _field_candidates(primary: str, *extra: str) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in (primary, *extra):
        if not candidate:
            continue
        key = candidate.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(candidate)
    return tuple(ordered)


def _fill_labeled_combobox(
    page: Page,
    labels: tuple[str, ...],
    candidates: tuple[str, ...],
) -> bool:
    for label in labels:
        for locator in (
            page.get_by_role("combobox", name=label),
            page.get_by_role("combobox", name=re.compile(re.escape(label), re.I)),
            page.locator(f'[role="combobox"][aria-label="{label}"]'),
            page.locator(f'[role="combobox"][aria-label*="{label}" i]'),
            page.locator(f'[aria-haspopup="listbox"][aria-label*="{label}" i]'),
            page.locator(f'text={label}').locator('xpath=following::*[@role="combobox"][1]'),
        ):
            try:
                if locator.count() == 0:
                    continue
                box = locator.first
                if not box.is_visible():
                    continue
                if _combobox_is_filled(box, labels):
                    return True
                box.scroll_into_view_if_needed()
                box.click()
                page.wait_for_timeout(700)
                for candidate in candidates:
                    if _pick_listbox_option(page, candidate):
                        page.wait_for_timeout(600)
                        if _combobox_is_filled(box, labels):
                            page.keyboard.press("Escape")
                            return True
                if _pick_first_listbox_option(page):
                    page.wait_for_timeout(600)
                    if _combobox_is_filled(box, labels):
                        page.keyboard.press("Escape")
                        return True
                page.keyboard.press("Escape")
            except Exception:
                continue
    return False


def _combobox_is_filled(box: Locator, labels: tuple[str, ...]) -> bool:
    try:
        current = (box.inner_text(timeout=1_000) or "").strip().lower()
    except Exception:
        return False
    if len(current) < 2:
        return False
    for label in labels:
        if current == label.lower():
            return False
    placeholders = ("select", "seleccionar", "choose", "elige")
    if any(current.startswith(p) for p in placeholders):
        return False
    return True


def _log_composer_inputs(page: Page) -> None:
    try:
        fields = page.evaluate(
            """() => {
                const out = [];
                for (const el of document.querySelectorAll('input, textarea')) {
                  const rect = el.getBoundingClientRect();
                  if (rect.width <= 0 || rect.height <= 0) continue;
                  const type = (el.type || '').toLowerCase();
                  if (type === 'file' || type === 'hidden' || type === 'checkbox') continue;
                  out.push({
                    tag: el.tagName,
                    type,
                    ariaLabel: el.getAttribute('aria-label') || '',
                    value: el.value || '',
                    id: el.id || '',
                  });
                }
                return out;
            }"""
        )
        print(f"  inputs: {fields}")
    except Exception as exc:
        print(f"  input dump failed: {exc}")


def _log_composer_comboboxes(page: Page) -> None:
    try:
        fields = page.evaluate(
            """() => {
                const out = [];
                for (const el of document.querySelectorAll('[role="combobox"]')) {
                  const rect = el.getBoundingClientRect();
                  if (rect.width <= 0 || rect.height <= 0) continue;
                  out.push({
                    label: el.getAttribute('aria-label') || '',
                    value: (el.innerText || '').trim(),
                    disabled: el.getAttribute('aria-disabled'),
                  });
                }
                return out;
            }"""
        )
        print(f"  comboboxes: {fields}")
    except Exception as exc:
        print(f"  combobox dump failed: {exc}")


def _fill_vehicle_condition(page: Page) -> bool:
    locators = [
        page.locator('[role="combobox"][aria-label*="estado" i]'),
        page.locator('[role="combobox"][aria-label*="condición" i]'),
        page.locator('[role="combobox"][aria-label*="condition" i]'),
        page.get_by_role("combobox", name=re.compile(r"estado|condición|condition", re.I)),
        page.locator('[role="combobox"]').filter(has_text=re.compile(r"^Estado del vehículo", re.I)),
        page.locator('text=Estado del vehículo').locator('xpath=following::*[@role="combobox"][1]'),
        page.locator('text=Condición del vehículo').locator('xpath=following::*[@role="combobox"][1]'),
        page.locator('text=Vehicle condition').locator('xpath=following::*[@role="combobox"][1]'),
    ]
    labels = (
        "Estado del vehículo",
        "Condición del vehículo",
        "Vehicle condition",
        "Condición",
        "Condition",
    )
    for locator in locators:
        try:
            if locator.count() == 0:
                continue
            box = locator.first
            if not box.is_visible():
                continue
            if _combobox_is_filled(box, labels):
                return True
            box.scroll_into_view_if_needed()
            box.click()
            page.wait_for_timeout(900)
            options = page.locator('[role="option"]')
            count = options.count()
            for index in range(count):
                option = options.nth(index)
                try:
                    if not option.is_visible():
                        continue
                    option.click()
                    page.wait_for_timeout(700)
                    if _combobox_is_filled(box, labels):
                        return True
                except Exception:
                    continue
            page.keyboard.press("Escape")
        except Exception:
            continue
    return False


def _check_clean_title(page: Page) -> bool:
    try:
        checked = page.evaluate(
            """() => {
                const words = [
                  'clean title',
                  'título limpio',
                  'titulo limpio',
                  'tiene un título limpio',
                  'tiene un titulo limpio',
                ];
                for (const el of document.querySelectorAll('[role="checkbox"], input[type="checkbox"]')) {
                  let node = el;
                  for (let depth = 0; depth < 6 && node; depth++) {
                    const text = (node.textContent || '').toLowerCase();
                    if (words.some((word) => text.includes(word))) {
                      if (el.getAttribute('aria-checked') === 'true' || el.checked) return true;
                      el.click();
                      return true;
                    }
                    node = node.parentElement;
                  }
                }
                return false;
            }"""
        )
    except Exception:
        return False
    if checked:
        page.wait_for_timeout(500)
    return bool(checked)


def _scroll_composer_sidebar(page: Page) -> None:
    try:
        page.evaluate(
            """() => {
                const scrollers = [
                  ...document.querySelectorAll('[role="complementary"], [role="dialog"], form'),
                ];
                for (const el of scrollers) {
                  const style = window.getComputedStyle(el);
                  if (!/(auto|scroll)/.test(style.overflowY)) continue;
                  const r = el.getBoundingClientRect();
                  if (r.width <= 0 || r.height <= 0) continue;
                  for (let y = 0; y <= el.scrollHeight; y += 220) {
                    el.scrollTop = y;
                  }
                  el.scrollTop = 0;
                }
            }"""
        )
        page.wait_for_timeout(800)
    except Exception:
        pass


def _pick_listbox_option(page: Page, text: str) -> bool:
    for locator in (
        page.get_by_role("option", name=re.compile(rf"^{re.escape(text)}$", re.I)),
        page.get_by_role("option", name=re.compile(re.escape(text), re.I)),
        page.locator(f'[role="option"]:has-text("{text}")'),
    ):
        try:
            if locator.count() and locator.first.is_visible():
                locator.first.click()
                return True
        except Exception:
            continue
    return False


def _pick_first_listbox_option(page: Page) -> bool:
    options = page.locator('[role="option"]')
    try:
        if options.count() and options.first.is_visible():
            options.first.click()
            return True
    except Exception:
        pass
    return False


def _fill_combobox(page: Page, labels: tuple[str, ...], value: str) -> bool:
    for label in labels:
        for locator in (
            page.get_by_role("combobox", name=label),
            page.get_by_role("combobox", name=re.compile(re.escape(label), re.I)),
            page.locator(f'[role="combobox"][aria-label="{label}"]'),
            page.locator(f'[role="combobox"][aria-label*="{label}" i]'),
            page.locator(f'[aria-haspopup="listbox"][aria-label*="{label}" i]'),
            page.locator(f'text={label}').locator('xpath=ancestor::*[@role="combobox"][1]'),
        ):
            try:
                if locator.count() == 0:
                    continue
                box = locator.first
                if not box.is_visible():
                    continue
                if _combobox_contains_value(box, value):
                    return True
                box.scroll_into_view_if_needed()
                box.click()
                page.wait_for_timeout(600)
                page.keyboard.press("Control+a")
                page.keyboard.type(value, delay=40)
                page.wait_for_timeout(1_500)
                if _pick_listbox_option(page, value):
                    page.wait_for_timeout(800)
                    if _combobox_contains_value(box, value):
                        return True
                loose = page.locator(f'[role="option"]:has-text("{value}")')
                if loose.count() and loose.first.is_visible():
                    loose.first.click()
                    page.wait_for_timeout(800)
                    if _combobox_contains_value(box, value):
                        return True
                page.keyboard.press("Enter")
                page.wait_for_timeout(800)
                if _combobox_contains_value(box, value):
                    return True
                page.keyboard.press("Escape")
            except Exception:
                continue
    return _fill_vehicle_field(page, labels, value)


def _combobox_contains_value(box: Locator, value: str) -> bool:
    try:
        text = (box.inner_text(timeout=1_000) or "").strip()
    except Exception:
        return False
    if not text or not value:
        return False
    parts = [part.strip() for part in text.split("\n") if part.strip()]
    needle = value.strip().lower()
    return any(part.lower() == needle or needle in part.lower() for part in parts)


def _marca_combobox_is_empty(page: Page) -> bool:
    box = _find_combobox(page, ("Marca", "Make"))
    if box is None:
        return True
    try:
        text = (box.inner_text(timeout=1_000) or "").strip()
    except Exception:
        return True
    parts = [part.strip() for part in text.split("\n") if part.strip()]
    if not parts:
        return True
    return len(parts) == 1 and parts[0].lower() in ("marca", "make")


def _fill_make_field(page: Page, brand: str, *, free_text: bool = False) -> bool:
    """Fill Marca / Make.

    Under Todoterreno (Powersport) Marca is a free-text input — type any brand.
    Under Auto/camioneta it is a searchable dropdown.
    """
    labels = ("Marca", "Make")
    candidates = fb_make_candidates(brand)

    if free_text:
        for candidate in candidates:
            if _fill_input_near_label(page, "Marca", candidate) or _fill_input_near_label(
                page, "Make", candidate
            ):
                print(f"  marca free-text: {candidate}")
                return True
            if _fill_vehicle_field(page, labels, candidate):
                print(f"  marca free-text: {candidate}")
                return True
        return False

    if is_unknown_fb_make(brand) and _fill_make_via_other_dropdown(page):
        if _make_is_filled(page, brand, free_text=free_text):
            print(f"  marca Otro/Other for {brand}")
            return True

    if _select_from_combobox_list(page, labels, candidates) and _make_is_filled(
        page, brand, free_text=free_text
    ):
        return True
    if _fill_make_via_other_dropdown(page) and _make_is_filled(page, brand, free_text=free_text):
        print(f"  marca Otro/Other for {brand}")
        return True
    # Unknown / classic makes — try free text after dropdown fails.
    for candidate in candidates:
        if _fill_input_near_label(page, "Marca", candidate) or _fill_input_near_label(
            page, "Make", candidate
        ):
            if _make_is_filled(page, brand):
                print(f"  marca free-text fallback: {candidate}")
                return True
    # Fallback: click visible "Marca" label, type brand, pick option.
    for candidate in candidates:
        for label in labels:
            try:
                label_node = page.get_by_text(label, exact=True)
                if not label_node.count():
                    continue
                target = label_node.first
                if not target.is_visible():
                    continue
                target.scroll_into_view_if_needed()
                target.click()
                page.wait_for_timeout(600)
                page.keyboard.press("Control+a")
                page.keyboard.press("Backspace")
                page.keyboard.type(candidate, delay=40)
                page.wait_for_timeout(1_500)
                if _pick_option_from_list(page, candidate):
                    page.wait_for_timeout(700)
                    if _combobox_has_value(page, labels, candidate):
                        return True
                page.keyboard.press("Enter")
                page.wait_for_timeout(700)
                if _combobox_has_value(page, labels, candidate):
                    return True
                page.keyboard.press("Escape")
            except Exception:
                continue
    # Last resort: free text (some locales / categories)
    if any(_fill_vehicle_field(page, labels, c) for c in candidates):
        return _make_is_filled(page, brand, free_text=free_text)
    return False


def _make_is_filled(page: Page, brand: str, *, free_text: bool = False) -> bool:
    candidates = fb_make_candidates(brand)
    labels = ("Marca", "Make")
    if free_text:
        if any(_input_near_label_has_value(page, label, c) for label in ("Marca", "Make") for c in candidates):
            return True
        return any(_text_field_has_value(page, labels, c) for c in candidates)
    if _marca_combobox_is_empty(page):
        return False
    return any(_combobox_has_value(page, labels, c) for c in candidates) or _make_other_selected(page) or any(
        _text_field_has_value(page, labels, c) for c in candidates
    )


def _make_other_selected(page: Page) -> bool:
    labels = ("Marca", "Make")
    return any(_combobox_has_value(page, labels, c) for c in ("Otro", "Other", "Otra"))


def _pick_make_other_option(page: Page) -> bool:
    try:
        count = page.locator('[role="option"]').count()
    except Exception:
        return False
    for index in range(count):
        option = page.locator('[role="option"]').nth(index)
        try:
            if not option.is_visible():
                continue
            text = (option.inner_text(timeout=500) or "").strip().lower()
            if text in ("otro", "other", "otra", "others") or text.startswith("otro") or text.startswith("other"):
                option.click()
                return True
        except Exception:
            continue
    return False


def _fill_make_via_other_dropdown(page: Page) -> bool:
    """Select Otro/Other in the Marca combobox (classic / exotic makes)."""
    labels = ("Marca", "Make")
    box = _find_combobox(page, labels)
    if box is None:
        try:
            bare = page.locator('[role="combobox"]').filter(
                has_text=re.compile(r"^Marca$", re.I)
            )
            if bare.count() and bare.first.is_visible():
                box = bare.first
        except Exception:
            return False
    if box is None:
        return False

    others = ("Otro", "Other", "Otra", "Others")
    try:
        box.scroll_into_view_if_needed()
        box.click()
        page.wait_for_timeout(1_000)
        if _pick_make_other_option(page):
            page.wait_for_timeout(800)
            if _make_other_selected(page):
                return True
        for other in others:
            page.keyboard.press("Control+a")
            page.keyboard.press("Backspace")
            page.keyboard.type(other, delay=40)
            page.wait_for_timeout(1_200)
            if _pick_make_other_option(page) or _pick_option_from_list(page, other):
                page.wait_for_timeout(800)
                if _make_other_selected(page):
                    return True
        page.keyboard.press("Escape")
    except Exception:
        pass
    return False


def _select_from_combobox_list(
    page: Page,
    labels: tuple[str, ...],
    candidates: tuple[str, ...],
) -> bool:
    box = _find_combobox(page, labels)
    if box is None:
        return False

    for candidate in candidates:
        if candidate and _combobox_contains_value(box, candidate):
            return True

    type_to_filter = any(
        label.lower() in {
            "make", "marca", "year", "año", "model year", "año del modelo",
            "carrocería", "estilo de carrocería", "body style", "body style",
            "color del exterior", "color exterior", "exterior color",
            "color del interior", "color interior", "interior color",
            "ubicación", "location", "ciudad",
        }
        for label in labels
    )

    try:
        box.scroll_into_view_if_needed()
        box.click()
        page.wait_for_timeout(800)
        for candidate in candidates:
            if not candidate:
                continue
            if type_to_filter:
                page.keyboard.press("Control+a")
                page.keyboard.press("Backspace")
                page.keyboard.type(candidate, delay=35)
                page.wait_for_timeout(1_200)
            if _pick_option_from_list(page, candidate):
                page.wait_for_timeout(700)
                if _combobox_contains_value(box, candidate):
                    page.keyboard.press("Escape")
                    return True
        page.keyboard.press("Escape")
    except Exception:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
    return False


def _pick_option_from_list(page: Page, value: str) -> bool:
    needle = value.strip().lower()
    if not needle:
        return False
    options = page.locator('[role="option"]')
    try:
        count = options.count()
    except Exception:
        return False
    for index in range(count):
        option = options.nth(index)
        try:
            if not option.is_visible():
                continue
            text = (option.inner_text(timeout=500) or "").strip()
            if text.lower() == needle or needle in text.lower():
                option.scroll_into_view_if_needed()
                option.click()
                return True
        except Exception:
            continue
    return _pick_listbox_option(page, value)


def _find_combobox(page: Page, labels: tuple[str, ...]) -> Locator | None:
    for label in labels:
        for locator in (
            page.get_by_role("combobox", name=label),
            page.get_by_role("combobox", name=re.compile(rf"^{re.escape(label)}$", re.I)),
            page.get_by_role("combobox", name=re.compile(re.escape(label), re.I)),
            page.get_by_label(label),
            page.locator(f'[role="combobox"][aria-label="{label}"]'),
            page.locator(f'[role="combobox"][aria-label*="{label}" i]'),
            page.locator(f'[aria-haspopup="listbox"][aria-label*="{label}" i]'),
            # Empty es-MX fields show only the label text (e.g. "Carrocería")
            page.locator('[role="combobox"]').filter(
                has_text=re.compile(rf"^{re.escape(label)}\s*$", re.I)
            ),
            page.locator('[role="combobox"]').filter(
                has_text=re.compile(rf"^{re.escape(label)}(?:\n|$)", re.I)
            ),
            page.locator('[role="combobox"]').filter(
                has_text=re.compile(rf"^{re.escape(label)}\b", re.I)
            ),
            # Floating label "Marca" / "Año" / "Carrocería" inside composer sidebar
            page.locator(f'span:text-is("{label}")').locator(
                "xpath=ancestor::*[@role='combobox'][1]"
            ),
            page.locator(f'label:text-is("{label}")').locator(
                "xpath=following::*[@role='combobox'][1]"
            ),
            page.locator(f'text={label}').locator('xpath=ancestor::*[@role="combobox"][1]'),
            page.locator(f'text={label}').locator(
                'xpath=following::*[@role="combobox" or @role="textbox"][1]'
            ),
        ):
            try:
                if locator.count() == 0:
                    continue
                candidate = locator.first
                if candidate.is_visible():
                    return candidate
            except Exception:
                continue
    # JS fallback: match combobox whose visible text starts with the label
    for label in labels:
        try:
            handle = page.evaluate_handle(
                """(label) => {
                    const needle = label.toLowerCase();
                    for (const el of document.querySelectorAll('[role="combobox"]')) {
                      const rect = el.getBoundingClientRect();
                      if (rect.width <= 0 || rect.height <= 0) continue;
                      const text = (el.innerText || '').trim().toLowerCase();
                      if (text === needle || text.startsWith(needle + '\\n') || text.startsWith(needle + ' ')) {
                        return el;
                      }
                    }
                    return null;
                }""",
                label,
            )
            element = handle.as_element()
            if element is not None:
                return page.locator(f'[role="combobox"]').filter(
                    has_text=re.compile(rf"^{re.escape(label)}", re.I)
                ).first
        except Exception:
            continue
    return None


def _ensure_required_comboboxes(
    page: Page, vehicle: Vehicle, attrs: ListingAttributes
) -> None:
    year_labels = ("Año", "Year", "Año del modelo", "Model year")
    make_labels = ("Marca", "Make")
    model_labels = ("Modelo", "Model")

    if not _combobox_has_value(page, year_labels, vehicle.year):
        _select_from_combobox_list(page, year_labels, (vehicle.year,))
    free_text_make = attrs.vehicle_type == "Powersport"
    if not _make_is_filled(page, vehicle.brand, free_text=free_text_make):
        _fill_make_field(page, vehicle.brand, free_text=free_text_make)
    if not _make_is_filled(page, vehicle.brand, free_text=free_text_make):
        _fill_make_via_other_dropdown(page)
    if is_unknown_fb_make(vehicle.brand) and _make_other_selected(page):
        full_model = f"{vehicle.brand} {attrs.model}".strip()
        if _fill_vehicle_field(page, model_labels, full_model):
            print(f"  model expanded for unknown make: {full_model}")
    if not _text_field_has_value(page, model_labels, attrs.model):
        _fill_vehicle_field(page, model_labels, attrs.model)

    if _combobox_has_value(page, year_labels, vehicle.year):
        print("  verified year")
    else:
        print(f"  WARN year still missing after retries ({vehicle.year})")

    if _make_is_filled(page, vehicle.brand, free_text=free_text_make):
        print("  verified make")
    else:
        print(f"  WARN make still missing after retries ({vehicle.brand})")

    if _text_field_has_value(page, model_labels, attrs.model):
        print("  verified model")
    else:
        print(f"  WARN model still missing after retries ({attrs.model})")


def _text_field_has_value(page: Page, labels: tuple[str, ...], value: str) -> bool:
    needle = value.strip().lower()
    if not needle:
        return False
    for label in labels:
        for locator in (
            page.get_by_label(re.compile(re.escape(label), re.I)),
            page.locator(f'input[aria-label*="{label}" i]'),
            page.locator(f'textarea[aria-label*="{label}" i]'),
        ):
            try:
                if locator.count() == 0:
                    continue
                field = locator.first
                if not field.is_visible():
                    continue
                current = (field.input_value(timeout=1_000) or field.inner_text(timeout=1_000) or "").strip()
                if needle in current.lower():
                    return True
            except Exception:
                continue
    return False


def _combobox_has_value(page: Page, labels: tuple[str, ...], value: str) -> bool:
    box = _find_combobox(page, labels)
    if box is None:
        return False
    try:
        return box.is_visible() and _combobox_contains_value(box, value)
    except Exception:
        return False


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


def _fill_location(page: Page, city: str) -> bool:
    labels = ("Ubicación", "Location", "Ciudad")
    state = "Chihuahua"
    candidates = (
        city,
        f"{city}, {state}",
        f"{city}, CH",
        f"{city}, Chihuahua, Mexico",
        f"{city}, Chihuahua, MX",
        f"{city}, Chihuahua",
    )
    for locator in (
        page.locator('input[role="combobox"][aria-label="Ubicación"]'),
        page.locator('input[role="combobox"][aria-label*="Ubicación" i]'),
        page.locator('input[role="combobox"][aria-label*="Location" i]'),
        page.locator('[role="combobox"][aria-label="Ubicación"]'),
        page.locator('[role="combobox"][aria-label*="Ubicación" i]'),
    ):
        try:
            if locator.count() == 0:
                continue
            box = locator.first
            if not box.is_visible():
                continue
            if _location_is_filled(page, city):
                return True
            box.scroll_into_view_if_needed()
            box.click()
            page.wait_for_timeout(500)
            box.fill(city)
            page.wait_for_timeout(2_000)
            option = page.get_by_role("option").filter(
                has_text=re.compile(rf"^{re.escape(city)}\b", re.I)
            )
            if option.count() and option.first.is_visible():
                option.first.click(force=True)
                page.wait_for_timeout(1_000)
                if _location_is_filled(page, city):
                    print(f"  location set: {city}")
                    return True
            for candidate in candidates:
                option = page.get_by_role("option").filter(
                    has_text=re.compile(re.escape(candidate.split(",")[0]), re.I)
                )
                if option.count() and option.first.is_visible():
                    option.first.click()
                    page.wait_for_timeout(800)
                    if _location_is_filled(page, city):
                        print(f"  location set: {candidate}")
                        return True
            page.keyboard.press("ArrowDown")
            page.wait_for_timeout(200)
            page.keyboard.press("Enter")
            page.wait_for_timeout(800)
            if _location_is_filled(page, city):
                return True
        except Exception:
            continue
    if _select_from_combobox_list(page, labels, candidates):
        return _location_is_filled(page, city)
    return False


def _location_is_filled(page: Page, city: str) -> bool:
    needle = city.strip().lower()
    if not needle:
        return True
    for locator in (
        page.locator('input[role="combobox"][aria-label="Ubicación"]'),
        page.locator('input[role="combobox"][aria-label*="Ubicación" i]'),
        page.locator('input[role="combobox"][aria-label*="Location" i]'),
    ):
        try:
            if locator.count() == 0:
                continue
            val = (locator.first.input_value(timeout=1_000) or "").strip()
            low = val.lower()
            if not val or low in ("ubicación", "location", "ciudad"):
                continue
            # Typed-only city name is not enough — FB requires picking a suggestion.
            if low == needle:
                continue
            if needle in low:
                return True
        except Exception:
            continue
    return False


def _input_near_label_has_value(page: Page, label_text: str, value: str) -> bool:
    needle = value.strip().lower()
    if not needle:
        return False
    try:
        result = page.evaluate(
            """([labelText, needle]) => {
                let anchor = null;
                for (const el of document.querySelectorAll('span, label, div')) {
                  if ((el.textContent || '').trim() !== labelText) continue;
                  const r = el.getBoundingClientRect();
                  if (r.width <= 0 || r.height <= 0) continue;
                  anchor = { top: r.top, left: r.left };
                  break;
                }
                if (!anchor) return false;
                for (const input of document.querySelectorAll('input')) {
                  const r = input.getBoundingClientRect();
                  if (r.width < 40 || r.height < 10) continue;
                  const dy = r.top - anchor.top;
                  if (dy < -5 || dy > 120) continue;
                  const val = (input.value || '').trim().toLowerCase();
                  if (val && val.includes(needle)) return true;
                }
                return false;
            }""",
            [label_text, needle],
        )
        return bool(result)
    except Exception:
        return False


def _fill_input_near_label(page: Page, label_text: str, value: str) -> bool:
    try:
        result = page.evaluate(
            """([labelText, value]) => {
                const setter = Object.getOwnPropertyDescriptor(
                  window.HTMLInputElement.prototype, 'value'
                )?.set;
                let anchor = null;
                for (const el of document.querySelectorAll('span, label, div')) {
                  const raw = (el.textContent || '').trim();
                  if (raw !== labelText) continue;
                  const r = el.getBoundingClientRect();
                  if (r.width <= 0 || r.height <= 0) continue;
                  anchor = { top: r.top, left: r.left };
                  break;
                }
                if (!anchor) return { ok: false };
                let best = null;
                let bestDist = Infinity;
                for (const input of document.querySelectorAll('input')) {
                  const r = input.getBoundingClientRect();
                  if (r.width < 40 || r.height < 10) continue;
                  const type = (input.type || '').toLowerCase();
                  if (type === 'file' || type === 'hidden' || type === 'checkbox') continue;
                  const dy = r.top - anchor.top;
                  const dx = Math.abs(r.left - anchor.left);
                  if (dy < -5 || dy > 120) continue;
                  const dist = dy * 3 + dx;
                  if (dist < bestDist) {
                    bestDist = dist;
                    best = input;
                  }
                }
                if (!best) return { ok: false };
                best.focus();
                if (setter) setter.call(best, value);
                else best.value = value;
                best.dispatchEvent(new Event('input', { bubbles: true }));
                best.dispatchEvent(new Event('change', { bubbles: true }));
                return { ok: true, id: best.id || '', value: best.value || '' };
            }""",
            [label_text, value],
        )
    except Exception:
        return False
    if result and result.get("ok"):
        return True
    return False


def _fill_price(page: Page, digits: str) -> bool:
    if not digits:
        return False
    strategies = (
        _fill_price_empty_input_scan,
        _fill_price_click_label,
        _fill_price_after_mileage_tab,
        _fill_price_after_model,
        lambda p, d: _fill_numeric_field(p, ("Precio", "Price"), d),
        _fill_price_near_label,
        _fill_price_via_js,
    )
    for strategy in strategies:
        try:
            if strategy(page, digits) and _price_is_filled(page, digits):
                return True
        except Exception:
            continue
    return _fill_vehicle_field(page, ("Precio", "Price"), digits) and _price_is_filled(page, digits)


def _fill_price_empty_input_scan(page: Page, digits: str) -> bool:
    """Find the empty price input (not model/location/mileage) and type digits."""
    try:
        count = page.locator('input[type="text"]').count()
        for i in range(count):
            el = page.locator('input[type="text"]').nth(i)
            if not el.is_visible():
                continue
            al = (el.get_attribute("aria-label") or "").lower()
            if any(k in al for k in ("search", "ubic", "location", "buscar")):
                continue
            val = (el.input_value(timeout=500) or "").strip()
            stripped = re.sub(r"[^\d]", "", val)
            if re.search(r"[a-zA-Z]{2,}", val):
                continue  # model
            if stripped and stripped not in ("", "0") and len(stripped) >= 4 and not val.startswith("$"):
                if not val.startswith("$") and digits not in stripped:
                    continue  # mileage or other numeric
            if val.startswith("$"):
                el.click()
            elif not val:
                el.click()
            else:
                continue
            page.wait_for_timeout(300)
            page.keyboard.press("Control+a")
            page.keyboard.press("Backspace")
            page.wait_for_timeout(200)
            page.keyboard.type(digits, delay=50)
            page.wait_for_timeout(500)
            page.keyboard.press("Tab")
            page.wait_for_timeout(400)
            if _price_is_filled(page, digits):
                print("  price via empty-input scan")
                return True
    except Exception:
        pass
    return False


def _fill_price_click_label(page: Page, digits: str) -> bool:
    for label in ("Precio", "Price"):
        try:
            anchor = page.get_by_text(label, exact=True)
            if anchor.count() == 0 or not anchor.first.is_visible():
                continue
            anchor.first.scroll_into_view_if_needed()
            anchor.first.click()
            page.wait_for_timeout(400)
            page.keyboard.press("Tab")
            page.wait_for_timeout(300)
            page.keyboard.press("Control+a")
            page.keyboard.press("Backspace")
            page.keyboard.type(digits, delay=50)
            page.wait_for_timeout(500)
            page.keyboard.press("Tab")
            page.wait_for_timeout(400)
            if _price_is_filled(page, digits):
                print(f"  price via {label} label click")
                return True
        except Exception:
            continue
    return False


def _fill_price_after_mileage_tab(page: Page, digits: str) -> bool:
    """From a filled mileage field, Tab once to price and type."""
    try:
        for i in range(page.locator('input[type="text"]').count()):
            el = page.locator('input[type="text"]').nth(i)
            if not el.is_visible():
                continue
            val = re.sub(r"[^\d]", "", el.input_value(timeout=500) or "")
            if not (4 <= len(val) <= 7):
                continue
            el.click()
            page.wait_for_timeout(300)
            page.keyboard.press("Tab")
            page.wait_for_timeout(400)
            page.keyboard.press("Control+a")
            page.keyboard.press("Backspace")
            page.keyboard.type(digits, delay=50)
            page.wait_for_timeout(500)
            page.keyboard.press("Tab")
            page.wait_for_timeout(400)
            if _price_is_filled(page, digits):
                print("  price via Tab after mileage")
                return True
            return False
    except Exception:
        pass
    return False


def _refill_price_and_mileage_keyboard(page: Page, vehicle: Vehicle, attrs: ListingAttributes) -> bool:
    price_digits = parse_mxn_price(vehicle.price)
    mileage_digits = _normalize_odometer_digits(attrs.mileage_km)
    mileage_ok = _fill_mileage_keyboard(page, mileage_digits)
    price_ok = (
        _fill_price_empty_input_scan(page, price_digits)
        or _fill_price_click_label(page, price_digits)
        or _fill_price_after_mileage_tab(page, price_digits)
        or _fill_price_after_model(page, price_digits)
    )
    return price_ok and mileage_ok


def _fill_mileage_keyboard(page: Page, digits: str) -> bool:
    digits = _normalize_odometer_digits(digits)
    model_labels = ("Modelo", "Model")
    for label in model_labels:
        try:
            field = page.get_by_label(label)
            if field.count() == 0:
                field = page.locator(f'input[aria-label*="{label}" i]')
            if field.count() == 0 or not field.first.is_visible():
                continue
            field.first.scroll_into_view_if_needed()
            field.first.click()
            page.wait_for_timeout(400)
            page.keyboard.press("Tab")
            page.wait_for_timeout(300)
            page.keyboard.press("Control+a")
            page.keyboard.press("Backspace")
            page.wait_for_timeout(200)
            page.keyboard.type(digits, delay=50)
            page.wait_for_timeout(500)
            page.keyboard.press("Tab")
            page.wait_for_timeout(300)
            inputs = page.evaluate(
                """() => [...document.querySelectorAll('input')].map(el => el.value || '')"""
            )
            if any(digits in re.sub(r"[^\d]", "", val) for val in inputs):
                print(f"  mileage via keyboard ({digits})")
                return True
        except Exception:
            continue
    return False


def _fill_price_near_label(page: Page, digits: str) -> bool:
    try:
        result = page.evaluate(
            """(digits) => {
                const setter = Object.getOwnPropertyDescriptor(
                  window.HTMLInputElement.prototype, 'value'
                )?.set;
                const labels = ['Precio', 'Price'];
                let anchor = null;
                for (const el of document.querySelectorAll('span, label, div')) {
                  const raw = (el.textContent || '').trim();
                  if (!labels.includes(raw)) continue;
                  const r = el.getBoundingClientRect();
                  if (r.width <= 0 || r.height <= 0) continue;
                  anchor = { top: r.top, left: r.left };
                  break;
                }
                if (!anchor) return { ok: false };
                const skip = (al) => {
                  al = (al || '').toLowerCase();
                  return /ubic|location|search|marca|make|modelo|model|año|year|kilometraje|mileage/.test(al);
                };
                let best = null;
                let bestDist = Infinity;
                for (const input of document.querySelectorAll('input')) {
                  const r = input.getBoundingClientRect();
                  if (r.width < 40 || r.height < 10) continue;
                  if (skip(input.getAttribute('aria-label'))) continue;
                  const type = (input.type || '').toLowerCase();
                  if (type === 'file' || type === 'hidden' || type === 'checkbox') continue;
                  const dy = r.top - anchor.top;
                  const dx = Math.abs(r.left - anchor.left);
                  if (dy < -5 || dy > 120) continue;
                  const dist = dy * 3 + dx;
                  if (dist < bestDist) {
                    bestDist = dist;
                    best = input;
                  }
                }
                if (!best) return { ok: false };
                best.focus();
                if (setter) setter.call(best, digits);
                else best.value = digits;
                best.dispatchEvent(new Event('input', { bubbles: true }));
                best.dispatchEvent(new Event('change', { bubbles: true }));
                return { ok: true, id: best.id || '', ariaLabel: best.getAttribute('aria-label') || '' };
            }""",
            digits,
        )
    except Exception:
        return False
    if result and result.get("ok"):
        print(
            f"  price near label "
            f"(id={result.get('id', '?')}, label={result.get('ariaLabel', '')!r})"
        )
        return True
    return False


def _fill_price_via_js(page: Page, digits: str) -> bool:
    try:
        result = page.evaluate(
            """(digits) => {
                const keywords = ['precio', 'price'];
                const skipLabels = [
                  'ubic', 'location', 'ciudad', 'search', 'buscar',
                  'marca', 'make', 'modelo', 'model', 'año', 'year',
                  'kilometraje', 'mileage', 'odometer',
                ];
                const setter = Object.getOwnPropertyDescriptor(
                  window.HTMLInputElement.prototype, 'value'
                )?.set;
                const visible = (el) => {
                  const r = el.getBoundingClientRect();
                  return r.width > 20 && r.height > 10;
                };
                const shouldSkip = (al) => {
                  al = (al || '').toLowerCase();
                  return skipLabels.some((k) => al.includes(k));
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
                for (const input of document.querySelectorAll('input, [role="spinbutton"]')) {
                  if (!visible(input)) continue;
                  const al = (input.getAttribute('aria-label') || '').toLowerCase();
                  if (shouldSkip(al)) continue;
                  if (keywords.some((k) => al.includes(k))) {
                    apply(input);
                    return { ok: true, id: input.id || '', ariaLabel: al };
                  }
                }
                for (const label of document.querySelectorAll('label, span')) {
                  const raw = (label.textContent || '').trim();
                  if (raw !== 'Precio' && raw !== 'Price') continue;
                  const field = label.closest('label')?.querySelector('input')
                    || label.parentElement?.querySelector('input')
                    || label.nextElementSibling?.querySelector?.('input');
                  if (field && visible(field) && !shouldSkip(field.getAttribute('aria-label'))) {
                    apply(field);
                    return { ok: true, id: field.id || '', ariaLabel: 'Precio-label' };
                  }
                }
                for (const input of document.querySelectorAll('input, [role="spinbutton"]')) {
                  if (!visible(input)) continue;
                  const al = (input.getAttribute('aria-label') || '').toLowerCase();
                  if (al || shouldSkip(al)) continue;
                  let node = input.parentElement;
                  for (let depth = 0; depth < 6 && node; depth++) {
                    const text = (node.textContent || '').trim().toLowerCase();
                    if (text === 'precio' || text === 'price' || text.startsWith('precio\\n') || text.startsWith('price\\n')) {
                      apply(input);
                      return { ok: true, id: input.id || '', ariaLabel: 'Precio-near' };
                    }
                    node = node.parentElement;
                  }
                }
                return { ok: false };
            }""",
            digits,
        )
    except Exception:
        return False
    if result and result.get("ok"):
        print(
            f"  price via JS "
            f"(id={result.get('id', '?')}, label={result.get('ariaLabel', '')!r})"
        )
        return True
    return False


def _fill_price_after_model(page: Page, digits: str) -> bool:
    """Tab from Model field — price follows model/mileage in the composer."""
    model_labels = ("Modelo", "Model")
    for label in model_labels:
        try:
            field = page.get_by_label(label)
            if field.count() == 0:
                field = page.locator(f'input[aria-label*="{label}" i]')
            if field.count() == 0:
                continue
            target = field.first
            if not target.is_visible():
                continue
            target.scroll_into_view_if_needed()
            target.click()
            page.wait_for_timeout(400)
            for _ in range(4):
                page.keyboard.press("Tab")
                page.wait_for_timeout(300)
            page.keyboard.press("Control+a")
            page.keyboard.press("Backspace")
            page.wait_for_timeout(200)
            page.keyboard.type(digits, delay=50)
            page.wait_for_timeout(500)
            page.keyboard.press("Tab")
            page.wait_for_timeout(500)
            if _price_is_filled(page, digits):
                print("  price via Tab after model")
                return True
        except Exception:
            continue
    return False


def _fill_mileage(page: Page, value: str) -> bool:
    digits = _normalize_odometer_digits(value)
    labels = (
        "Mileage", "Kilometraje", "Odometer", "Vehicle mileage",
        "Kilometers", "Kilómetros",
    )

    if _fill_mileage_keyboard(page, digits):
        return True
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
