"""Facebook Page Messenger webhook parsing and quote orchestration."""
from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs

from src.meta_gateway.client import MessengerClient
from src.meta_gateway.messenger_autoreply import facebook_to_whatsapp_redirect
from src.odoo_sync.client import OdooCRMClient
from src.quote_engine.engine import CalibratedQuoteEngine


FINANCE_TERMS = re.compile(
    r"\b(cot[ií]za\w*|financia(?:r|miento)?|cr[eé]dito|"
    r"mensualidad|enganche|plazo|meses)\b",
    re.IGNORECASE,
)
TERM_PATTERN = re.compile(r"\b(12|24|36|48|60|72)\s*mes(?:es)?\b", re.IGNORECASE)
PRICE_PATTERN = re.compile(
    r"(?:precio\s*[:=]?\s*|\$\s*)(\d[\d,\s]*(?:\.\d{1,2})?)",
    re.IGNORECASE,
)
DOWN_PATTERN = re.compile(
    r"enganche\s*[:=]?\s*\$?\s*(\d[\d,\s]*(?:\.\d{1,2})?)",
    re.IGNORECASE,
)
VEHICLE_PATTERN = re.compile(
    r"(?:veh[ií]culo|auto|coche|para|de)\s*[:=]?\s*"
    r"(.+?)(?=\s+(?:a\s+)?(?:12|24|36|48|60|72)\s*mes|"
    r"\s+precio|\s+enganche|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MessengerEvent:
    sender_id: str
    text: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LeadAdEvent:
    """Facebook Lead Ads (leadgen) webhook change."""

    leadgen_id: str
    page_id: str = ""
    form_id: str = ""
    ad_id: str = ""
    created_time: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def parse_leadgen_events(payload: dict[str, Any]) -> list[LeadAdEvent]:
    """Extract Lead Ads ``leadgen`` changes from a Page webhook payload."""
    if not isinstance(payload, dict) or payload.get("object") != "page":
        return []
    events: list[LeadAdEvent] = []
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        page_id = str(entry.get("id") or "").strip()
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            if str(change.get("field") or "") != "leadgen":
                continue
            value = change.get("value") or {}
            if not isinstance(value, dict):
                continue
            leadgen_id = str(
                value.get("leadgen_id") or value.get("lead_id") or ""
            ).strip()
            if not leadgen_id:
                continue
            events.append(
                LeadAdEvent(
                    leadgen_id=leadgen_id,
                    page_id=page_id or str(value.get("page_id") or ""),
                    form_id=str(value.get("form_id") or ""),
                    ad_id=str(value.get("ad_id") or ""),
                    created_time=str(value.get("created_time") or ""),
                    raw=dict(value),
                )
            )
    return events


def _number(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    cleaned = re.sub(r"[^\d.-]", "", str(value))
    if not cleaned:
        return None
    return Decimal(cleaned)


def _decode_context(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str) or not raw.strip():
        return {}
    value = raw.strip()
    try:
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    except (TypeError, ValueError):
        pass
    parsed = parse_qs(value, keep_blank_values=False)
    return {key: values[-1] for key, values in parsed.items() if values}


def _merge_context(target: dict[str, Any], raw: Any) -> None:
    for key, value in _decode_context(raw).items():
        if value not in (None, ""):
            target.setdefault(key, value)


def parse_messenger_events(payload: dict[str, Any]) -> list[MessengerEvent]:
    """Extract sender, text, and referral/attachment vehicle context."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    if payload.get("object") != "page":
        return []

    events: list[MessengerEvent] = []
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for item in entry.get("messaging") or []:
            if not isinstance(item, dict):
                continue
            message = item.get("message") or {}
            if not isinstance(message, dict) or message.get("is_echo"):
                continue
            sender = item.get("sender") or {}
            sender_id = str(sender.get("id") or "").strip()
            if not sender_id:
                continue

            context: dict[str, Any] = {}
            _merge_context(context, (message.get("quick_reply") or {}).get("payload"))
            _merge_context(context, (item.get("postback") or {}).get("payload"))
            _merge_context(context, (item.get("referral") or {}).get("ref"))

            for attachment in message.get("attachments") or []:
                if not isinstance(attachment, dict):
                    continue
                attachment_payload = attachment.get("payload") or {}
                _merge_context(context, attachment_payload)
                if isinstance(attachment_payload, dict):
                    for key in ("url", "title", "vehicle_name", "vehicle_price"):
                        value = attachment_payload.get(key)
                        if value not in (None, ""):
                            context.setdefault(key, value)

            events.append(
                MessengerEvent(
                    sender_id=sender_id,
                    text=str(message.get("text") or "").strip(),
                    context=context,
                )
            )
    return events


def _context_value(context: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = context.get(key)
        if value not in (None, ""):
            return value
    return None


def _money(value: Decimal) -> str:
    return f"${value:,.2f}"


def format_messenger_quote(name: str, vehicle_name: str, quote: Any) -> str:
    """Format a compact Messenger-safe quote and next step."""
    return "\n".join(
        [
            f"Hola {name}.",
            f"Cotización Autosell — {vehicle_name}",
            f"Precio: {_money(quote.vehicle_price)}",
            f"Enganche: {_money(quote.down_payment)}",
            f"Plazo: {quote.term_months} meses",
            f"Pago mensual estimado: {_money(quote.estimated_monthly_payment)}",
            "",
            "Cotización informativa, sujeta a aprobación crediticia.",
            "Responde “asesor” para continuar con tu solicitud.",
        ]
    )


class MetaWebhookGateway:
    """Messenger event → local quote → Odoo lead/chatter → Graph API reply."""

    ENV_VERIFY_TOKEN = "FB_VERIFY_TOKEN"

    def __init__(
        self,
        *,
        verify_token: str | None = None,
        quote_engine: CalibratedQuoteEngine | None = None,
        odoo: OdooCRMClient | None = None,
        messenger: MessengerClient | None = None,
        branch_id: int | None = None,
    ) -> None:
        self.verify_token = (
            verify_token or os.getenv(self.ENV_VERIFY_TOKEN, "")
        ).strip()
        self.quote_engine = quote_engine or CalibratedQuoteEngine()
        self.odoo = odoo or OdooCRMClient()
        self.messenger = messenger or MessengerClient()
        self.branch_id = int(
            branch_id
            or os.getenv("META_DEFAULT_BRANCH_ID")
            or os.getenv("VOICE_DEFAULT_BRANCH_ID")
            or 1
        )

    def verify(self, mode: str, token: str) -> bool:
        return bool(
            mode == "subscribe"
            and self.verify_token
            and secrets.compare_digest(token or "", self.verify_token)
        )

    def process_event(self, event: MessengerEvent) -> dict[str, Any]:
        """Create Odoo lead (FB Messenger attribution) + WhatsApp redirect reply.

        Financing quotes remain available when the user sends cotización cues;
        every inbound message still receives the WA redirect CTA.
        """
        context = event.context
        vehicle_name = str(
            _context_value(context, "vehicle_name", "vehicle", "title", "name") or ""
        ).strip()
        if not vehicle_name:
            match = VEHICLE_PATTERN.search(event.text)
            if match:
                vehicle_name = match.group(1).strip(" .,-")
        lead_name = str(
            _context_value(context, "customer_name", "lead_name")
            or f"Prospecto Messenger {event.sender_id}"
        ).strip()

        reply = facebook_to_whatsapp_redirect(
            name=lead_name,
            vehicle=vehicle_name,
        )

        lead_id: int | None = None
        chatter_id: int | None = None
        try:
            self.odoo.authenticate()
            lead_result = self.odoo.create_or_update_lead(
                lead_name,
                f"messenger:{event.sender_id}",
                vehicle_name or "Consulta Facebook Messenger",
                self.branch_id,
                stage_name="Nuevo / Web Lead",
                channel="facebook_messenger",
                quote_summary=reply,
            )
            lead_id = lead_result.lead_id
            try:
                chatter_id = self.odoo.post_quote_to_chatter(
                    int(lead_id),
                    f"FB Messenger → WA redirect\n{reply}",
                )
            except Exception:
                chatter_id = None
        except Exception as exc:
            print(f"WARN meta messenger CRM: {type(exc).__name__}: {exc}", flush=True)

        graph_response = self.messenger.send_text_message(event.sender_id, reply)

        # Optional financing quote path (does not replace the WA redirect).
        financial = bool(
            FINANCE_TERMS.search(event.text)
            or _context_value(
                context,
                "vehicle_price",
                "price",
                "term",
                "term_months",
                "down_payment",
            )
        )
        quote_meta: dict[str, Any] = {}
        if financial and vehicle_name:
            try:
                quote_meta = self._maybe_attach_quote(
                    event=event,
                    lead_name=lead_name,
                    vehicle_name=vehicle_name,
                    lead_id=lead_id,
                )
            except Exception as exc:
                quote_meta = {"quote_error": str(exc)}

        return {
            "status": "redirected_whatsapp",
            "sender_id": event.sender_id,
            "lead_id": lead_id,
            "chatter_id": chatter_id,
            "channel": "facebook_messenger",
            "reply": reply,
            "graph_response": graph_response,
            **quote_meta,
        }

    def _maybe_attach_quote(
        self,
        *,
        event: MessengerEvent,
        lead_name: str,
        vehicle_name: str,
        lead_id: int | None,
    ) -> dict[str, Any]:
        context = event.context
        term_raw = _context_value(context, "term_months", "term")
        term_match = TERM_PATTERN.search(event.text)
        term_months = int(term_raw or (term_match.group(1) if term_match else 36))
        price = _number(_context_value(context, "vehicle_price", "price"))
        if price is None:
            price_match = PRICE_PATTERN.search(event.text)
            if price_match:
                price = _number(price_match.group(1))
        down_payment = _number(_context_value(context, "down_payment", "down"))
        if down_payment is None:
            down_match = DOWN_PATTERN.search(event.text)
            if down_match:
                down_payment = _number(down_match.group(1))
        if price is None or price <= 0:
            inventory = self.odoo.search_vehicle_inventory(vehicle_name)
            priced = [
                item
                for item in inventory
                if Decimal(str(item.get("list_price") or 0)) > 0
            ]
            if not priced:
                return {"quote_status": "needs_price"}
            selected = priced[0]
            price = Decimal(str(selected["list_price"]))
            vehicle_name = str(selected.get("name") or vehicle_name)

        from src.quote_engine.term_limits import extract_model_year

        quote = self.quote_engine.calculate(
            price,
            term_months,
            down_payment=down_payment,
            vehicle_year=extract_model_year(vehicle_name),
        )
        summary = format_messenger_quote(lead_name, vehicle_name, quote)
        if lead_id:
            try:
                self.odoo.post_quote_to_chatter(int(lead_id), summary)
            except Exception:
                pass
        return {
            "quote_status": "quoted",
            "estimated_monthly_payment": str(quote.estimated_monthly_payment),
        }

    def process_lead_ad(self, event: LeadAdEvent) -> dict[str, Any]:
        """Register a Facebook Lead Ads form submission in Odoo (FB Lead Form)."""
        name = (
            str(event.raw.get("full_name") or event.raw.get("name") or "").strip()
            or f"Lead Ads {event.leadgen_id}"
        )
        phone = str(
            event.raw.get("phone_number")
            or event.raw.get("phone")
            or f"leadgen:{event.leadgen_id}"
        ).strip()
        vehicle = str(
            event.raw.get("vehicle")
            or event.raw.get("vehicle_interest")
            or "Consulta Facebook Lead Ads"
        ).strip()
        reply = facebook_to_whatsapp_redirect(name=name, vehicle=vehicle)
        try:
            self.odoo.authenticate()
            lead_result = self.odoo.create_or_update_lead(
                name,
                phone,
                vehicle,
                self.branch_id,
                stage_name="Nuevo / Web Lead",
                channel="facebook_lead_ads",
                quote_summary=(
                    f"FB Lead Form id={event.leadgen_id} form={event.form_id}\n"
                    f"{reply}"
                ),
            )
            return {
                "status": "lead_ad_registered",
                "leadgen_id": event.leadgen_id,
                "lead_id": lead_result.lead_id,
                "channel": "facebook_lead_ads",
                "reply_template": reply,
            }
        except Exception as exc:
            return {
                "status": "error",
                "leadgen_id": event.leadgen_id,
                "error": f"{type(exc).__name__}: {exc}",
            }
