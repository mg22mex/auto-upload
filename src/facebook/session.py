from __future__ import annotations

import os
import platform
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright

from src.facebook.errors import FacebookSessionError
from src.facebook.util import env_bool, env_str


def resolve_session_dir(config: dict, account_id: str, root: Path) -> Path:
    for account in config.get("accounts", []):
        if account.get("id") == account_id:
            session_dir = account.get("session_dir", f"sessions/{account_id}")
            return (root / session_dir).resolve()
    raise FacebookSessionError(f"Unknown account id: {account_id}")


def session_health_report(session_dir: Path) -> dict[str, object]:
    """Describe local Chromium profile state (no secrets)."""
    path = Path(session_dir)
    exists = path.is_dir()
    files = 0
    bytes_total = 0
    has_cookies = False
    if exists:
        for child in path.rglob("*"):
            if not child.is_file():
                continue
            # Skip noisy crash pads
            name = child.name
            if name.endswith(".log") or name == "SingletonLock":
                continue
            files += 1
            try:
                bytes_total += child.stat().st_size
            except OSError:
                pass
            if name == "Cookies" or name == "Cookies-journal":
                has_cookies = True
    empty = exists and files == 0
    return {
        "path": str(path),
        "exists": exists,
        "file_count": files,
        "bytes": bytes_total,
        "has_cookies_file": has_cookies,
        "looks_empty": empty or not exists or (exists and files < 3 and not has_cookies),
    }


def format_session_login_error(account_id: str, session_dir: Path) -> str:
    """Human-readable message when Marketplace shows a login wall."""
    health = session_health_report(session_dir)
    hints: list[str] = []
    if not health["exists"]:
        hints.append("session directory missing (never logged in on this host?)")
    elif health["looks_empty"]:
        hints.append("session profile looks empty / incomplete")
    elif not health["has_cookies_file"]:
        hints.append("no Cookies file found under profile (stale or wiped session)")
    else:
        hints.append("cookies present but Facebook still shows login (session expired)")
    hint = "; ".join(hints)
    return (
        f"Not logged in for {account_id}. {hint}. "
        f"Session path: {health['path']} "
        f"(exists={health['exists']}, files={health['file_count']}, "
        f"cookies_file={health['has_cookies_file']}). "
        f"Refresh on fb-worker (headed): "
        f"python scripts/fb_login.py --account {account_id}"
    )


def page_shows_login_form(page: Page) -> bool:
    if "login" in page.url.lower():
        return True
    login_indicators = [
        page.get_by_role("button", name="Log in"),
        page.get_by_role("button", name="Iniciar sesión"),
        page.locator('input[name="email"]'),
        page.locator('input[aria-label="Correo o teléfono"]'),
        page.locator('input[aria-label="Contraseña"]'),
    ]
    for locator in login_indicators:
        try:
            if locator.count() and locator.first.is_visible():
                return True
        except Exception:
            continue
    return False


