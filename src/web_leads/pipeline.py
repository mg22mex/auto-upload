"""Web lead → Odoo CRM (RR) → Beatriz customer WA → rep WA."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from src.config import PRIMARY_BRANCH, branch_label
from src.odoo_sync.crm import (
    CRMLeadManager,
    assign_lead_owner,
    normalize_crm_branch,
)
from src.web_leads.imap_poll import (
    FetchedEmail,
    already_processed,
    fetch_unseen_web_leads,
    imap_configured,
    mark_processed,
)
from src.web_leads.models import WebLead
from src.web_leads.parser import parse_webhook_payload

ENV_STAGE = "WEB_LEAD_STAGE_NAME"
ENV_STAGE_ALIAS = "WEB_LEADS_STAGE_NAME"
ENV_DRY_RUN = "WEB_LEADS_DRY_RUN"
ENV_CUSTOMER_WA = "WEB_LEADS_CUSTOMER_WHATSAPP"
DEFAULT_STAGE = "Nuevo / Web Lead"


@dataclass
class WebLeadIngestResult:
    status: str
    lead: WebLead | None = None
    lead_id: int | None = None
    partner_id: int | None = None
    assignment: dict[str, Any] | None = None
    customer_wa: dict[str, Any] | None = None
    rep_wa: dict[str, Any] | None = None
    crm: dict[str, Any] | None = None
    error: str | None = None
    skipped_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "lead": self.lead.as_dict() if self.lead else None,
            "lead_id": self.lead_id,
            "partner_id": self.partner_id,
            "assignment": self.assignment,
            "customer_wa": self.customer_wa,
            "rep_wa": self.rep_wa,
            "crm": self.crm,
            "error": self.error,
            "skipped_reason": self.skipped_reason,
        }


def web_lead_stage_name() -> str:
    raw = (
        (os.getenv(ENV_STAGE) or "").strip()
        or (os.getenv(ENV_STAGE_ALIAS) or "").strip()
        or DEFAULT_STAGE
    )
    return raw or DEFAULT_STAGE


def _dry_run() -> bool:
    raw = (os.getenv(ENV_DRY_RUN) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _customer_wa_enabled() -> bool:
    raw = (os.getenv(ENV_CUSTOMER_WA) or "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def format_beatriz_web_lead_message(lead: WebLead) -> str:
    name = (lead.name or "Cliente").strip() or "Cliente"
    first = name.split()[0]
    vehicle = (lead.vehicle or "").strip() or "vehículo de tu interés"
    return (
        f"Hola {first}, recibimos tu solicitud en Autosell.mx para el {vehicle}. "
        "Soy Beatriz, ¿te gustaría conocer las opciones de financiamiento o "
        "agendar una prueba de manejo?"
    )


def ingest_web_lead(
    lead: WebLead,
    *,
    crm: CRMLeadManager | None = None,
    whatsapp_client: Any | None = None,
    dry_run: bool | None = None,
) -> WebLeadIngestResult:
    """Create/update CRM lead, RR-assign, WhatsApp customer + rep."""
    if lead.message_id and already_processed(lead.message_id):
        return WebLeadIngestResult(
            status="skipped",
            lead=lead,
            skipped_reason="message_id already processed",
        )
    if not lead.ok:
        return WebLeadIngestResult(
            status="skipped",
            lead=lead,
            skipped_reason="missing name or phone",
        )

    use_dry = _dry_run() if dry_run is None else bool(dry_run)
    branch = normalize_crm_branch(lead.branch or PRIMARY_BRANCH)
    stage = web_lead_stage_name()
    notes = "\n".join(
        bit
        for bit in (
            "--- Web form Autosell.mx ---",
            f"Subject: {lead.subject}" if lead.subject else "",
            lead.message,
            f"Message-ID: {lead.message_id}" if lead.message_id else "",
        )
        if bit
    )

    payload: dict[str, Any] = {
        "client_name": lead.name,
        "phone": lead.phone,
        "email": lead.email or False,
        "vehicle_info": lead.vehicle or "Consulta web",
        "channel": "Website",
        "stage_name": stage,
        # CRM owner = Marco (Appointment Setter). Closers get WA via RR notify.
        "assign_round_robin": False,
        "preserve_salesperson": False,
        "description": notes,
        "physical_location": branch_label(branch),
        "opportunity_name": (
            f"Web: {lead.vehicle} - {lead.name}"
            if lead.vehicle
            else f"Web Lead - {lead.name}"
        )[:128],
    }

    from src.odoo_sync.structure import setter_user_id_from_mapping

    setter_id = setter_user_id_from_mapping()

    if use_dry:
        assignment = assign_lead_owner(branch)
        return WebLeadIngestResult(
            status="dry_run",
            lead=lead,
            partner_id=-1,
            assignment={
                **assignment.as_dict(),
                "setter_user_id": setter_id,
                "crm_owner": "setter" if setter_id else "rr_fallback",
            },
            customer_wa={
                "would_send": True,
                "message": format_beatriz_web_lead_message(lead),
            },
            rep_wa={"would_notify": True, "phone": assignment.phone},
            crm={"payload": payload, "branch": branch},
        )

    manager = crm or CRMLeadManager()
    partner_id: int | None = None
    try:
        from src.odoo_sync.appointment_sync import ensure_customer_partner

        partner_id = ensure_customer_partner(
            manager._client,
            name=lead.name,
            phone=lead.phone,
            dry_run=False,
        )
        if partner_id:
            payload["partner_id"] = int(partner_id)
    except Exception as exc:
        print(f"WARN web_leads partner: {exc}", flush=True)

    try:
        crm_result = manager.create_or_update_lead(payload, branch=branch)
    except Exception as exc:
        return WebLeadIngestResult(
            status="error",
            lead=lead,
            partner_id=partner_id,
            error=f"crm: {type(exc).__name__}: {exc}",
        )

    lead_id = int(crm_result.get("lead_id") or 0) or None

    # Bind CRM owner to Marco (setter) when mapped.
    if lead_id and setter_id:
        try:
            manager._client.assign_lead_advisor(int(lead_id), int(setter_id))
            if partner_id:
                try:
                    manager._client.execute_kw(
                        "crm.lead",
                        "write",
                        [[int(lead_id)], {"partner_id": int(partner_id)}],
                    )
                except Exception:
                    pass
        except Exception as exc:
            print(f"WARN web_leads assign setter: {exc}", flush=True)

    # RR closer for WhatsApp alert only (does not overwrite setter ownership).
    assignment = assign_lead_owner(branch)
    assignment_dict = {
        **assignment.as_dict(),
        "setter_user_id": setter_id,
        "crm_owner_user_id": setter_id,
    }

    customer_wa: dict[str, Any] | None = None
    if _customer_wa_enabled():
        try:
            from src.notifications.whatsapp import send_whatsapp_message

            text = format_beatriz_web_lead_message(lead)
            send_whatsapp_message(
                lead.phone,
                text,
                branch=branch,
                client=whatsapp_client,
            )
            customer_wa = {"sent": True, "phone": lead.phone, "message": text}
        except Exception as exc:
            customer_wa = {
                "sent": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
            print(f"WARN web_leads customer WA: {exc}", flush=True)

    rep_wa: dict[str, Any] | None = None
    try:
        from src.notifications.whatsapp_rep import notify_rep

        notice = notify_rep(
            client_phone=lead.phone,
            branch=branch,
            vehicle_interest=lead.vehicle or "Consulta web",
            payment_method=None,
            lead_id=lead_id,
            assignment=assignment,
            whatsapp_client=whatsapp_client,
        )
        rep_wa = notice.as_dict()
    except Exception as exc:
        rep_wa = {"sent": False, "error": f"{type(exc).__name__}: {exc}"}
        print(f"WARN web_leads rep WA: {exc}", flush=True)

    if lead.message_id:
        mark_processed(lead.message_id, lead_id=lead_id, phone=lead.phone)

    return WebLeadIngestResult(
        status="ok",
        lead=lead,
        lead_id=lead_id,
        partner_id=partner_id,
        assignment=assignment_dict,
        customer_wa=customer_wa,
        rep_wa=rep_wa,
        crm=crm_result,
    )


def process_web_lead_batch(
    *,
    dry_run: bool | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """IMAP UNSEEN → ingest each parseable lead."""
    if not imap_configured():
        return {
            "status": "skipped",
            "reason": "imap_not_configured",
            "results": [],
        }
    fetched = fetch_unseen_web_leads(mark_seen=True, limit=limit)
    results: list[dict[str, Any]] = []
    for item in fetched:
        results.append(
            ingest_web_lead(item.lead, dry_run=dry_run).as_dict()
        )
    return {
        "status": "ok",
        "fetched": len(fetched),
        "results": results,
    }


def ingest_webhook_payload(
    payload: dict[str, Any],
    *,
    dry_run: bool | None = None,
) -> WebLeadIngestResult:
    lead = parse_webhook_payload(payload)
    return ingest_web_lead(lead, dry_run=dry_run)


__all__ = [
    "DEFAULT_STAGE",
    "ENV_CUSTOMER_WA",
    "ENV_DRY_RUN",
    "ENV_STAGE",
    "WebLeadIngestResult",
    "format_beatriz_web_lead_message",
    "ingest_web_lead",
    "ingest_webhook_payload",
    "process_web_lead_batch",
    "web_lead_stage_name",
]
