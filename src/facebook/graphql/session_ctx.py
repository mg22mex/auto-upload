from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
from playwright.sync_api import BrowserContext, Page

from src.facebook.errors import FacebookSessionError
from src.facebook.session import (
    _default_user_agent,
    get_page,
    launch_persistent_context,
    resolve_session_dir,
)
from src.facebook.util import env_str

GRAPHQL_URL = "https://www.facebook.com/api/graphql/"
MARKETPLACE_URL = "https://www.facebook.com/marketplace/"
COMPOSER_URL = "https://www.facebook.com/marketplace/create/vehicle"

# Comet Relay form keys copied from a live browser GraphQL POST (create needs these;
# bare av/fb_dtsg/doc_id → field_exception on marketplace_listing_create).
_COMET_SCAFFOLD_KEYS = (
    "__aaid",
    "__ccg",
    "__comet_req",
    "__crn",
    "__csr",
    "__dyn",
    "__hblp",
    "__hs",
    "__hsdp",
    "__hsi",
    "__s",
    "__sjsp",
    "__spin_b",
    "__spin_r",
    "__spin_t",
    "dpr",
    "fb_api_analytics_tags",
    "qpl_active_flow_ids",
)

_DTSG_PATTERNS = (
    re.compile(r'"DTSGInitData"\s*,\s*\[\s*\]\s*,\s*\{\s*"token"\s*:\s*"([^"]+)"'),
    re.compile(r'"DTSGInitialData"\s*,\s*\[\s*\]\s*,\s*\{\s*"token"\s*:\s*"([^"]+)"'),
    re.compile(r'"dtsg"\s*:\s*\{\s*"token"\s*:\s*"([^"]+)"'),
    re.compile(r'name="fb_dtsg"\s+value="([^"]+)"'),
    re.compile(r'"token"\s*:\s*"([^"]+)"[^}]*"async_get_token"'),
)
_LSD_PATTERNS = (
    re.compile(r'"LSD"\s*,\s*\[\s*\]\s*,\s*\{\s*"token"\s*:\s*"([^"]+)"'),
    re.compile(r'name="lsd"\s+value="([^"]+)"'),
)
_JAZOEST_PATTERNS = (
    re.compile(r"jazoest=(\d+)"),
    re.compile(r'"jazoest"\s*:\s*"?(\d+)"?'),
)
_REV_PATTERNS = (
    re.compile(r'"client_revision"\s*:\s*(\d+)'),
    re.compile(r"__spin_r\s*:\s*(\d+)"),
    re.compile(r'"__spin_r"\s*:\s*(\d+)'),
)


@dataclass
class FbSessionContext:
    """Cookies + CSRF tokens needed for ``/api/graphql/`` POSTs."""

    c_user: str
    xs: str
    fb_dtsg: str
    cookies: dict[str, str] = field(default_factory=dict)
    lsd: str = ""
    jazoest: str = ""
    client_revision: str = "1"
    user_agent: str = field(default_factory=_default_user_agent)
    account_id: str = ""
    # Live Comet Relay form extras (__dyn/__spin_*/__crn/…). Required for create.
    comet_scaffold: dict[str, str] = field(default_factory=dict)

    @property
    def cookie_header(self) -> str:
        # Prefer full jar; always ensure c_user + xs present.
        jar = dict(self.cookies)
        if self.c_user:
            jar["c_user"] = self.c_user
        if self.xs:
            jar["xs"] = self.xs
        return "; ".join(f"{k}={v}" for k, v in jar.items() if v)

    def redacted_summary(self) -> str:
        return (
            f"c_user={self.c_user} xs={'set' if self.xs else 'missing'} "
            f"fb_dtsg={'set' if self.fb_dtsg else 'missing'} "
            f"cookies={len(self.cookies)} "
            f"scaffold={len(self.comet_scaffold)}"
        )


def _first_match(patterns: tuple[re.Pattern[str], ...], html: str) -> str:
    for pattern in patterns:
        match = pattern.search(html)
        if match:
            return match.group(1).strip()
    return ""


def parse_tokens_from_html(html: str) -> dict[str, str]:
    return {
        "fb_dtsg": _first_match(_DTSG_PATTERNS, html),
        "lsd": _first_match(_LSD_PATTERNS, html),
        "jazoest": _first_match(_JAZOEST_PATTERNS, html),
        "client_revision": _first_match(_REV_PATTERNS, html) or "1",
    }


