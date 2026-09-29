"""Resolve Odoo credentials for Streamlit Cloud + local/VPS.

Priority:
1. Explicit constructor args (tests)
2. ``st.secrets`` (Streamlit Community Cloud / ``.streamlit/secrets.toml``)
3. Process env / ``.env`` (``ODOO_URL``, ``ODOO_DB``, ``ODOO_USERNAME``, ``ODOO_API_KEY``)
"""
from __future__ import annotations

import os
from typing import Any


def _secret_get(secrets: Any, *keys: str) -> str:
    """Read flat or nested secret keys; return stripped string or ""."""
    for key in keys:
        try:
            val = secrets.get(key) if hasattr(secrets, "get") else secrets[key]
        except Exception:
            val = None
        if val not in (None, ""):
            return str(val).strip()
    # Nested [odoo] block
    try:
        block = secrets["odoo"]
    except Exception:
        return ""
    for key in keys:
        short = key.lower().removeprefix("odoo_")
        try:
            val = block.get(short) if hasattr(block, "get") else block[short]
        except Exception:
            try:
                val = block.get(key) if hasattr(block, "get") else block[key]
            except Exception:
                val = None
        if val not in (None, ""):
            return str(val).strip()
    return ""


def apply_odoo_secrets_to_environ() -> dict[str, str]:
    """Copy Odoo creds from ``st.secrets`` into ``os.environ`` when missing.

    Safe to call outside Streamlit (no-op). Never overwrites a non-empty env var.
    Returns the resolved (non-secret) keys that were populated.
    """
    try:
        import streamlit as st

        secrets = st.secrets
    except Exception:
        return {}

    mapping = {
        "ODOO_URL": ("ODOO_URL", "odoo_url", "url"),
        "ODOO_DB": ("ODOO_DB", "odoo_db", "db"),
        "ODOO_USERNAME": ("ODOO_USERNAME", "ODOO_USER", "odoo_username", "username", "user"),
        "ODOO_API_KEY": (
            "ODOO_API_KEY",
            "ODOO_PASSWORD",
            "odoo_api_key",
            "api_key",
            "password",
        ),
    }
    applied: dict[str, str] = {}
    for env_key, aliases in mapping.items():
        if (os.getenv(env_key) or "").strip():
            continue
        # Also skip if alternate env already set for username/password
        if env_key == "ODOO_USERNAME" and (os.getenv("ODOO_USER") or "").strip():
            continue
        if env_key == "ODOO_API_KEY" and (os.getenv("ODOO_PASSWORD") or "").strip():
            continue
        value = _secret_get(secrets, *aliases)
        if not value:
            continue
        os.environ[env_key] = value
        applied[env_key] = "(from st.secrets)"
    return applied


def get_odoo_client():
    """Authenticate ``OdooCRMClient`` using secrets → env → .env."""
    from src.odoo_sync.client import OdooCRMClient

    apply_odoo_secrets_to_environ()
    client = OdooCRMClient()
    client.authenticate()
    return client


__all__ = [
    "apply_odoo_secrets_to_environ",
    "get_odoo_client",
]
