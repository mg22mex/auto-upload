"""Voice gateway public exports.

Heavy deps (FastAPI / dotenv) load only when webhook app symbols are imported,
so pure unit tests for intent can run without full extras.
"""
from src.voice_gateway.intent import (
    TRANSFER_PROMPT_ES,
    VOICE_CHANNEL,
    VoiceIntent,
    format_tts_quote,
    parse_voice_intent,
)

__all__ = [
    "TRANSFER_PROMPT_ES",
    "VOICE_CHANNEL",
    "VoiceIntent",
    "app",
    "create_app",
    "format_tts_quote",
    "parse_voice_intent",
    "parse_voice_lead_payload",
]


def __getattr__(name: str):
    if name in {"app", "create_app", "parse_voice_lead_payload"}:
        from src.voice_gateway.webhook import app, create_app, parse_voice_lead_payload

        exports = {
            "app": app,
            "create_app": create_app,
            "parse_voice_lead_payload": parse_voice_lead_payload,
        }
        return exports[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
