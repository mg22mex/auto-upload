"""Alternate weekly Marketplace bump: renew one week, full repost the next."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "America/Chihuahua"
VALID_MODES = frozenset({"renew", "repost"})


def resolve_weekly_bump_mode(
    now: datetime | None = None,
    *,
    timezone: str = DEFAULT_TIMEZONE,
    even_week: str = "renew",
    odd_week: str = "repost",
    force_mode: str | None = None,
) -> str:
    """Pick renew vs repost from ISO week number (or an explicit override).

    Even ISO weeks → ``even_week`` (default renew).
    Odd ISO weeks → ``odd_week`` (default repost).
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
    """Read ``sync.weekly_bump`` with safe defaults."""
    sync = (config or {}).get("sync") or {}
    bump = sync.get("weekly_bump") or {}
    return {
        "timezone": str(bump.get("timezone") or DEFAULT_TIMEZONE),
        "even_week": str(bump.get("even_week") or "renew"),
        "odd_week": str(bump.get("odd_week") or "repost"),
        "schedule": str(bump.get("schedule") or sync.get("renew", {}).get("schedule") or ""),
    }
