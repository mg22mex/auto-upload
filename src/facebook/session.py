from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright

from src.facebook.errors import FacebookSessionError


def resolve_session_dir(config: dict, account_id: str, root: Path) -> Path:
    for account in config.get("accounts", []):
        if account.get("id") == account_id:
            session_dir = account.get("session_dir", f"sessions/{account_id}")
            return (root / session_dir).resolve()
    raise FacebookSessionError(f"Unknown account id: {account_id}")


def is_logged_in(page: Page) -> bool:
    page.goto("https://www.facebook.com/marketplace", wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(2_000)

    if "login" in page.url.lower():
        return False

    login_indicators = [
        page.get_by_role("button", name="Log in"),
        page.get_by_role("button", name="Iniciar sesión"),
        page.locator('input[name="email"]'),
    ]
    for locator in login_indicators:
        try:
            if locator.count() and locator.first.is_visible():
                return False
        except Exception:
            continue

    return True


@contextmanager
def open_account_context(
    config: dict,
    account_id: str,
    *,
    root: Path,
    headless: bool,
) -> Iterator[BrowserContext]:
    session_dir = resolve_session_dir(config, account_id, root)
    session_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = launch_persistent_context(playwright, session_dir, headless=headless)
        try:
            yield context
        finally:
            context.close()


def launch_persistent_context(
    playwright: Playwright,
    session_dir: Path,
    *,
    headless: bool,
) -> BrowserContext:
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(session_dir),
        headless=headless,
        viewport={"width": 1400, "height": 900},
        locale="es-MX",
        user_agent=(
            "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        ignore_default_args=["--enable-automation"],
        args=["--disable-blink-features=AutomationControlled"],
    )


def get_page(context: BrowserContext) -> Page:
    if context.pages:
        return context.pages[0]
    return context.new_page()


def login_interactive(config: dict, account_id: str, *, root: Path) -> None:
    session_dir = resolve_session_dir(config, account_id, root)
    session_dir.mkdir(parents=True, exist_ok=True)

    print(f"Session directory: {session_dir}")
    print("Opening Facebook in a headed browser. Log in, then return here and press Enter.")

    with sync_playwright() as playwright:
        context = launch_persistent_context(playwright, session_dir, headless=False)
        page = get_page(context)
        page.goto("https://www.facebook.com/", wait_until="domcontentloaded")
        input("Press Enter after you are logged in and see your feed...")
        page.goto("https://www.facebook.com/marketplace", wait_until="domcontentloaded")
        if not is_logged_in(page):
            context.close()
            raise FacebookSessionError("Still not logged in — try again.")
        print("Login OK. Session saved.")
        context.close()
