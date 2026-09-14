"""Voice gateway configuration — Vapi outbound + related env knobs."""
from __future__ import annotations

import os
from dataclasses import dataclass


ENV_VAPI_API_KEY = "VAPI_API_KEY"
ENV_VAPI_PHONE_NUMBER_ID = "VAPI_PHONE_NUMBER_ID"
ENV_VAPI_ASSISTANT_ID = "VAPI_ASSISTANT_ID"
ENV_VAPI_BASE_URL = "VAPI_BASE_URL"
ENV_VOICE_OUTBOUND_DRY = "VOICE_OUTBOUND_DRY_RUN"
ENV_VOICE_OUTBOUND_ENABLED = "VOICE_OUTBOUND_ENABLED"
ENV_DB_PATH = "DB_PATH"

DEFAULT_VAPI_BASE_URL = "https://api.vapi.ai"
DEFAULT_DB_PATH = "data/sync.db"
# Documented Autosell Periférico DID linked to Vapi phoneNumberId
DOCUMENTED_CALLER_E164 = "+526142274381"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _env_str(name: str, default: str = "") -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip()


@dataclass(frozen=True)
class VapiConfig:
    """Credentials for https://api.vapi.ai/call/phone outbound dials."""

    api_key: str
    phone_number_id: str
    assistant_id: str = ""
    base_url: str = DEFAULT_VAPI_BASE_URL
    caller_e164: str = DOCUMENTED_CALLER_E164

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.phone_number_id)

    @property
    def call_phone_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/call/phone"


def load_vapi_config() -> VapiConfig:
    return VapiConfig(
        api_key=_env_str(ENV_VAPI_API_KEY),
        phone_number_id=_env_str(ENV_VAPI_PHONE_NUMBER_ID),
        assistant_id=_env_str(ENV_VAPI_ASSISTANT_ID),
        base_url=_env_str(ENV_VAPI_BASE_URL, DEFAULT_VAPI_BASE_URL)
        or DEFAULT_VAPI_BASE_URL,
    )


def voice_outbound_dry_run() -> bool:
    return _env_bool(ENV_VOICE_OUTBOUND_DRY, True)


def voice_outbound_enabled() -> bool:
    return _env_bool(ENV_VOICE_OUTBOUND_ENABLED, True)


def sync_db_path() -> str:
    return _env_str(ENV_DB_PATH, DEFAULT_DB_PATH) or DEFAULT_DB_PATH


__all__ = [
    "DEFAULT_DB_PATH",
    "DEFAULT_VAPI_BASE_URL",
    "DOCUMENTED_CALLER_E164",
    "ENV_DB_PATH",
    "ENV_VAPI_API_KEY",
    "ENV_VAPI_ASSISTANT_ID",
    "ENV_VAPI_BASE_URL",
    "ENV_VAPI_PHONE_NUMBER_ID",
    "ENV_VOICE_OUTBOUND_DRY",
    "ENV_VOICE_OUTBOUND_ENABLED",
    "VapiConfig",
    "load_vapi_config",
    "sync_db_path",
    "voice_outbound_dry_run",
    "voice_outbound_enabled",
]
