from __future__ import annotations

from pathlib import Path

from src.facebook.util import env_bool


def graphql_enabled() -> bool:
    """True when create/delete should try direct GraphQL before Playwright UI."""
    return env_bool("FB_USE_GRAPHQL", False)


def graphql_ui_fallback() -> bool:
    """Fall back to Playwright when GraphQL templates/requests fail (default on)."""
    return env_bool("FB_GQL_FALLBACK_UI", True)


def graphql_create_ui_fallback() -> bool:
    """UI fallback for create only.

    Defaults **off** so GraphQL create never opens the Playwright composer
    (avoids FB duplicate-listing spam). Opt in with ``FB_GQL_CREATE_FALLBACK_UI=1``.
    """
    return env_bool("FB_GQL_CREATE_FALLBACK_UI", False)


def project_root_from_log_dir(log_dir: Path) -> Path:
    """``data/logs/facebook`` → repo root; else ``cwd``."""
    try:
        if log_dir.name == "facebook" and log_dir.parent.name == "logs":
            return log_dir.parents[2].resolve()
    except (IndexError, OSError):
        pass
    return Path.cwd().resolve()
