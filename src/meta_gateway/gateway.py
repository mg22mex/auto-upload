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
        """Process one financing request. Non-financial messages are ignored."""
        context = event.context
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
        if not financial:
            return {"status": "ignored", "sender_id": event.sender_id}

        vehicle_name = str(
            _context_value(context, "vehicle_name", "vehicle", "title", "name") or ""
        ).strip()
        if not vehicle_name:
            match = VEHICLE_PATTERN.search(event.text)
            if match:
                vehicle_name = match.group(1).strip(" .,-")

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

        if not vehicle_name:
            reply = (
                "Para cotizar necesito el vehículo (marca, modelo y año). "
                "Envíamelo junto con el plazo deseado: 12, 24, 36 o 48 meses."
            )
            self.messenger.send_text_message(event.sender_id, reply)
            return {"status": "needs_vehicle", "sender_id": event.sender_id}

        self.odoo.authenticate()
        if price is None or price <= 0:
            inventory = self.odoo.search_vehicle_inventory(vehicle_name)
            priced = [
                item
                for item in inventory
                if Decimal(str(item.get("list_price") or 0)) > 0
            ]
            if not priced:
                reply = (
                    f"No encontré un precio activo para {vehicle_name}. "
                    "Compárteme el enlace del vehículo o su precio publicado."
                )
                self.messenger.send_text_message(event.sender_id, reply)
                return {"status": "needs_price", "sender_id": event.sender_id}
            selected = priced[0]
            price = Decimal(str(selected["list_price"]))
            vehicle_name = str(selected.get("name") or vehicle_name)

        quote = self.quote_engine.calculate(
            price,
            term_months,
            down_payment=down_payment,
        )
        lead_name = str(
            _context_value(context, "customer_name", "lead_name")
            or f"Prospecto Messenger {event.sender_id}"
        ).strip()
        lead_id = self.odoo.create_or_update_lead(
            lead_name,
            f"messenger:{event.sender_id}",
            vehicle_name,
            self.branch_id,
        )
        reply = format_messenger_quote(lead_name, vehicle_name, quote)
        chatter_id = self.odoo.post_quote_to_chatter(lead_id, reply)
        graph_response = self.messenger.send_text_message(event.sender_id, reply)
        return {
            "status": "quoted",
            "sender_id": event.sender_id,
            "lead_id": lead_id,
            "chatter_id": chatter_id,
            "estimated_monthly_payment": str(quote.estimated_monthly_payment),
            "graph_response": graph_response,
        }
