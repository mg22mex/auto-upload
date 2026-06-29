from __future__ import annotations

import re
import time

from playwright.sync_api import Locator, Page

from src.facebook.errors import FacebookPostingError

NEXT_LABELS = ("Siguiente", "Next", "Continuar", "Continue")
PUBLISH_LABELS = (
    "Publicar",
    "Publish",
    "Publicar artículo",
    "List item",
    "Publish listing",
    "Publicar anuncio",
)
DISMISS_LABELS = (
    "Ahora no",
    "Not now",
    "Cerrar",
    "Close",
    "Permitir todas las cookies",
    "Allow all cookies",
    "Aceptar",
    "Accept",
)


def dismiss_overlays(page: Page) -> None:
    for label in DISMISS_LABELS:
        for locator in (
            page.locator(f'[aria-label="{label}"]'),
            page.get_by_role("button", name=label),
        ):
            try:
                if locator.count() and locator.first.is_visible():
                    locator.first.click(timeout=2_000)
                    page.wait_for_timeout(800)
            except Exception:
                continue


def click_labeled_action(
    page: Page,
    labels: tuple[str, ...],
    *,
    timeout_ms: int = 60_000,
    allow_force: bool = False,
) -> None:
    deadline = time.monotonic() + (timeout_ms / 1000)
    last_state = "not found"

    while time.monotonic() < deadline:
        dismiss_overlays(page)
        for label in labels:
            for locator in _labeled_button_locators(page, label):
                try:
                    count = locator.count()
                except Exception:
                    continue
                for index in range(count):
                    button = locator.nth(index)
                    try:
                        if not button.is_visible():
                            continue
                        disabled = button.get_attribute("aria-disabled")
                        if disabled == "true" and not allow_force:
                            last_state = f"{label} disabled"
                            continue
                        if not button.is_enabled() and not allow_force:
                            last_state = f"{label} not enabled"
                            continue
                        button.scroll_into_view_if_needed()
                        button.click(timeout=5_000, force=allow_force)
                        page.wait_for_timeout(1_500)
                        return
                    except Exception as exc:
                        last_state = f"{label}: {exc}"
        page.wait_for_timeout(2_000)

    raise FacebookPostingError(
        f"Timed out waiting for button ({', '.join(labels)}): {last_state}"
    )


def advance_past_photo_step(page: Page, *, timeout_ms: int = 90_000) -> None:
    """Click Next after photo upload. FB often keeps aria-disabled=true while still accepting clicks."""
    wait_for_photo_previews(page, min_count=1, timeout_ms=120_000)
    page.wait_for_timeout(3_000)

    deadline = time.monotonic() + (timeout_ms / 1000)
    last_state = "no click attempted"

    while time.monotonic() < deadline:
        if _advanced_past_photo_step(page):
            return

        for label in NEXT_LABELS:
            for locator in _next_button_locators(page, label):
                try:
                    if locator.count() == 0:
                        continue
                    button = locator.last
                    if not button.is_visible():
                        continue
                    button.scroll_into_view_if_needed()
                    try:
                        button.click(timeout=5_000)
                    except Exception:
                        button.click(timeout=5_000, force=True)
                    page.wait_for_timeout(2_000)
                    if _advanced_past_photo_step(page):
                        return
                    last_state = f"clicked {label} but still on step 1"
                except Exception as exc:
                    last_state = str(exc)

        if _js_click_next(page):
            page.wait_for_timeout(2_000)
            if _advanced_past_photo_step(page):
                return
            last_state = "js click did not advance"

        page.wait_for_timeout(2_000)

    raise FacebookPostingError(f"Could not advance past photo step: {last_state}")


def _advanced_past_photo_step(page: Page) -> bool:
    try:
        body = page.locator("body").inner_text(timeout=3_000)
    except Exception:
        body = ""

    if re.search(r"step 2 of 2|paso 2 de 2", body, re.I):
        return True

    field_patterns = (
        re.compile(r"price|precio", re.I),
        re.compile(r"year|año", re.I),
        re.compile(r"mileage|kilometraje", re.I),
    )
    for pattern in field_patterns:
        if page.get_by_label(pattern).count():
            return True
        if page.get_by_placeholder(pattern).count():
            return True
    return False


def _next_button_locators(page: Page, label: str) -> list[Locator]:
    return [
        page.locator(f'[aria-label="Marketplace Composer"] [aria-label="{label}"]'),
        page.locator(f'[aria-label="{label}"]'),
    ]


