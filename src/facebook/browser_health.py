"""Detect dead Playwright browser / page targets so workers can reopen."""

from __future__ import annotations

_DEAD_TYPE_NAMES = frozenset(
    {
        "TargetClosedError",
        "BrowserClosedError",
        "TargetDestroyedError",
    }
)

_DEAD_MESSAGE_FRAGMENTS = (
    "target closed",
    "target page, context or browser has been closed",
    "target page closed",
    "browser has been closed",
    "context or browser has been closed",
    "connection closed",
    "browser closed",
)


def is_browser_dead(exc: BaseException) -> bool:
    """True when Chromium/page/context died and a fresh launch is needed."""
    name = type(exc).__name__
    if name in _DEAD_TYPE_NAMES or name.endswith("TargetClosedError"):
        return True
    msg = str(exc).lower()
    return any(fragment in msg for fragment in _DEAD_MESSAGE_FRAGMENTS)
