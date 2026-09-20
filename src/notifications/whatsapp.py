"""Customer-facing WhatsApp confirmations via Evolution API.

Best-effort: send failures are logged and never raised to the caller.
Uses ``WhatsAppWorkerClient`` (``POST …/message/sendText/{instance}``).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from src.whatsapp_worker.client import WhatsAppWorkerClient, normalize_phone_number

ENV_ENABLED = "VAPI_CUSTOMER_WHATSAPP"
# Optional: ``52`` (default) or ``521`` for 10-digit MX mobiles (Evolution / WA).
ENV_MX_PREFIX = "WHATSAPP_MX_COUNTRY_PREFIX"


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
    """Digits only; MX 10-digit → ``52…`` or ``521…`` per ``WHATSAPP_MX_COUNTRY_PREFIX``."""
    prefix = (os.getenv(ENV_MX_PREFIX) or "52").strip() or "52"
    if prefix not in {"52", "521"}:
        prefix = "52"
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 10:
        return f"{prefix}{digits}"
    # Already international (52… / 521…) — leave to worker validator.
    return normalize_phone_number(phone, default_country="52")


def format_lead_confirmation(
    *,
    name: str,
    interested_vehicle: str | None = None,
    financing_summary: str | None = None,
    tradein_summary: str | None = None,
    appointment_date: str | None = None,
    branch: str | None = None,
) -> str:
    """Professional ES-MX confirmation after Vapi / Paulina wrap-up."""
    del branch  # reserved for future branch-specific copy
    client = (name or "Cliente").strip() or "Cliente"
    lines = [
        f"Hola {client}, ¡gracias por comunicarte a Autosell! 🚗",
        "",
        "Aquí tienes el resumen de tu consulta con nuestro asistente:",
        "",
    ]

    vehicle = (interested_vehicle or "").strip()
    financing = (financing_summary or "").strip()
    tradein = (tradein_summary or "").strip()
    appointment = (appointment_date or "").strip()

    if vehicle:
        lines.append(f"📌 Vehículo de interés: {vehicle}")
    if financing:
        lines.append(f"💰 Financiamiento / Enganche: {financing}")
    if tradein:
        lines.append(f"🔄 Avalúo Trade-In: {tradein}")
    if appointment:
        lines.append(f"📅 Cita Agendada: {appointment}")

    if vehicle or financing or tradein or appointment:
        lines.append("")

    lines.append(
        "Un asesor de nuestra sucursal se pondrá en contacto contigo a la "
        "brevedad. ¡Quedamos a tus órdenes!"
    )
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
    "ENV_MX_PREFIX",
    "customer_whatsapp_enabled",
    "format_customer_phone",
    "format_lead_confirmation",
    "notify_lead_confirmation",
    "send_whatsapp_message",
]
