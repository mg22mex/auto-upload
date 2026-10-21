"""Session health pre-check run before any Playwright posting/repost work.

Offline stage inspects the persistent Chromium profile; the optional live stage
opens Marketplace once per account to catch logged-out sessions and security
checkpoints before a long automation loop burns time (or trips Facebook).
"""
from __future__ import annotations

import os
import sys
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


def is_ci_environment() -> bool:
    """True under GitHub Actions / generic CI (self-hosted runners included)."""
    if os.getenv("GITHUB_ACTIONS") or os.getenv("GITHUB_RUN_ID"):
        return True
    return env_bool("CI", False)


def format_fb_worker_session_guard(
    results: list[AccountHealth],
    *,
    command_hint: str = "python run_sync.py",
) -> str:
    """Banner when profiles are missing / expired — do not open Playwright yet.

    Playwright cookies live only under ``sessions/account_*`` on the fb-worker
    host. GitHub-hosted runners never have them; a self-hosted runner still
    fails if the profile was never logged in or the symlink is wrong.
    """
    bad = unhealthy(results)
    if not bad:
        return ""
    ci_note = (
        "Detected CI (GITHUB_ACTIONS/CI). Profiles are NOT in the cloud checkout — "
        "they must exist on the fb-worker disk under sessions/account_*/."
        if is_ci_environment()
        else "Run Facebook automation only on the host that owns sessions/account_* "
        "(typically fb-worker), not a bare laptop checkout without profiles."
    )
    lines = [
        "",
        "=" * 72,
        "FACEBOOK SESSIONS NOT READY — aborting browser automation",
        "=" * 72,
        ci_note,
        "",
        "Playwright persistent profiles (not cookies.json):",
        "  <repo>/sessions/account_1/   ← scripts/fb_login.py --account account_1",
        "  <repo>/sessions/account_2/   ← scripts/fb_login.py --account account_2",
        "",
        "Broken accounts:",
    ]
    for result in bad:
        lines.append(
            f"  - {result.account_id}: state={result.state} path={result.session_path}"
        )
        lines.append(f"      {result.detail}")
    lines.extend(
        [
            "",
            "Fix on fb-worker (headed display / X11):",
            "  python scripts/fb_login.py --account account_1",
            "  python scripts/fb_login.py --account account_2",
            "  python scripts/fb_test_session.py --account account_1",
            "",
            "Then re-run on that same host:",
            f"  {command_hint}",
            "=" * 72,
            "",
        ]
    )
    return "\n".join(lines)


def run_session_preflight(
    config: dict,
    account_ids: list[str],
    root: Path,
    *,
    live: bool | None = None,
    headless: bool | None = None,
    command_hint: str = "python run_sync.py",
    stream=None,
) -> tuple[list[AccountHealth], bool]:
    """Offline (+ optional live) session gate before any Marketplace browser work.

    Returns ``(results, ready)``. When ``ready`` is False, print the guard banner
    and do not call ``open_account_context`` / ``execute_*``.
    """
    out = sys.stderr if stream is None else stream
    results = precheck_accounts(
        config, account_ids, root, live=live, headless=headless
    )
    print(format_health_report(results), flush=True)
    if unhealthy(results):
        print(
            format_fb_worker_session_guard(results, command_hint=command_hint),
            file=out,
            flush=True,
        )
        return results, False
    return results, True


__all__ = [
    "AccountHealth",
    "STATE_CHECKPOINT",
    "STATE_ERROR",
    "STATE_LOGGED_OUT",
    "STATE_NO_SESSION",
    "STATE_OK",
    "check_live",
    "check_profile",
    "format_fb_worker_session_guard",
    "format_health_report",
    "is_ci_environment",
    "live_precheck_enabled",
    "precheck_accounts",
    "run_session_preflight",
    "unhealthy",
]
