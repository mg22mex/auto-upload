"""Facebook Messenger → WhatsApp auto-responder copy.

DNS migration is deferred; Messenger replies redirect to official WA.
"""
from __future__ import annotations

import os
import re

ENV_WA_LINK = "FB_MESSENGER_WA_LINK"
# Desk / official Autosell line (Periférico marketplace CTA default).
DEFAULT_WA_LINK = "https://wa.me/526142274381"


def official_whatsapp_link() -> str:
    explicit = (os.getenv(ENV_WA_LINK) or "").strip()
    if explicit:
        return explicit
    digits = re.sub(r"\D", "", os.getenv("MARKETPLACE_WA_PERIFERICO") or "")
    if digits:
        return f"https://wa.me/{digits}"
    return DEFAULT_WA_LINK


def facebook_messenger_autoreply(*, wa_link: str | None = None) -> str:
    """Standard Messenger auto-response redirecting to WhatsApp."""
    link = (wa_link or official_whatsapp_link()).strip() or DEFAULT_WA_LINK
    return (
        "¡Hola! Para darte una atención mucho más rápida y agendar tu prueba "
        "de manejo o cotización al instante, escríbenos directamente a nuestro "
        f"WhatsApp oficial: {link}"
    )


__all__ = [
    "DEFAULT_WA_LINK",
    "ENV_WA_LINK",
    "facebook_messenger_autoreply",
    "official_whatsapp_link",
]
