"""Automation triggers — CRM stage changes & inbound webhooks.

Wires ``CRMLeadManager`` (lead upsert + location routing) and
``QuotePDFManager`` (PDF → chatter). WhatsApp template dispatch is **queued**
only until Meta Cloud API account IDs are live.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.odoo_sync.client import OdooCRMClient
from src.odoo_sync.crm import (
    CRMLeadManager,
    PRIMARY_BRANCH,
    normalize_crm_branch,
)
from src.odoo_sync.quotes import QuotePDFManager

# Stages that auto-generate quote PDFs (normalized).
QUOTE_STAGES = frozenset(
    {
        "quoted",
        "cotizado",
        "quote generated",
        "cotizacion",
        "cotización",
        "quote",
    }
)

# Inbound event types handled by process_incoming_webhook.
WEBHOOK_EVENT_LEAD = frozenset(
    {
        "lead",
        "lead_form",
        "form",
        "inquiry",
        "vehicle_inquiry",
        "voice",
        "voice_note",
        "voice_lead",
        "create_lead",
        "crm.lead",
    }
)


def _normalize_stage(stage: str | None) -> str:
    text = (stage or "").strip().lower()
    text = (
        text.replace("á", "a")
        .replace("é", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ú", "u")
    )
    return " ".join(text.replace("_", " ").replace("-", " ").split())


def is_quote_stage(stage: str | None) -> bool:
    return _normalize_stage(stage) in QUOTE_STAGES


def extract_vehicle_from_lead(lead_data: dict[str, Any]) -> dict[str, Any]:
    """Build vehicle dict for QuotePDFManager from free-form lead payload."""
    nested = lead_data.get("vehicle") if isinstance(lead_data.get("vehicle"), dict) else {}
    interest = (
        lead_data.get("vehicle_interest")
        if isinstance(lead_data.get("vehicle_interest"), dict)
        else {}
    )
    src = {**interest, **nested, **lead_data}
    name = str(
        src.get("vehicle_name")
        or src.get("vehicle_info")
        or src.get("name")
        or nested.get("name")
        or interest.get("name")
        or "Vehículo"
    ).strip()
    return {
        "name": name,
        "vehicle_name": name,
        "year": src.get("year"),
        "make": src.get("make") or src.get("brand"),
        "model": src.get("model"),
        "vin": src.get("vin") or src.get("vin_sn"),
        "sku": src.get("sku") or src.get("autosell_id"),
        "autosell_id": src.get("autosell_id") or src.get("sku"),
        "mileage_km": src.get("mileage_km") or src.get("mileage"),
        "transmission": src.get("transmission"),
        "features": src.get("features") or [],
        "photos": src.get("photos") or src.get("image_urls") or [],
        "physical_location": src.get("physical_location")
        or src.get("vehicle_location")
        or src.get("ubicacion"),
        "price": src.get("vehicle_price") or src.get("price") or src.get("list_price"),
    }


def extract_quote_from_lead(lead_data: dict[str, Any]) -> dict[str, Any]:
    nested = lead_data.get("quote") if isinstance(lead_data.get("quote"), dict) else {}
    src = {**nested, **lead_data}
    price = src.get("vehicle_price") or src.get("price") or src.get("list_price") or 0
    return {
        "vehicle_price": price,
        "down_payment": src.get("down_payment") or src.get("enganche") or 0,
        "cash_down_payment": src.get("cash_down_payment")
        or src.get("down_payment")
        or 0,
        "net_trade_in_equity": src.get("net_trade_in_equity") or 0,
        "financed_principal": src.get("financed_principal")
        or src.get("amount_to_finance")
        or 0,
        "origination_fee": src.get("origination_fee") or 0,
        "term_months": src.get("term_months") or src.get("term") or 36,
        "estimated_monthly_payment": src.get("estimated_monthly_payment")
        or src.get("monthly_payment")
        or 0,
        "monthly_admin_fee": src.get("monthly_admin_fee") or 0,
    }


def extract_client_from_lead(lead_data: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": str(
            lead_data.get("client_name")
            or lead_data.get("name")
            or lead_data.get("contact_name")
            or lead_data.get("caller_name")
            or "Cliente"
        ).strip(),
        "phone": str(
            lead_data.get("phone")
            or lead_data.get("mobile")
            or lead_data.get("caller_phone")
            or ""
        ).strip(),
        "email": str(
            lead_data.get("email_from")
            or lead_data.get("email")
            or ""
        ).strip(),
    }


def build_whatsapp_queue_item(
    *,
    lead_id: int,
    template_name: str,
    phone: str,
    variables: dict[str, Any] | None = None,
    branch: str = PRIMARY_BRANCH,
) -> dict[str, Any]:
    """WhatsApp template job — ready when Meta Cloud API accounts are live."""
    return {
        "status": "queued_pending_meta",
        "channel": "odoo_whatsapp_template",
        "template_name": template_name,
        "lead_id": int(lead_id),
        "phone": phone,
        "branch": normalize_crm_branch(branch),
        "variables": dict(variables or {}),
        "meta": {
            "note": (
                "Meta Cloud API / ODOO_WA_ACCOUNT_* pending — "
                "do not send until credentials are live"
            ),
            "ready": False,
        },
    }


@dataclass
class OdooTriggerManager:
    """Stage-change and webhook automation entrypoint."""

    client: OdooCRMClient | None = None
    dry_run: bool | None = None
    crm: CRMLeadManager | None = None
    quotes: QuotePDFManager | None = None
    _client: OdooCRMClient = field(init=False, repr=False)
    _crm: CRMLeadManager = field(init=False, repr=False)
    _quotes: QuotePDFManager = field(init=False, repr=False)
    whatsapp_queue: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._client = self.client or OdooCRMClient()
        if self.dry_run is not None:
            self._client.dry_run = bool(self.dry_run)
        self._crm = self.crm or CRMLeadManager(
            client=self._client, dry_run=self.dry_run
        )
        self._quotes = self.quotes or QuotePDFManager(
            client=self._client, dry_run=self.dry_run
        )

    @property
    def odoo(self) -> OdooCRMClient:
        return self._client

    def _use_dry_run(self) -> bool:
        return self._client._use_dry_run(self.dry_run)

    def on_lead_stage_change(
        self,
        lead_id: int,
        new_stage: str,
        lead_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """React to CRM stage changes.

        For ``quoted`` / ``cotizado``: render + attach quote PDF, queue WA template.
        """
        use_dry = self._use_dry_run()
        lead_data = dict(lead_data or {})
        stage_norm = _normalize_stage(new_stage)

        result: dict[str, Any] = {
            "ok": True,
            "lead_id": int(lead_id),
            "stage": new_stage,
            "stage_normalized": stage_norm,
            "action": "none",
            "quote": None,
            "whatsapp": None,
            "dry_run": use_dry,
            "error": None,
        }

        if not is_quote_stage(new_stage):
            result["action"] = "ignored"
            result["reason"] = f"stage {new_stage!r} is not a quote trigger"
            print(
                f"OdooTriggerManager stage={new_stage!r} lead={lead_id}: ignored"
            )
            return result

        vehicle = extract_vehicle_from_lead(lead_data)
        quote = extract_quote_from_lead(lead_data)
        client_info = extract_client_from_lead(lead_data)
        branch = str(
            lead_data.get("branch")
            or vehicle.get("physical_location")
            or PRIMARY_BRANCH
        )
        physical_location = (
            vehicle.get("physical_location")
            or lead_data.get("physical_location")
            or lead_data.get("vehicle_location")
        )

        if use_dry:
            print(
                f"DRY-RUN OdooTriggerManager.on_lead_stage_change "
                f"lead={lead_id} stage={stage_norm!r} branch={branch!r}"
            )

        try:
            quote_result = self._quotes.render_and_attach(
                int(lead_id),
                vehicle,
                quote,
                client_info,
                branch=branch,
                physical_location=(
                    str(physical_location) if physical_location else None
                ),
            )
        except Exception as exc:
            result["ok"] = False
            result["action"] = "quote_failed"
            result["error"] = str(exc)
            print(f"WARN on_lead_stage_change quote lead={lead_id}: {exc}")
            return result

        result["quote"] = quote_result
        result["action"] = "quote_generated"

        # Queue WhatsApp — never live-send while Meta is paused.
        phone = client_info.get("phone") or ""
        monthly = quote.get("estimated_monthly_payment")
        wa_item = build_whatsapp_queue_item(
            lead_id=int(lead_id),
            template_name="payment_link",
            phone=phone,
            branch=str(quote_result.get("branch") or branch),
            variables={
                "vehicle": vehicle.get("name"),
                "vin": vehicle.get("vin"),
                "estimated_monthly_payment": monthly,
                "client_name": client_info.get("name"),
                "attachment_id": (quote_result.get("attach") or {}).get(
                    "attachment_id"
                ),
            },
        )
        if use_dry:
            wa_item["dry_run"] = True
        self.whatsapp_queue.append(wa_item)
        result["whatsapp"] = wa_item

        if not quote_result.get("ok"):
            result["ok"] = False
            result["error"] = quote_result.get("error") or "quote render/attach failed"

        print(
            f"OdooTriggerManager stage={stage_norm} lead={lead_id}: "
            f"quote ok={quote_result.get('ok')} wa_queued=True dry_run={use_dry}"
        )
        return result

    def process_webhook(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Instance entry for inbound webhooks (forms / voice / inquiries)."""
        return process_incoming_webhook(
            payload,
            manager=self,
            dry_run=self._use_dry_run(),
        )


