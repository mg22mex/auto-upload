"""Session health pre-check run before any Playwright posting/repost work.

Offline stage inspects the persistent Chromium profile; the optional live stage
opens Marketplace once per account to catch logged-out sessions and security
checkpoints before a long automation loop burns time (or trips Facebook).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.facebook.session import (
    detect_session_state,
    format_session_login_error,
    open_account_context,
    resolve_session_dir,
    session_health_report,
)
from src.facebook.util import env_bool

STATE_OK = "ok"
STATE_LOGGED_OUT = "logged_out"
STATE_CHECKPOINT = "checkpoint"
STATE_NO_SESSION = "no_session"
STATE_ERROR = "error"


@dataclass
class AccountHealth:
    account_id: str
    state: str
    detail: str
    session_path: str
    checked_live: bool = False

    @property
    def healthy(self) -> bool:
        return self.state == STATE_OK

    def line(self) -> str:
        flag = "OK" if self.healthy else self.state.upper()
        scope = "live" if self.checked_live else "profile"
        return f"  [{flag}] {self.account_id} ({scope}): {self.detail}"


def live_precheck_enabled() -> bool:
    """``FB_SESSION_PRECHECK_LIVE`` toggle (default on)."""
    return env_bool("FB_SESSION_PRECHECK_LIVE", True)


def check_profile(config: dict, account_id: str, root: Path) -> AccountHealth:
    """Offline profile inspection — no browser launch."""
    session_dir = resolve_session_dir(config, account_id, root)
    report = session_health_report(session_dir)
    if report["looks_empty"]:
        return AccountHealth(
            account_id=account_id,
            state=STATE_NO_SESSION,
            detail=format_session_login_error(account_id, session_dir),
            session_path=str(report["path"]),
        )
    return AccountHealth(
        account_id=account_id,
        state=STATE_OK,
        detail=(
            f"profile ok (files={report['file_count']}, "
            f"cookies_file={report['has_cookies_file']})"
        ),
        session_path=str(report["path"]),
    )


def check_live(
    config: dict,
    account_id: str,
    root: Path,
    *,
    headless: bool | None = None,
) -> AccountHealth:
    """Open Marketplace once and classify the session state."""
    session_dir = resolve_session_dir(config, account_id, root)
    if headless is None:
        headless = env_bool("FB_HEADLESS", True)
    try:
        with open_account_context(config, account_id, root=root, headless=headless) as ctx:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(
                "https://www.facebook.com/marketplace",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            page.wait_for_timeout(2_000)
            state = detect_session_state(page)
    except Exception as exc:
        return AccountHealth(
            account_id=account_id,
            state=STATE_ERROR,
            detail=f"session probe failed: {type(exc).__name__}: {exc}",
            session_path=str(session_dir),
            checked_live=True,
        )

    if state == STATE_CHECKPOINT:
        detail = (
            "Facebook security checkpoint / 2FA is blocking automation. "
            f"Clear it headed on fb-worker: python scripts/fb_login.py --account {account_id}"
        )
    elif state == STATE_LOGGED_OUT:
        detail = format_session_login_error(account_id, session_dir)
    else:
        detail = "logged in (Marketplace reachable)"
    return AccountHealth(
        account_id=account_id,
        state=state,
        detail=detail,
        session_path=str(session_dir),
        checked_live=True,
    )


def precheck_accounts(
    config: dict,
    account_ids: list[str],
    root: Path,
    *,
    live: bool | None = None,
    headless: bool | None = None,
) -> list[AccountHealth]:
    """Profile check for every account; live probe for those that pass it."""
    if live is None:
        live = live_precheck_enabled()
    results: list[AccountHealth] = []
    for account_id in account_ids:
        result = check_profile(config, account_id, root)
        if result.healthy and live:
            result = check_live(config, account_id, root, headless=headless)
        results.append(result)
    return results


def format_health_report(results: list[AccountHealth]) -> str:
    lines = ["Session health pre-check:"]
    lines.extend(result.line() for result in results)
    return "\n".join(lines)


def unhealthy(results: list[AccountHealth]) -> list[AccountHealth]:
    return [result for result in results if not result.healthy]


__all__ = [
    "AccountHealth",
    "STATE_CHECKPOINT",
    "STATE_ERROR",
    "STATE_LOGGED_OUT",
    "STATE_NO_SESSION",
    "STATE_OK",
    "check_live",
    "check_profile",
    "format_health_report",
    "live_precheck_enabled",
    "precheck_accounts",
    "unhealthy",
]