def is_logged_in(page: Page) -> bool:
    page.goto("https://www.facebook.com/marketplace", wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(2_000)
    return not page_shows_login_form(page)


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


def _session_profile_writable(session_dir: Path) -> bool:
    """True if Chromium can create/update profile files here."""
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        probe = session_dir / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        default = session_dir / "Default"
        if default.is_dir() and not os.access(default, os.W_OK):
            return False
        return True
    except OSError:
        return False


def _default_user_agent() -> str:
    """Match host arch (worker was aarch64; desktop login is usually x86_64)."""
    override = env_str("FB_USER_AGENT", "")
    if override:
        return override
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        arch = "x86_64"
    elif machine in {"aarch64", "arm64"}:
        arch = "aarch64"
    else:
        arch = machine or "x86_64"
    return (
        f"Mozilla/5.0 (X11; Linux {arch}) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )


def _chromium_args(*, headless: bool) -> list[str]:
    """Stable Chromium flags. Headed Plasma/Wayland often SIGTRAP without these."""
    args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--password-store=basic",
    ]
    # SIGTRAP (signal 5) on ms-playwright chromium is frequently GPU/Ozone on Wayland.
    disable_gpu = env_bool("FB_DISABLE_GPU", default=not headless)
    if disable_gpu:
        args.extend(
            [
                "--disable-gpu",
                "--disable-gpu-compositing",
                "--disable-software-rasterizer",
            ]
        )
    if not headless:
        ozone = env_str("FB_OZONE_PLATFORM", "x11")
        if ozone and ozone.lower() not in {"0", "off", "none"}:
            args.append(f"--ozone-platform={ozone}")
    extra = env_str("FB_CHROME_ARGS", "")
    if extra:
        args.extend(part for part in extra.split() if part)
    return args


def launch_persistent_context(
    playwright: Playwright,
    session_dir: Path,
    *,
    headless: bool,
) -> BrowserContext:
    _clear_profile_locks(session_dir)
    kwargs: dict = {
        "user_data_dir": str(session_dir),
        "headless": headless,
        "viewport": {"width": 1400, "height": 900},
        "locale": "es-MX",
        "user_agent": _default_user_agent(),
        "ignore_default_args": ["--enable-automation"],
        "args": _chromium_args(headless=headless),
    }
    # Optional: system browser (Arch: channel=chromium) — more stable than ms-playwright bundle.
    channel = env_str("FB_BROWSER_CHANNEL", "")
    if channel:
        kwargs["channel"] = channel
    return playwright.chromium.launch_persistent_context(**kwargs)


def get_page(context: BrowserContext) -> Page:
    if context.pages:
        return context.pages[0]
    return context.new_page()


def _clear_profile_locks(session_dir: Path) -> None:
    """Remove Chromium lock files so a closed/crashed profile can relaunch."""
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lock = session_dir / name
        try:
            if lock.exists() or lock.is_symlink():
                lock.unlink()
        except OSError:
            pass


def login_interactive(config: dict, account_id: str, *, root: Path) -> None:
    """Headed login. Profile cookies persist under sessions/<account_id>/.

    If the browser window is closed before verification, we re-open the same
    user-data dir (cookies already flushed) and re-check Marketplace login.
    """
    session_dir = resolve_session_dir(config, account_id, root)
    session_dir.mkdir(parents=True, exist_ok=True)
    if not _session_profile_writable(session_dir):
        raise FacebookSessionError(
            f"Session dir not writable: {session_dir} "
            f"(often root-owned after a sandbox run). Fix:\n"
            f"  sudo chown -R \"$USER:$USER\" {session_dir}"
        )

    print(f"Session directory: {session_dir}")
    channel = env_str("FB_BROWSER_CHANNEL", "") or "(playwright chromium)"
    print(f"Browser channel: {channel}")
    print("")
    print("1) A Chromium window will open.")
    print("2) Log into Facebook for this account (Marketplace access).")
    print("3) Wait until you see your feed (or Marketplace) — stay logged in.")
    print("4) Leave the browser open, come back HERE, and press Enter.")
    print("   (If you already closed it after login, we re-open the profile to verify.)")
    print("")

    with sync_playwright() as playwright:
        try:
            context = launch_persistent_context(playwright, session_dir, headless=False)
        except Exception as exc:
            raise FacebookSessionError(
                f"Chromium failed to start ({exc}). "
                "On Arch/KDE try: FB_BROWSER_CHANNEL=chromium FB_OZONE_PLATFORM=x11 "
                "FB_DISABLE_GPU=1 (and clear crash locks with the refresh script)."
            ) from exc
        page = get_page(context)
        try:
            page.goto(
                "https://www.facebook.com/",
                wait_until="domcontentloaded",
                timeout=90_000,
            )
        except Exception as exc:
            print(
                f"WARN: initial navigation failed ({exc}); "
                "continue logging in if the window is up."
            )

        input("Press Enter after you are logged in and can see Facebook...")

        logged_in = False
        try:
            if context.pages:
                logged_in = _verify_marketplace_login(get_page(context))
        except Exception as exc:
            print(f"WARN: live-window verify failed ({exc})")

        try:
            context.close()
        except Exception:
            pass

        if logged_in:
            print("Login OK. Session saved.")
            return

        print("Re-opening profile to verify cookies…")
        _clear_profile_locks(session_dir)
        context = launch_persistent_context(playwright, session_dir, headless=False)
        try:
            if _verify_marketplace_login(get_page(context)):
                print("Login OK. Session saved (verified after relaunch).")
                return
            raise FacebookSessionError(
                "Still not logged in — cookies not saved. "
                "Keep browser open until after you press Enter; "
                "finish any FB checkpoint/2FA first."
            )
        finally:
            try:
                context.close()
            except Exception:
                pass


def _verify_marketplace_login(page: Page) -> bool:
    """Navigate Marketplace and return True if not on a login wall."""
    page.goto(
        "https://www.facebook.com/marketplace",
        wait_until="domcontentloaded",
        timeout=90_000,
    )
    page.wait_for_timeout(2_500)
    return not page_shows_login_form(page)