def cookies_from_playwright(context: BrowserContext) -> dict[str, str]:
    jar: dict[str, str] = {}
    try:
        raw = context.cookies("https://www.facebook.com")
    except Exception as exc:
        raise FacebookSessionError(f"Failed to read Facebook cookies: {exc}") from exc
    for cookie in raw:
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "").strip()
        if name and value:
            jar[name] = value
    return jar


def extract_session_context_from_cookies(
    cookies: dict[str, str],
    *,
    user_agent: str | None = None,
    account_id: str = "",
    html: str | None = None,
    fetch_tokens: bool = True,
    timeout_sec: float = 45.0,
) -> FbSessionContext:
    """Build session from cookie jar; refresh ``fb_dtsg`` via Marketplace HTML."""
    c_user = str(cookies.get("c_user") or "").strip()
    xs = unquote(str(cookies.get("xs") or "").strip())
    if not c_user or not xs:
        raise FacebookSessionError(
            f"{account_id or 'session'}: missing c_user/xs cookies — "
            "run scripts/fb_login.py for this account"
        )

    tokens = {
        "fb_dtsg": "",
        "lsd": "",
        "jazoest": "",
        "client_revision": "1",
    }
    if html:
        tokens.update({k: v for k, v in parse_tokens_from_html(html).items() if v})

    ua = user_agent or env_str("FB_USER_AGENT", "") or _default_user_agent()
    if fetch_tokens and not tokens["fb_dtsg"]:
        tokens = _fetch_tokens_http(cookies, user_agent=ua, timeout_sec=timeout_sec)

    if not tokens.get("fb_dtsg"):
        raise FacebookSessionError(
            f"{account_id or 'session'}: failed to extract fb_dtsg — "
            "session may be expired (refresh with fb_login.py)"
        )

    return FbSessionContext(
        c_user=c_user,
        xs=xs,
        fb_dtsg=tokens["fb_dtsg"],
        cookies=dict(cookies),
        lsd=tokens.get("lsd") or "",
        jazoest=tokens.get("jazoest") or "",
        client_revision=tokens.get("client_revision") or "1",
        user_agent=ua,
        account_id=account_id,
    )


def _fetch_tokens_http(
    cookies: dict[str, str],
    *,
    user_agent: str,
    timeout_sec: float,
) -> dict[str, str]:
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items() if v)
    headers = {
        "User-Agent": user_agent,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
        "Cookie": cookie_header,
        "Upgrade-Insecure-Requests": "1",
    }
    with httpx.Client(timeout=timeout_sec, follow_redirects=True) as client:
        response = client.get(MARKETPLACE_URL, headers=headers)
    if response.status_code in {401, 403}:
        raise FacebookSessionError(
            f"Facebook rejected session while fetching fb_dtsg "
            f"(HTTP {response.status_code})"
        )
    html = response.text or ""
    if "login" in str(response.url).lower() or 'name="email"' in html[:8000].lower():
        raise FacebookSessionError(
            "Facebook session expired while fetching Marketplace tokens"
        )
    tokens = parse_tokens_from_html(html)
    if not tokens.get("fb_dtsg"):
        raise FacebookSessionError("fb_dtsg not found in Marketplace HTML")
    return tokens


