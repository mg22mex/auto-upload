from __future__ import annotations

import re
import time

from playwright.sync_api import Locator, Page

from src.facebook.errors import FacebookPostingError

NEXT_LABELS = ("Siguiente", "Next", "Continuar", "Continue")
PUBLISH_LABELS = ("Publicar", "Publish", "Publicar artículo", "List item")
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
                        if disabled == "true":
                            last_state = f"{label} disabled"
                            continue
                        if not button.is_enabled():
                            last_state = f"{label} not enabled"
                            continue
                        button.scroll_into_view_if_needed()
                        button.click(timeout=5_000)
                        page.wait_for_timeout(1_500)
                        return
                    except Exception as exc:
                        last_state = f"{label}: {exc}"
        page.wait_for_timeout(2_000)

    raise FacebookPostingError(
        f"Timed out waiting for button ({', '.join(labels)}): {last_state}"
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
    label_js = list(labels)
    try:
        page.wait_for_function(
            """(labels) => {
                for (const label of labels) {
                  const nodes = document.querySelectorAll(`[aria-label="${label}"]`);
                  for (const node of nodes) {
                    if (node.getAttribute('aria-disabled') === 'true') continue;
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
        raise FacebookPostingError(f"Button never became ready ({', '.join(labels)}): {exc}") from exc


def log_page_state(page: Page, step: str) -> None:
    try:
        title = page.title()
    except Exception:
        title = "?"
    print(f"FB step [{step}]: {page.url} | {title}")


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
