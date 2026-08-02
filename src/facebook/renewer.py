from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Locator, Page

from src.facebook.browser_health import is_browser_dead
from src.facebook.errors import FacebookAutomationError, FacebookPostingError, FacebookSessionError
from src.facebook.poster import _save_debug
from src.facebook.session import get_page, is_logged_in, open_account_context, page_shows_login_form
from src.facebook.ui import dismiss_overlays
from src.facebook.util import ensure_log_dir, env_bool, env_float, env_int, random_delay
from src.models import SyncAction, Vehicle
from src.store.db import SyncStore

DASHBOARD_URL = "https://www.facebook.com/marketplace/you/dashboard"
SELLING_URL = "https://www.facebook.com/marketplace/you/selling"


@dataclass
class RenewResult:
    renews: int = 0
    errors: list[str] = None  # type: ignore[assignment]
    browser_reopens: int = 0

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


def execute_renews(
    actions: list[SyncAction],
    store: SyncStore,
    config: dict,
    *,
    root: Path,
    account_order: list[str] | None = None,
) -> RenewResult:
    if not actions:
        return RenewResult()

    fb_config = config.get("facebook", {})
    renew_cfg = config.get("sync", {}).get("renew", {}) or {}
    headless = env_bool("FB_HEADLESS", bool(fb_config.get("headless", True)))
    delay_min = env_float("FB_RENEW_DELAY_MIN_SEC", 15.0, "FB_ACTION_DELAY_MIN_SEC")
    delay_max = env_float("FB_RENEW_DELAY_MAX_SEC", 30.0, "FB_ACTION_DELAY_MAX_SEC")
    restart_every = env_int(
        "FB_RENEW_BROWSER_EVERY",
        int(renew_cfg.get("restart_browser_every", 10)),
    )
    max_reopens = env_int(
        "FB_BROWSER_REOPEN_MAX",
        int(renew_cfg.get("max_browser_reopens", 5)),
    )
    log_dir = ensure_log_dir(root / "data" / "logs" / "facebook")

    by_account: dict[str, list[SyncAction]] = defaultdict(list)
    for action in actions:
        if action.action != "renew" or not action.account_id:
            continue
        by_account[action.account_id].append(action)

    result = RenewResult()
    ordered_accounts = account_order or list(by_account.keys())

    for account_id in ordered_accounts:
        remaining = list(by_account.get(account_id) or [])
        if not remaining:
            continue
        print(f"Renew: processing {len(remaining)} listing(s) for {account_id}")
        reopens = 0

        while remaining:
            reopen_requested = False
            try:
                with open_account_context(
                    config,
                    account_id,
                    root=root,
                    headless=headless,
                ) as context:
                    page = get_page(context)
                    if not is_logged_in(page):
                        raise FacebookSessionError(
                            f"Not logged in for {account_id}. "
                            f"Run: python scripts/fb_login.py --account {account_id}"
                        )

                    if not _open_renew_dialog(page):
                        raise FacebookPostingError(
                            "Could not open Renew listings dialog (To renew)"
                        )

                    done_in_session = 0
                    while remaining:
                        action = remaining[0]
                        if page_shows_login_form(page):
                            raise FacebookSessionError(
                                f"Session expired for {account_id}. "
                                f"Run: python scripts/fb_login.py --account {account_id}"
                            )
                        try:
                            _renew_one_in_dialog(
                                page,
                                action,
                                store,
                                log_dir=log_dir,
                                result=result,
                            )
                            remaining.pop(0)
                            done_in_session += 1
                        except Exception as exc:
                            if is_browser_dead(exc):
                                print(
                                    f"WARN: renew {action.autosell_id} on {account_id}: "
                                    f"browser died ({exc}); will reopen and retry"
                                )
                                reopen_requested = True
                                break
                            msg = f"renew {action.autosell_id} on {account_id}: {exc}"
                            print(f"ERROR: {msg}")
                            result.errors.append(msg)
                            remaining.pop(0)
                            if not _dialog_is_open(page):
                                _open_renew_dialog(page)

                        if remaining and not reopen_requested:
                            random_delay(delay_min, delay_max)

                        if (
                            restart_every > 0
                            and done_in_session >= restart_every
                            and remaining
                            and not reopen_requested
                        ):
                            print(
                                f"Renew: restarting browser after {done_in_session} "
                                f"listing(s) for {account_id} "
                                f"({len(remaining)} left)"
                            )
                            break

                    if not reopen_requested:
                        _close_dialog(page)
            except FacebookSessionError as exc:
                result.errors.append(str(exc))
                break
            except Exception as exc:
                if is_browser_dead(exc):
                    reopen_requested = True
                    print(
                        f"WARN: renew on {account_id}: browser died ({exc}); "
                        f"will reopen and retry"
                    )
                else:
                    result.errors.append(f"{account_id}: {exc}")
                    break

            if not remaining:
                break
            if not reopen_requested and restart_every > 0:
                continue
            if reopen_requested:
                reopens += 1
                result.browser_reopens += 1
                if reopens > max_reopens:
                    result.errors.append(
                        f"{account_id}: too many browser reopens ({reopens}); "
                        f"{len(remaining)} listing(s) left"
                    )
                    break
                print(
                    f"Renew: reopening browser for {account_id} "
                    f"({reopens}/{max_reopens}, {len(remaining)} left)"
                )
                continue
            break

    return result