def _js_click_next(page: Page) -> bool:
    return bool(
        page.evaluate(
            """() => {
                const labels = ['Next', 'Siguiente', 'Continuar', 'Continue'];
                for (const label of labels) {
                    const nodes = [...document.querySelectorAll(`[aria-label="${label}"]`)];
                    if (!nodes.length) continue;
                    nodes[nodes.length - 1].click();
                    return true;
                }
                return false;
            }"""
        )
    )


def wait_for_photo_previews(page: Page, *, min_count: int = 1, timeout_ms: int = 120_000) -> None:
    try:
        page.wait_for_function(
            """(minCount) => {
                const imgs = document.querySelectorAll(
                  'img[src^="blob:"], img[src*="fbcdn.net"], img[src*="scontent"]'
                );
                return imgs.length >= minCount;
            }""",
            arg=min_count,
            timeout=timeout_ms,
        )
    except Exception as exc:
        raise FacebookPostingError(f"Photo previews did not appear: {exc}") from exc


def wait_for_enabled_labeled_button(
    page: Page,
    labels: tuple[str, ...],
    *,
    timeout_ms: int = 180_000,
) -> None:
    """Best-effort wait; Facebook may never clear aria-disabled."""
    label_js = list(labels)
    try:
        page.wait_for_function(
            """(labels) => {
                for (const label of labels) {
                  const nodes = document.querySelectorAll(`[aria-label="${label}"]`);
                  for (const node of nodes) {
                    const rect = node.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) return true;
                  }
                }
                return false;
            }""",
            arg=label_js,
            timeout=timeout_ms,
        )
    except Exception as exc:
        raise FacebookPostingError(f"Button never appeared ({', '.join(labels)}): {exc}") from exc


def log_page_state(page: Page, step: str) -> None:
    try:
        title = page.title()
    except Exception:
        title = "?"
    print(f"FB step [{step}]: {page.url} | {title}")


def disable_promote_listing(page: Page) -> bool:
    """Turn off 'Promote listing after publish' — it blocks the Next button."""
    try:
        turned_off = page.evaluate(
            """() => {
                const words = ['promote listing', 'promocionar', 'promover'];
                for (const sw of document.querySelectorAll('[role="switch"]')) {
                  if (sw.getAttribute('aria-checked') !== 'true') continue;
                  let el = sw.parentElement;
                  for (let depth = 0; depth < 10 && el; depth++) {
                    const text = (el.textContent || '').toLowerCase();
                    if (words.some((w) => text.includes(w))) {
                      sw.click();
                      return true;
                    }
                    el = el.parentElement;
                  }
                }
                return false;
            }"""
        )
    except Exception:
        return False
    if turned_off:
        print("  disabled Promote listing toggle")
        page.wait_for_timeout(1_000)
    return bool(turned_off)


def advance_composer_next(page: Page, *, timeout_ms: int = 90_000) -> None:
    """Click Next/Siguiente on composer steps (review, etc.)."""
    deadline = time.monotonic() + (timeout_ms / 1000)
    last_state = "no click attempted"

    while time.monotonic() < deadline:
        disable_promote_listing(page)
        for label in NEXT_LABELS:
            for locator in _next_button_locators(page, label):
                try:
                    if locator.count() == 0:
                        continue
                    button = locator.last
                    if not button.is_visible():
                        continue
                    if button.get_attribute("aria-disabled") == "true":
                        last_state = f"{label} disabled"
                        continue
                    if not button.is_enabled():
                        last_state = f"{label} not enabled"
                        continue
                    button.scroll_into_view_if_needed()
                    button.click(timeout=5_000)
                    page.wait_for_timeout(2_000)
                    return
                except Exception as exc:
                    last_state = str(exc)
                    try:
                        button.click(timeout=5_000, force=True)
                        page.wait_for_timeout(2_000)
                        return
                    except Exception:
                        pass
        if _js_click_next(page):
            page.wait_for_timeout(2_000)
            return
        page.wait_for_timeout(2_000)

    raise FacebookPostingError(f"Could not click composer Next: {last_state}")


def _labeled_button_locators(page: Page, label: str) -> list[Locator]:
    pattern = re.compile(rf"^\s*{re.escape(label)}\s*$", re.I)
    return [
        page.locator(f'[aria-label="{label}"]'),
        page.locator(f'div[role="button"][aria-label="{label}"]'),
        page.get_by_role("button", name=label),
        page.get_by_role("button", name=pattern),
        page.locator('[role="button"]').filter(has_text=pattern),
        page.locator(f'span:text-is("{label}")').locator("xpath=ancestor::*[@role='button'][1]"),
    ]
