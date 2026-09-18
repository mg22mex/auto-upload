"""Customer-facing WhatsApp confirmations via Evolution API.

Best-effort: send failures are logged and never raised to the caller.
Uses ``WhatsAppWorkerClient`` (``POST …/message/sendText/{instance}``).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from src.odoo_sync.quotes import BRANCH_BRANDING, PRIMARY_BRANCH
from src.whatsapp_worker.client import WhatsAppWorkerClient, normalize_phone_number

ENV_ENABLED = "VAPI_CUSTOMER_WHATSAPP"
DEFAULT_SITE_URL = "https://www.autosell.mx"


@dataclass
class CustomerNotifyResult:
    sent: bool
    phone: str = ""
    message: str = ""
    skipped_reason: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "sent": self.sent,
            "phone": self.phone,
            "skipped_reason": self.skipped_reason,
            "error": self.error,
        }


def customer_whatsapp_enabled() -> bool:
    raw = (os.getenv(ENV_ENABLED) or "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def format_customer_phone(phone: str) -> str:
    """Digits for Evolution ``number`` field (MX 10-digit → ``52…``)."""
    return normalize_phone_number(phone)


def agency_address(branch: str | None = None) -> str:
    key = (branch or PRIMARY_BRANCH).strip().lower() or PRIMARY_BRANCH
    branding = BRANCH_BRANDING.get(key) or BRANCH_BRANDING[PRIMARY_BRANCH]
    return str(branding.get("address") or branding.get("city") or "").strip()


def site_url() -> str:
    return (os.getenv("AUTOSELL_SITE_URL") or DEFAULT_SITE_URL).strip() or DEFAULT_SITE_URL


def format_lead_confirmation(
    *,
    name: str,
    interested_vehicle: str | None = None,
    financing_summary: str | None = None,
    tradein_summary: str | None = None,
    appointment_date: str | None = None,
    branch: str | None = None,
) -> str:
    """Professional ES-MX confirmation for Paulina / Vapi wrap-up."""
    client = (name or "Cliente").strip() or "Cliente"
    lines = [
        f"Hola {client}, ¡gracias por comunicarte con Autosell!",
        "",
    ]

    vehicle = (interested_vehicle or "").strip()
    financing = (financing_summary or "").strip()
    if vehicle or financing:
        lines.append("*Vehículo y financiamiento*")
        if vehicle:
            lines.append(f"• Interés: {vehicle}")
        if financing:
            lines.append(f"• Cotización: {financing}")
        lines.append("")

    tradein = (tradein_summary or "").strip()
    if tradein:
        lines.append("*Auto a cambio*")
        lines.append(f"• Estimación Autométrica: {tradein}")
        lines.append("")

    appointment = (appointment_date or "").strip()
    if appointment:
        address = agency_address(branch)
        lines.append("*Cita*")
        lines.append(f"• Fecha/hora solicitada: {appointment}")
        if address:
            lines.append(f"• Agencia: {address}")
        lines.append("")

    lines += [
        "Un asesor de Autosell dará seguimiento a tu caso.",
        f"Más info: {site_url()}",
    ]
    return "\n".join(lines)


def send_whatsapp_message(
    phone: str,
    text: str,
    *,
    branch: str | None = None,
    client: WhatsAppWorkerClient | None = None,
) -> dict[str, Any]:
    """POST Evolution ``/message/sendText/{INSTANCE}`` (or open-wa equivalent)."""
    wa = client or WhatsAppWorkerClient()
    number = format_customer_phone(phone)
    return wa.send_text_message(number, text, branch=branch)


def notify_lead_confirmation(
    *,
    name: str,
    phone: str,
    interested_vehicle: str | None = None,
    financing_summary: str | None = None,
    tradein_summary: str | None = None,
    appointment_date: str | None = None,
    branch: str | None = None,
    whatsapp_client: Any | None = None,
) -> CustomerNotifyResult:
    """Best-effort customer WhatsApp after Vapi lead create/update."""
    if not customer_whatsapp_enabled():
        return CustomerNotifyResult(sent=False, skipped_reason=f"{ENV_ENABLED}=false")

    phone_raw = (phone or "").strip()
    if not phone_raw:
        return CustomerNotifyResult(sent=False, skipped_reason="missing phone")

    message = format_lead_confirmation(
        name=name,
        interested_vehicle=interested_vehicle,
        financing_summary=financing_summary,
        tradein_summary=tradein_summary,
        appointment_date=appointment_date,
        branch=branch,
    )

    try:
        number = format_customer_phone(phone_raw)
    except Exception as exc:
        return CustomerNotifyResult(
            sent=False,
            message=message,
            skipped_reason="invalid phone",
            error=str(exc),
        )

    try:
        send_whatsapp_message(
            number,
            message,
            branch=branch,
            client=whatsapp_client,
        )
    except Exception as exc:
        print(
            f"WARN customer WhatsApp failed phone={number}: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        return CustomerNotifyResult(
            sent=False,
            phone=number,
            message=message,
            error=str(exc),
        )

    return CustomerNotifyResult(sent=True, phone=number, message=message)


__all__ = [
    "CustomerNotifyResult",
    "ENV_ENABLED",
    "agency_address",
    "customer_whatsapp_enabled",
    "format_customer_phone",
    "format_lead_confirmation",
    "notify_lead_confirmation",
    "send_whatsapp_message",
    "site_url",
]
