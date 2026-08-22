"""Marketplace bump schedule: prefer full repost/relist; renew optional."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "America/Chihuahua"
VALID_MODES = frozenset({"renew", "repost"})
# Relist-first: both ISO weeks default to full delete+recreate.
DEFAULT_EVEN_WEEK = "repost"
DEFAULT_ODD_WEEK = "repost"
DEFAULT_MIN_AGE_DAYS = 2.0
DEFAULT_MAX_PER_ACCOUNT_PER_RUN = 25


def resolve_weekly_bump_mode(
    now: datetime | None = None,
    *,
    timezone: str = DEFAULT_TIMEZONE,
    even_week: str = DEFAULT_EVEN_WEEK,
    odd_week: str = DEFAULT_ODD_WEEK,
    force_mode: str | None = None,
) -> str:
    """Pick renew vs repost from ISO week number (or an explicit override).

    Defaults favor **repost** every week (even and odd both ``repost``).
    Operators can still alternate by setting ``even_week`` / ``odd_week``
    differently, or force ``renew`` | ``repost`` | ``auto``.
    """
    if force_mode:
        mode = str(force_mode).strip().lower()
        if mode == "auto":
            force_mode = None
        elif mode in VALID_MODES:
            return mode
        else:
            raise ValueError(f"force_mode must be renew|repost|auto, got {force_mode!r}")

    even = str(even_week).strip().lower()
    odd = str(odd_week).strip().lower()
    if even not in VALID_MODES or odd not in VALID_MODES:
        raise ValueError("even_week/odd_week must be 'renew' or 'repost'")

    tz = ZoneInfo(timezone)
    when = now.astimezone(tz) if now is not None else datetime.now(tz)
    if when.tzinfo is None:
        when = when.replace(tzinfo=tz)
    else:
        when = when.astimezone(tz)

    week = int(when.isocalendar().week)
    return even if week % 2 == 0 else odd


def weekly_bump_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Read ``sync.weekly_bump`` with relist-first safe defaults."""
    sync = (config or {}).get("sync") or {}
    bump = sync.get("weekly_bump") or {}
    repost = sync.get("repost") or {}
    renew = sync.get("renew") or {}
    min_age = (
        bump.get("min_age_days")
        if bump.get("min_age_days") is not None
        else repost.get("min_age_days")
        if repost.get("min_age_days") is not None
        else renew.get("min_age_days")
        if renew.get("min_age_days") is not None
        else DEFAULT_MIN_AGE_DAYS
    )
    return {
        "timezone": str(bump.get("timezone") or DEFAULT_TIMEZONE),
        "even_week": str(bump.get("even_week") or DEFAULT_EVEN_WEEK),
        "odd_week": str(bump.get("odd_week") or DEFAULT_ODD_WEEK),
        "schedule": str(
            bump.get("schedule")
            or repost.get("schedule")
            or renew.get("schedule")
            or ""
        ),
        "min_age_days": float(min_age),
        "max_per_account_per_run": int(
            bump.get("max_per_account_per_run")
            if bump.get("max_per_account_per_run") is not None
            else repost.get("max_per_account_per_run")
            if repost.get("max_per_account_per_run") is not None
            else DEFAULT_MAX_PER_ACCOUNT_PER_RUN
        ),
    }