def extract_session_context(
    context: BrowserContext,
    *,
    page: Page | None = None,
    account_id: str = "",
    fetch_tokens: bool = True,
) -> FbSessionContext:
    """Extract ``c_user`` / ``xs`` / ``fb_dtsg`` from an open Playwright profile."""
    cookies = cookies_from_playwright(context)
    html: str | None = None
    if page is not None:
        try:
            html = page.content()
        except Exception:
            html = None
        if not html or not parse_tokens_from_html(html).get("fb_dtsg"):
            try:
                page.goto(MARKETPLACE_URL, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(800)
                html = page.content()
            except Exception:
                html = None
    return extract_session_context_from_cookies(
        cookies,
        user_agent=_default_user_agent(),
        account_id=account_id,
        html=html,
        fetch_tokens=fetch_tokens,
    )


def harvest_comet_scaffold(page: Page, *, timeout_ms: int = 12_000) -> dict[str, str]:
    """Capture Comet Relay form extras from a live ``/api/graphql/`` POST.

    ``useCometMarketplaceListingCreateMutation`` rejects minimal forms with
    ``field_exception``; browser creates include ``__dyn`` / ``__spin_*`` /
    ``__crn=…ComposerRoute`` etc. Prefer a composer-route request.
    """
    from urllib.parse import parse_qs

    captured: list[dict[str, str]] = []

    def on_request(request) -> None:  # type: ignore[no-untyped-def]
        try:
            url = request.url or ""
            if "/api/graphql" not in url or request.method.upper() != "POST":
                return
            post = request.post_data or ""
            params = {
                k: (v[0] if v else "")
                for k, v in parse_qs(post, keep_blank_values=True).items()
            }
            if "__spin_r" not in params and "__dyn" not in params:
                return
            captured.append(params)
        except Exception:
            return

    import time as _time

    page.on("request", on_request)
    try:
        try:
            page.goto(COMPOSER_URL, wait_until="domcontentloaded", timeout=60_000)
        except Exception:
            try:
                page.goto(MARKETPLACE_URL, wait_until="domcontentloaded", timeout=60_000)
            except Exception:
                pass
        end = _time.time() + max(2.0, timeout_ms / 1000.0)
        while _time.time() < end and not captured:
            page.wait_for_timeout(400)
    finally:
        try:
            page.remove_listener("request", on_request)
        except Exception:
            pass

    if not captured:
        return {}

    # Prefer composer route.
    chosen = captured[-1]
    for params in reversed(captured):
        crn = params.get("__crn", "")
        if "Composer" in crn or "composer" in crn.lower():
            chosen = params
            break

    scaffold = {
        key: chosen[key]
        for key in _COMET_SCAFFOLD_KEYS
        if key in chosen and str(chosen[key]).strip()
    }
    if scaffold:
        scaffold["__spin_t"] = str(int(_time.time()))
    return scaffold


def extract_session_context_from_profile(
    config: dict,
    account_id: str,
    *,
    root: Path,
    headless: bool = True,
) -> FbSessionContext:
    """Open the stored Chromium profile briefly and extract GraphQL session auth."""
    from playwright.sync_api import sync_playwright

    session_dir = resolve_session_dir(config, account_id, root)
    if not session_dir.is_dir():
        raise FacebookSessionError(
            f"{account_id}: session dir missing: {session_dir}. "
            f"Run: python scripts/fb_login.py --account {account_id}"
        )

    with sync_playwright() as playwright:
        context = None
        try:
            context = launch_persistent_context(
                playwright, session_dir, headless=headless
            )
            page = get_page(context)
            return extract_session_context(
                context, page=page, account_id=account_id, fetch_tokens=True
            )
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass


def session_to_httpx_cookies(session: FbSessionContext) -> httpx.Cookies:
    jar = httpx.Cookies()
    for name, value in session.cookies.items():
        jar.set(name, value, domain=".facebook.com")
    jar.set("c_user", session.c_user, domain=".facebook.com")
    jar.set("xs", session.xs, domain=".facebook.com")
    return jar


def browser_headers(session: FbSessionContext, *, for_graphql: bool = False) -> dict[str, str]:
    headers: dict[str, str] = {
        "User-Agent": session.user_agent,
        "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
        "Cookie": session.cookie_header,
        "Origin": "https://www.facebook.com",
        "Referer": "https://www.facebook.com/marketplace/",
    }
    if for_graphql:
        headers.update(
            {
                "Accept": "*/*",
                "Content-Type": "application/x-www-form-urlencoded",
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
            }
        )
        if session.lsd:
            headers["X-FB-LSD"] = session.lsd
    else:
        headers["Accept"] = (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        )
    return headers


def jazoest_from_dtsg(fb_dtsg: str) -> str:
    """FB often derives jazoest as ``2`` + sum of ``fb_dtsg`` codepoints."""
    if not fb_dtsg:
        return ""
    return "2" + str(sum(ord(ch) for ch in fb_dtsg))


def as_public_dict(session: FbSessionContext) -> dict[str, Any]:
    """Non-secret summary for logs/tests."""
    return {
        "account_id": session.account_id,
        "c_user": session.c_user,
        "has_xs": bool(session.xs),
        "has_fb_dtsg": bool(session.fb_dtsg),
        "has_lsd": bool(session.lsd),
        "cookie_count": len(session.cookies),
        "client_revision": session.client_revision,
    }