def process_incoming_webhook(
    payload: dict[str, Any],
    *,
    manager: OdooTriggerManager | None = None,
    dry_run: bool | None = None,
    crm: CRMLeadManager | None = None,
) -> dict[str, Any]:
    """Handle inbound lead form / vehicle inquiry / voice note payload.

    Dispatches into ``CRMLeadManager.create_or_update_lead`` with physical
    location routing. Optionally runs quote stage automation when
    ``stage`` / ``trigger_quote`` indicates a quoted lead.
    """
    if not isinstance(payload, dict):
        return {
            "ok": False,
            "error": "payload must be a dict",
            "event": None,
        }

    event = str(
        payload.get("event")
        or payload.get("type")
        or payload.get("event_type")
        or "lead"
    ).strip().lower()
    # Nested data wrappers
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    lead_payload = dict(data)
    # Promote common envelope fields
    for key in (
        "client_name",
        "name",
        "phone",
        "email",
        "email_from",
        "vehicle_info",
        "vehicle_name",
        "vehicle",
        "vin",
        "plate",
        "branch",
        "physical_location",
        "vehicle_location",
        "channel",
        "notes",
        "description",
        "stage",
        "new_stage",
        "lead_id",
        "trigger_quote",
        "quote",
        "term_months",
        "vehicle_price",
        "down_payment",
        "estimated_monthly_payment",
        "fleet_vehicle_id",
    ):
        if key in payload and key not in lead_payload:
            lead_payload[key] = payload[key]

    # Voice note shorthand
    if event in {"voice", "voice_note", "voice_lead"} or payload.get("utterance"):
        lead_payload.setdefault("channel", "Voice / Phone")
        if payload.get("utterance") and not lead_payload.get("notes"):
            lead_payload["notes"] = str(payload.get("utterance"))
        if payload.get("caller_name") and not lead_payload.get("client_name"):
            lead_payload["client_name"] = payload["caller_name"]
        if payload.get("caller_phone") and not lead_payload.get("phone"):
            lead_payload["phone"] = payload["caller_phone"]

    branch = str(
        lead_payload.get("branch")
        or payload.get("branch")
        or PRIMARY_BRANCH
    )

    use_mgr = manager
    if use_mgr is None:
        use_mgr = OdooTriggerManager(dry_run=dry_run)
    elif dry_run is not None:
        use_mgr._client.dry_run = bool(dry_run)
        use_mgr.dry_run = bool(dry_run)

    crm_mgr = crm or use_mgr._crm

    result: dict[str, Any] = {
        "ok": True,
        "event": event,
        "lead": None,
        "stage_trigger": None,
        "dry_run": use_mgr._use_dry_run(),
        "error": None,
    }

    # Stage-only webhook (existing lead)
    stage_only = str(
        payload.get("new_stage") or lead_payload.get("new_stage") or ""
    ).strip()
    existing_lead_id = lead_payload.get("lead_id") or payload.get("lead_id")
    if (
        event in {"stage_change", "crm.stage", "lead_stage"}
        or (stage_only and existing_lead_id and not lead_payload.get("phone"))
    ):
        try:
            stage_result = use_mgr.on_lead_stage_change(
                int(existing_lead_id),
                stage_only or str(lead_payload.get("stage") or "quoted"),
                lead_payload,
            )
            result["stage_trigger"] = stage_result
            result["ok"] = bool(stage_result.get("ok"))
            return result
        except Exception as exc:
            result["ok"] = False
            result["error"] = str(exc)
            return result

    if event and event not in WEBHOOK_EVENT_LEAD and event not in {
        "stage_change",
        "crm.stage",
        "lead_stage",
        "*",
        "any",
        "",
    }:
        # Still attempt lead upsert for unknown events that look like leads
        if not (lead_payload.get("phone") or lead_payload.get("caller_phone")):
            result["ok"] = False
            result["error"] = f"unsupported event type: {event!r}"
            return result

    try:
        lead_result = crm_mgr.create_or_update_lead(lead_payload, branch=branch)
        result["lead"] = lead_result
    except Exception as exc:
        result["ok"] = False
        result["error"] = str(exc)
        print(f"WARN process_incoming_webhook create_or_update_lead: {exc}")
        return result

    # Optional auto-quote after create when stage is quote-like
    target_stage = str(
        lead_payload.get("stage")
        or lead_payload.get("new_stage")
        or payload.get("stage")
        or ""
    ).strip()
    trigger_quote = bool(
        lead_payload.get("trigger_quote")
        or payload.get("trigger_quote")
        or is_quote_stage(target_stage)
    )
    if trigger_quote and lead_result.get("lead_id"):
        stage_for_quote = target_stage if is_quote_stage(target_stage) else "quoted"
        # Merge CRM result location into lead payload for PDF branding
        merged = dict(lead_payload)
        if lead_result.get("physical_location"):
            merged.setdefault(
                "physical_location", lead_result.get("physical_location")
            )
        if lead_result.get("branch"):
            merged.setdefault("branch", lead_result.get("branch"))
        stage_result = use_mgr.on_lead_stage_change(
            int(lead_result["lead_id"]),
            stage_for_quote,
            merged,
        )
        result["stage_trigger"] = stage_result
        if not stage_result.get("ok"):
            result["ok"] = False
            result["error"] = stage_result.get("error")

    return result


__all__ = [
    "QUOTE_STAGES",
    "OdooTriggerManager",
    "build_whatsapp_queue_item",
    "extract_client_from_lead",
    "extract_quote_from_lead",
    "extract_vehicle_from_lead",
    "is_quote_stage",
    "process_incoming_webhook",
]
