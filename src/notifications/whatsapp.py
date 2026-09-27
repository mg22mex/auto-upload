"""Customer-facing WhatsApp confirmations via Evolution API.

Best-effort: send failures are logged and never raised to the caller.
Uses ``WhatsAppWorkerClient`` (``POST …/message/sendText/{instance}``).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
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

    vehicle = (interested_vehicle or "").strip() if interested_vehicle else ""
    financing = (financing_summary or "").strip() if financing_summary else ""
    tradein = (tradein_summary or "").strip() if tradein_summary else ""
    appointment = (appointment_date or "").strip() if appointment_date else ""

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


def format_financing_whatsapp_summary(
    *,
    name: str | None = None,
    vehicle_price: float | None = None,
    down_payment: float | None = None,
    term_months: int | None = None,
    monthly_payment: float | None = None,
    vehicle_name: str | None = None,
) -> str:
    """Short ES-MX text sent with the CrediAuto PDF attachment."""
    client = (name or "").strip() or "Cliente"
    lines = [
        f"Hola {client}, aquí tienes tu cotización Scotiabank CrediAuto de Autosell. 🚗",
        "",
    ]
    if vehicle_name:
        lines.append(f"📌 Vehículo: {vehicle_name.strip()}")
    if vehicle_price is not None:
        lines.append(f"💵 Precio: ${float(vehicle_price):,.2f} MXN")
    if down_payment is not None:
        lines.append(f"💰 Enganche: ${float(down_payment):,.2f} MXN")
    if term_months is not None:
        lines.append(f"📆 Plazo: {int(term_months)} meses")
    if monthly_payment is not None:
        lines.append(f"📅 Mensualidad estimada: ${float(monthly_payment):,.2f} MXN")
    lines.extend(
        [
            "",
            "Adjuntamos la tabla de amortización (PDF). "
            "Un asesor te contactará para resolver dudas. ¡Gracias!",
        ]
    )
    return "\n".join(lines)


def notify_financing_quote(
    *,
    phone: str,
    pdf_path: str | Path,
    name: str | None = None,
    vehicle_price: float | None = None,
    down_payment: float | None = None,
    term_months: int | None = None,
    monthly_payment: float | None = None,
    vehicle_name: str | None = None,
    branch: str | None = None,
    whatsapp_client: Any | None = None,
    caption: str | None = None,
) -> CustomerNotifyResult:
    """Send financing text summary + ``financing_quote.pdf`` document to the client.

    Evolution: ``sendText`` then ``sendMedia`` (document). Failures are logged,
    never raised.
    """
    if not customer_whatsapp_enabled():
        return CustomerNotifyResult(sent=False, skipped_reason=f"{ENV_ENABLED}=false")

    phone_raw = (phone or "").strip()
    if not phone_raw:
        return CustomerNotifyResult(sent=False, skipped_reason="missing phone")

    path = Path(pdf_path)
    if not path.is_file():
        return CustomerNotifyResult(
            sent=False,
            skipped_reason="pdf missing",
            error=f"PDF not found: {path}",
        )

    message = format_financing_whatsapp_summary(
        name=name,
        vehicle_price=vehicle_price,
        down_payment=down_payment,
        term_months=term_months,
        monthly_payment=monthly_payment,
        vehicle_name=vehicle_name,
    )
    doc_caption = (caption or "Autosell — Tabla de amortización CrediAuto").strip()

    try:
        number = format_customer_phone(phone_raw)
    except Exception as exc:
        return CustomerNotifyResult(
            sent=False,
            message=message,
            skipped_reason="invalid phone",
            error=str(exc),
        )

    wa = whatsapp_client or WhatsAppWorkerClient()
    try:
        wa.send_text_message(number, message, branch=branch)
    except Exception as exc:
        print(
            f"WARN financing WhatsApp text failed phone={number}: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        return CustomerNotifyResult(
            sent=False,
            phone=number,
            message=message,
            error=f"text: {exc}",
        )

    try:
        wa.send_quote_pdf(number, path, caption=doc_caption, branch=branch)
    except Exception as exc:
        print(
            f"WARN financing WhatsApp PDF failed phone={number}: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        return CustomerNotifyResult(
            sent=False,
            phone=number,
            message=message,
            error=f"pdf: {exc}",
        )

    return CustomerNotifyResult(sent=True, phone=number, message=message)


__all__ = [
    "CustomerNotifyResult",
    "ENV_ENABLED",
    "ENV_MX_PREFIX",
    "customer_whatsapp_enabled",
    "format_customer_phone",
    "format_financing_whatsapp_summary",
    "format_lead_confirmation",
    "notify_financing_quote",
    "notify_lead_confirmation",
    "send_whatsapp_message",
]
