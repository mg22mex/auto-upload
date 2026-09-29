"""Facebook Messenger / Lead Ads → WhatsApp auto-responder copy."""
from __future__ import annotations

import os
import re

ENV_WA_LINK = "FB_MESSENGER_WA_LINK"
DEFAULT_WA_LINK = "https://wa.me/526142274381"


def official_whatsapp_link() -> str:
    explicit = (os.getenv(ENV_WA_LINK) or "").strip()
    if explicit:
        return explicit
    digits = re.sub(r"\D", "", os.getenv("MARKETPLACE_WA_PERIFERICO") or "")
    if digits:
        return f"https://wa.me/{digits}"
    return DEFAULT_WA_LINK


def facebook_to_whatsapp_redirect(
    *,
    name: str = "",
    vehicle: str = "",
    wa_link: str | None = None,
) -> str:
    """Personalized FB → WhatsApp redirect (Messenger + Lead Ads)."""
    link = (wa_link or official_whatsapp_link()).strip() or DEFAULT_WA_LINK
    who = (name or "").strip() or "Cliente"
    first = who.split()[0]
    vehicle_bit = (vehicle or "").strip() or "el vehículo de tu interés"
    return (
        f"¡Hola {first}! Gracias por contactarnos por Facebook. "
        f"Para darte información inmediata sobre {vehicle_bit} y agendar tu "
        "prueba de manejo o cotización al instante, escríbenos directamente a "
        f"nuestro WhatsApp oficial: {link}"
    )


def facebook_messenger_autoreply(*, wa_link: str | None = None) -> str:
    """Legacy generic redirect (no name/vehicle). Prefer ``facebook_to_whatsapp_redirect``."""
    return facebook_to_whatsapp_redirect(wa_link=wa_link)


__all__ = [
    "DEFAULT_WA_LINK",
    "ENV_WA_LINK",
    "facebook_messenger_autoreply",
    "facebook_to_whatsapp_redirect",
    "official_whatsapp_link",
]
