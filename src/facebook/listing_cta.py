"""Marketplace listing WhatsApp CTA — branch-mapped wa.me deep links."""
from __future__ import annotations

from urllib.parse import quote

from src.models import Vehicle
from src.odoo_sync.crm import (
    PLACEHOLDER_BRANCH,
    PRIMARY_BRANCH,
    infer_physical_location,
    normalize_crm_branch,
)
from src.odoo_sync.quotes import BRANCH_BRANDING

DEFAULT_WA_PERIFERICO = "526142274381"
DEFAULT_WA_SAN_FELIPE = "526141293763"

ENV_WA_PERIFERICO = "MARKETPLACE_WA_PERIFERICO"
ENV_WA_SAN_FELIPE = "MARKETPLACE_WA_SAN_FELIPE"

_BRANCH_PHONES: dict[str, str] = {
    PRIMARY_BRANCH: DEFAULT_WA_PERIFERICO,
    PLACEHOLDER_BRANCH: DEFAULT_WA_SAN_FELIPE,
}

_LOCATION_SPEC_KEYS = frozenset(
    {
        "sucursal",
        "ubicación",
        "ubicacion",
        "location",
        "branch",
        "lote",
        "sede",
    }
)


def _whatsapp_phone(branch_key: str) -> str:
    import os

    key = normalize_crm_branch(branch_key)
    if key == PLACEHOLDER_BRANCH:
        raw = (os.getenv(ENV_WA_SAN_FELIPE) or "").strip()
        return raw or _BRANCH_PHONES[PLACEHOLDER_BRANCH]
    raw = (os.getenv(ENV_WA_PERIFERICO) or "").strip()
    return raw or _BRANCH_PHONES[PRIMARY_BRANCH]


def infer_vehicle_branch(vehicle: Vehicle) -> str:
    """Return CRM branch key (``periferico`` | ``san_felipe``) for a catalog vehicle."""
    for key, value in vehicle.specs.items():
        key_norm = key.strip().lower()
        if key_norm in _LOCATION_SPEC_KEYS or any(
            token in key_norm for token in ("sucursal", "ubic", "branch", "location", "lote")
        ):
            inferred = infer_physical_location(value)
            if inferred:
                return inferred
    for value in vehicle.specs.values():
        inferred = infer_physical_location(value)
        if inferred:
            return inferred
    return PRIMARY_BRANCH


def branch_display_name(branch_key: str) -> str:
    key = normalize_crm_branch(branch_key)
    branding = BRANCH_BRANDING.get(key) or BRANCH_BRANDING[PRIMARY_BRANCH]
    return str(branding.get("branch_label") or "Periférico")


def build_whatsapp_link(
    *,
    phone: str,
    year: str,
    make: str,
    model: str,
) -> str:
    """Build ``wa.me`` URL with pre-filled Spanish inquiry text."""
    digits = "".join(ch for ch in phone if ch.isdigit())
    vehicle_label = " ".join(part for part in (year, make, model) if part).strip()
    if not vehicle_label:
        vehicle_label = "vehículo"
    text = f"Hola, me interesa información sobre el {vehicle_label}"
    return f"https://wa.me/{digits}?text={quote(text)}"


def format_whatsapp_cta_block(*, branch_name: str, whatsapp_link: str) -> str:
    """Append-style CTA block for FB Marketplace description text."""
    branch = (branch_name or "Periférico").strip()
    return (
        "\n\n"
        "📲 **¡Contáctanos por WhatsApp!**\n"
        f"Haz clic en el enlace para iniciar chat directo con un asesor de {branch}:\n"
        f"{whatsapp_link}"
    )


def whatsapp_cta_for_vehicle(vehicle: Vehicle, *, branch: str | None = None) -> str:
    """Full WhatsApp CTA block for ``vehicle_description()``."""
    branch_key = normalize_crm_branch(branch) if branch else infer_vehicle_branch(vehicle)
    link = build_whatsapp_link(
        phone=_whatsapp_phone(branch_key),
        year=(vehicle.year or "").strip(),
        make=(vehicle.brand or "").strip(),
        model=(vehicle.title or "").strip(),
    )
    return format_whatsapp_cta_block(
        branch_name=branch_display_name(branch_key),
        whatsapp_link=link,
    )


__all__ = [
    "DEFAULT_WA_PERIFERICO",
    "DEFAULT_WA_SAN_FELIPE",
    "branch_display_name",
    "build_whatsapp_link",
    "format_whatsapp_cta_block",
    "infer_vehicle_branch",
    "whatsapp_cta_for_vehicle",
]
