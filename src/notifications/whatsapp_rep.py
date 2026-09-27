"""Direct WhatsApp handoff alert to the assigned sales rep (Evolution API).

Best-effort by design: a failed rep alert must never break the customer-facing
conversation, so every entry point returns a result dict instead of raising.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from src.config import branch_label, normalize_rep_phone
from src.odoo_sync.crm import RepAssignment, assign_lead_owner

PAYMENT_LABELS = {
    "cash": "Contado",
    "financing": "Financiamiento",
    "credit": "Financiamiento",
    "trade_in": "Auto a cambio",
    "financing_trade_in": "Financiamiento + Auto a cambio",
}

ENV_ENABLED = "REP_NOTIFY_ENABLED"


@dataclass
class RepNotifyResult:
    sent: bool
    phone: str = ""
    odoo_id: int | None = None
    branch: str = ""
    message: str = ""
    skipped_reason: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "sent": self.sent,
            "rep_phone": self.phone,
            "rep_odoo_id": self.odoo_id,
            "branch": self.branch,
            "skipped_reason": self.skipped_reason,
            "error": self.error,
        }


def rep_notifications_enabled() -> bool:
    raw = (os.getenv(ENV_ENABLED) or "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def payment_label(payment_method: str | None) -> str:
    key = str(payment_method or "").strip().lower()
    if not key:
        return "Por definir"
    return PAYMENT_LABELS.get(key, key.replace("_", " ").title())


def odoo_lead_url(lead_id: int | None) -> str:
    """Deep link to the lead form; empty when Odoo base URL or id is unknown."""
    base = (os.getenv("ODOO_URL") or "").strip().rstrip("/")
    if not base or not lead_id:
        return ""
    return f"{base}/web#id={int(lead_id)}&model=crm.lead&view_type=form"


def format_rep_notification(
    *,
    client_phone: str,
    vehicle_interest: str = "",
    payment_method: str | None = None,
    branch_name: str = "",
    lead_url: str = "",
    appointment_time: str | None = None,
    valuation_amount: str | None = None,
    monthly_payment: str | None = None,
    tradein_summary: str | None = None,
) -> str:
    """Spanish handoff card sent 1-on-1 to the rep."""
    lines = [
        "🎯 *¡Nuevo Lead Asignado!*",
        f"👤 *Cliente:* {client_phone or 'n/d'}",
        f"🚘 *Auto:* {vehicle_interest or 'Por confirmar'}",
        f"💳 *Modalidad:* {payment_label(payment_method)}",
        f"📍 *Sucursal:* {branch_name or 'Periférico'}",
    ]
    tradein = (tradein_summary or "").strip()
    if tradein:
        lines.append(f"🔄 *Auto a cambio:* {tradein}")
    if valuation_amount:
        val = str(valuation_amount).strip()
        if not tradein or val not in tradein:
            lines.append(f"💵 *Valor auto a cambio:* {val}")
    if monthly_payment:
        lines.append(f"📅 *Mensualidad estimada:* {monthly_payment}")
    if appointment_time:
        lines.append(f"🗓️ *Cita solicitada:* {appointment_time}")
    lines.append(f"🔗 *Odoo Lead:* {lead_url or 'n/d'}")
    return "\n".join(lines)


def format_appointment_lead_alert(
    *,
    customer_name: str,
    phone: str,
    interested_vehicle: str | None = None,
    branch_name: str = "",
    appointment_date: str | None = None,
    financing_summary: str | None = None,
    tradein_summary: str | None = None,
    valuation_amount: str | None = None,
    stage_name: str | None = None,
) -> str:
    """Instant WhatsApp alert when Beatriz registers a lead / cita."""
    stage = (stage_name or "").strip() or (
        "Beatriz Cita" if (appointment_date or "").strip() else "Beatriz Lead"
    )
    lines = [
        "🚨 ¡NUEVO LEAD EN REGISTRO!",
        f"• Stage: {stage}",
        f"• Cliente: {(customer_name or '').strip() or 'n/d'}",
        f"• Teléfono: {(phone or '').strip() or 'n/d'}",
        f"• Vehículo: {(interested_vehicle or '').strip() or 'Por confirmar'}",
        f"• Sucursal: {(branch_name or '').strip() or 'Periférico'}",
    ]
    appt = (appointment_date or "").strip()
    if appt:
        lines.append(f"• Fecha/Hora Cita: {appt}")
    fin = (financing_summary or "").strip()
    if fin:
        lines.append(f"• Financiamiento: {fin}")
    tradein = (tradein_summary or "").strip()
    if tradein:
        lines.append(f"• Auto a cambio: {tradein}")
    valuation = (valuation_amount or "").strip()
    if valuation and (not tradein or valuation not in tradein):
        lines.append(f"• Valor Autométrica: {valuation}")
    return "\n".join(lines)


def notify_rep(
    *,
    client_phone: str,
    branch: str | None = None,
    tag: str | None = None,
    vehicle_interest: str = "",
    payment_method: str | None = None,
    lead_id: int | None = None,
    lead_url: str | None = None,
    assignment: RepAssignment | None = None,
    whatsapp_client: Any | None = None,
    appointment_time: str | None = None,
    valuation_amount: str | None = None,
    monthly_payment: str | None = None,
    tradein_summary: str | None = None,
) -> RepNotifyResult:
    """Round-robin a rep for the branch and WhatsApp them the lead card."""
    if not rep_notifications_enabled():
        return RepNotifyResult(sent=False, skipped_reason=f"{ENV_ENABLED}=false")

    pick = assignment or assign_lead_owner(branch, tag=tag)
    rep_phone = normalize_rep_phone(pick.phone)
    if not rep_phone:
        from src.assigner import resolve_roster_phone

        rep_phone = resolve_roster_phone(
            branch=pick.branch or branch,
            odoo_id=pick.odoo_id,
            rep_name=pick.rep_name,
        )
    if not rep_phone:
        fresh = assign_lead_owner(branch or pick.branch, tag=tag)
        rep_phone = normalize_rep_phone(fresh.phone) or resolve_roster_phone(
            branch=fresh.branch,
            odoo_id=fresh.odoo_id,
            rep_name=fresh.rep_name,
        )
        if rep_phone:
            pick = fresh
    if not rep_phone:
        return RepNotifyResult(
            sent=False,
            branch=pick.branch,
            odoo_id=pick.odoo_id,
            skipped_reason="no rep phone configured for branch",
        )

    message = format_rep_notification(
        client_phone=client_phone,
        vehicle_interest=vehicle_interest,
        payment_method=payment_method,
        branch_name=branch_label(pick.branch),
        lead_url=lead_url if lead_url is not None else odoo_lead_url(lead_id),
        appointment_time=appointment_time,
        valuation_amount=valuation_amount,
        monthly_payment=monthly_payment,
        tradein_summary=tradein_summary,
    )

    client = whatsapp_client
    if client is None:
        from src.whatsapp_worker.client import WhatsAppWorkerClient

        client = WhatsAppWorkerClient()

    try:
        client.send_text_message(rep_phone, message, branch=pick.branch)
    except Exception as exc:
        print(
            f"WARN rep notify failed (branch={pick.branch}): {type(exc).__name__}: {exc}",
            flush=True,
        )
        return RepNotifyResult(
            sent=False,
            phone=rep_phone,
            odoo_id=pick.odoo_id,
            branch=pick.branch,
            message=message,
            error=str(exc),
        )

    return RepNotifyResult(
        sent=True,
        phone=rep_phone,
        odoo_id=pick.odoo_id,
        branch=pick.branch,
        message=message,
    )


def notify_appointment_rep(
    *,
    customer_name: str,
    client_phone: str,
    branch: str | None = None,
    interested_vehicle: str | None = None,
    appointment_date: str | None = None,
    financing_summary: str | None = None,
    tradein_summary: str | None = None,
    valuation_amount: str | None = None,
    stage_name: str | None = None,
    lead_id: int | None = None,
    assignment: RepAssignment | None = None,
    whatsapp_client: Any | None = None,
) -> RepNotifyResult:
    """Round-robin (or reuse ``assignment``) + WhatsApp Beatriz lead alert to agent."""
    if not rep_notifications_enabled():
        return RepNotifyResult(sent=False, skipped_reason=f"{ENV_ENABLED}=false")

    pick = assignment or assign_lead_owner(branch)
    rep_phone = normalize_rep_phone(pick.phone)
    if not rep_phone:
        # Phone-only desk / cross-branch CRM user_id → resolve from REPS_*.
        from src.assigner import resolve_roster_phone

        rep_phone = resolve_roster_phone(
            branch=pick.branch or branch,
            odoo_id=pick.odoo_id,
            rep_name=pick.rep_name,
        )
    if not rep_phone:
        # Last resort: rotate a fresh roster pick that has a phone.
        fresh = assign_lead_owner(branch or pick.branch)
        rep_phone = normalize_rep_phone(fresh.phone) or resolve_roster_phone(
            branch=fresh.branch,
            odoo_id=fresh.odoo_id,
            rep_name=fresh.rep_name,
        )
        if rep_phone:
            pick = fresh
    if not rep_phone:
        return RepNotifyResult(
            sent=False,
            branch=pick.branch,
            odoo_id=pick.odoo_id,
            skipped_reason="no rep phone configured for branch",
        )

    message = format_appointment_lead_alert(
        customer_name=customer_name,
        phone=client_phone,
        interested_vehicle=interested_vehicle,
        branch_name=branch_label(pick.branch),
        appointment_date=appointment_date,
        financing_summary=financing_summary,
        tradein_summary=tradein_summary,
        valuation_amount=valuation_amount,
        stage_name=stage_name,
    )

    client = whatsapp_client
    if client is None:
        from src.whatsapp_worker.client import WhatsAppWorkerClient

        client = WhatsAppWorkerClient()

    try:
        client.send_text_message(rep_phone, message, branch=pick.branch)
    except Exception as exc:
        print(
            f"WARN appointment rep notify failed (branch={pick.branch} "
            f"lead={lead_id}): {type(exc).__name__}: {exc}",
            flush=True,
        )
        return RepNotifyResult(
            sent=False,
            phone=rep_phone,
            odoo_id=pick.odoo_id,
            branch=pick.branch,
            message=message,
            error=str(exc),
        )

    return RepNotifyResult(
        sent=True,
        phone=rep_phone,
        odoo_id=pick.odoo_id,
        branch=pick.branch,
        message=message,
    )


__all__ = [
    "ENV_ENABLED",
    "RepNotifyResult",
    "format_appointment_lead_alert",
    "format_rep_notification",
    "notify_appointment_rep",
    "notify_rep",
    "odoo_lead_url",
    "payment_label",
    "rep_notifications_enabled",
]