def _renew_one_in_dialog(
    page: Page,
    action: SyncAction,
    store: SyncStore,
    *,
    log_dir: Path,
    result: RenewResult,
) -> None:
    if not action.vehicle:
        raise FacebookAutomationError("Renew action missing vehicle payload")

    if not _dialog_is_open(page) and not _open_renew_dialog(page):
        raise FacebookPostingError("Renew listings dialog not open")

    try:
        if not _click_renew_for_vehicle(page, action.vehicle):
            raise FacebookPostingError(
                f"Listing not found in Renew dialog or Renew disabled "
                f"({action.vehicle.marketplace_title})"
            )
    except Exception:
        _save_debug(page, log_dir, action.autosell_id, "renew_failed")
        raise

    page.wait_for_timeout(1_500)
    store.touch_posted_at(action.autosell_id, action.account_id or "")
    result.renews += 1
    print(f"Renewed {action.autosell_id} on {action.account_id} (URL unchanged)")


def _open_renew_dialog(page: Page) -> bool:
    page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_timeout(3_000)
    dismiss_overlays(page)

    link = page.get_by_role("link", name=re.compile(r"to renew|para renovar", re.I))
    if link.count() == 0:
        page.goto(
            "https://www.facebook.com/marketplace/selling/renew_listings/?is_routable_dialog=true",
            wait_until="domcontentloaded",
            timeout=90_000,
        )
        page.wait_for_timeout(4_000)
    else:
        try:
            link.first.click(timeout=10_000)
        except Exception:
            page.goto(
                "https://www.facebook.com/marketplace/selling/renew_listings/?is_routable_dialog=true",
                wait_until="domcontentloaded",
                timeout=90_000,
            )
        page.wait_for_timeout(4_000)

    # Do NOT dismiss_overlays here — FB "Close" would dismiss the renew dialog.
    if _renew_buttons(page).count() > 0:
        return True
    page.wait_for_timeout(3_000)
    return _renew_buttons(page).count() > 0 or _dialog_is_open(page)


def _dialog_is_open(page: Page) -> bool:
    try:
        if page.get_by_text(re.compile(r"renew listings|renovar publicaciones", re.I)).count():
            return True
        return _renew_buttons(page).count() > 0
    except Exception:
        return False


def _renew_buttons(page: Page) -> Locator:
    return page.get_by_role("button", name=re.compile(r"^\s*renew\s*$|^\s*renovar\s*$", re.I))


def _click_renew_for_vehicle(page: Page, vehicle: Vehicle) -> bool:
    needles = _match_needles(vehicle)
    for _ in range(25):
        for needle in needles:
            row = _find_renew_row(page, needle)
            if row is None:
                continue
            btn = row.get_by_role("button", name=re.compile(r"^\s*renew\s*$|^\s*renovar\s*$", re.I))
            if btn.count() == 0:
                btn = row.locator('[role="button"]').filter(
                    has_text=re.compile(r"^\s*renew\s*$|^\s*renovar\s*$", re.I)
                )
            if btn.count() == 0:
                continue
            target = btn.first
            try:
                if not target.is_visible():
                    continue
                disabled = target.get_attribute("aria-disabled")
                if disabled == "true":
                    continue
                target.scroll_into_view_if_needed()
                target.click(timeout=5_000)
                page.wait_for_timeout(2_000)
                return True
            except Exception:
                continue
        _scroll_renew_list(page)
    return False


def _find_renew_row(page: Page, needle: str) -> Locator | None:
    """Return a locator for a renew-list row containing needle + Renew button."""
    pattern = re.compile(re.escape(needle), re.I)
    # Prefer compact rows that include both the title and Renew
    candidates = page.locator("div").filter(has_text=pattern).filter(
        has=page.get_by_role("button", name=re.compile(r"^renew$|^renovar$", re.I))
    )
    try:
        count = candidates.count()
    except Exception:
        return None
    if count == 0:
        return None
    # Smallest-ish: take the last matching nested container (usually the row)
    # Prefer ones whose text is short (row-level, not the whole dialog)
    best: Locator | None = None
    best_len = 10_000
    for i in range(min(count, 15)):
        cand = candidates.nth(i)
        try:
            text = (cand.inner_text(timeout=800) or "").strip()
        except Exception:
            continue
        # Row should look like: title + price + Renew
        if "\n" in text and len(text) < best_len and len(text) < 400:
            best = cand
            best_len = len(text)
    return best or candidates.first


def _scroll_renew_list(page: Page) -> None:
    try:
        dialog = page.locator('[role="dialog"]').first
        if dialog.count() and dialog.is_visible():
            dialog.evaluate("el => { el.scrollTop = (el.scrollTop || 0) + 400; }")
            page.wait_for_timeout(800)
            return
    except Exception:
        pass
    page.mouse.wheel(0, 800)
    page.wait_for_timeout(800)


def _close_dialog(page: Page) -> None:
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(800)
    except Exception:
        pass


def _match_needles(vehicle: Vehicle) -> list[str]:
    """Ordered search strings for matching a listing row in the Renew dialog."""
    needles: list[str] = []
    title = (vehicle.title or "").strip()
    brand = (vehicle.brand or "").strip()
    year = (vehicle.year or "").strip()
    full = vehicle.marketplace_title.strip()

    def add(value: str) -> None:
        value = value.strip()
        if value and value not in needles and len(value) >= 2:
            needles.append(value)

    add(full)
    if title:
        add(title)
        add(title.replace(" ", ""))  # "A 3" -> "A3"
        add(re.sub(r"\s+", " ", title))
    if year and brand and title:
        add(f"{year} {brand} {title}")
        add(f"{year} {brand} {title.replace(' ', '')}")
    if brand and title:
        add(f"{brand} {title}")
        add(f"{brand} {title.replace(' ', '')}")
    if brand:
        add(brand)
    return needles

