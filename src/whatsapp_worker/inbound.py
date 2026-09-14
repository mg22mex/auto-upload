"""Parse Evolution API v2 webhooks and run WhatsApp lead qualification."""
from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WA_CHANNEL = "WhatsApp"

# Qualification conversation states
STATE_NEW_LEAD = "NEW_LEAD"
STATE_AWAITING_PAYMENT_METHOD = "AWAITING_PAYMENT_METHOD"
STATE_AWAITING_TRADE_IN = "AWAITING_TRADE_IN"
STATE_AWAITING_DOWN_PAYMENT = "AWAITING_DOWN_PAYMENT"
STATE_AI_ACTIVE = "AI_ACTIVE"
STATE_HANDOFF_TO_HUMAN = "HANDOFF_TO_HUMAN"

PAYMENT_CASH = "cash"
PAYMENT_FINANCING = "financing"
PAYMENT_TRADE_IN = "trade_in"
PAYMENT_FINANCING_TRADE_IN = "financing_trade_in"

_PAYMENT_LABELS = {
    PAYMENT_CASH: "Contado / efectivo",
    PAYMENT_FINANCING: "Financiamiento",
    PAYMENT_TRADE_IN: "Auto a cambio (trade-in)",
    PAYMENT_FINANCING_TRADE_IN: "Financiamiento + Auto a cambio",
}

_JID_SKIP = ("@g.us", "@broadcast", "@newsletter", "status@broadcast")


@dataclass
class WhatsAppInboundEvent:
    phone: str
    name: str
    text: str
    instance: str
    message_id: str


@dataclass
class QualificationSession:
    phone: str
    instance: str
    state: str = STATE_NEW_LEAD
    contact_name: str = ""
    branch: str = "periferico"
    branch_id: int | None = None
    physical_location: str = "Periférico"
    lead_id: int | None = None
    initial_message: str = ""
    payment_method: str = ""
    trade_in_vehicle: str = ""
    down_payment: str = ""
    handling_agent: str = ""
    appointment_time: str = ""
    vehicle_interest: str = ""
    updated_at: str = ""


@dataclass
class QualificationTurnResult:
    session: QualificationSession
    reply_text: str
    odoo_create: bool = False
    odoo_handoff: bool = False
    odoo_notes: str = ""
    appointment_handoff: bool = False
    odoo_stage: str = ""
    routing: dict[str, Any] | None = None
    handoff_result: dict[str, Any] | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def qualification_enabled() -> bool:
    raw = os.getenv("WHATSAPP_QUALIFICATION", "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _phone_from_jid(remote_jid: str) -> str:
    local = (remote_jid or "").split("@", 1)[0]
    return _digits(local)


def _message_text(message: dict[str, Any] | None) -> str:
    if not isinstance(message, dict):
        return ""
    conversation = message.get("conversation")
    if isinstance(conversation, str) and conversation.strip():
        return conversation.strip()
    extended = message.get("extendedTextMessage")
    if isinstance(extended, dict):
        text = extended.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    image = message.get("imageMessage")
    if isinstance(image, dict):
        caption = image.get("caption")
        if isinstance(caption, str) and caption.strip():
            return caption.strip()
    video = message.get("videoMessage")
    if isinstance(video, dict):
        caption = video.get("caption")
        if isinstance(caption, str) and caption.strip():
            return caption.strip()
    return ""


def _iter_data_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        if "key" in data or "message" in data:
            return [data]
        messages = data.get("messages")
        if isinstance(messages, list):
            return [item for item in messages if isinstance(item, dict)]
        if isinstance(messages, dict):
            return [messages]
    return []


def parse_evolution_inbound(payload: dict[str, Any]) -> list[WhatsAppInboundEvent]:
    """Extract inbound user texts from an Evolution webhook body."""
    if not isinstance(payload, dict):
        return []
    event = str(payload.get("event") or payload.get("type") or "").lower()
    if event and "message" not in event and event not in {"messages.upsert", "messages"}:
        return []

    instance = str(payload.get("instance") or payload.get("instanceName") or "").strip()
    events: list[WhatsAppInboundEvent] = []
    seen: set[str] = set()

    for item in _iter_data_items(payload):
        key = item.get("key") if isinstance(item.get("key"), dict) else {}
        if key.get("fromMe") is True:
            continue
        remote_jid = str(key.get("remoteJid") or item.get("remoteJid") or "")
        if not remote_jid or any(skip in remote_jid for skip in _JID_SKIP):
            continue
        phone = _phone_from_jid(remote_jid)
        if len(phone) < 10:
            continue
        text = _message_text(
            item.get("message") if isinstance(item.get("message"), dict) else item
        )
        if not text:
            continue
        message_id = str(key.get("id") or item.get("id") or "")
        dedupe = message_id or f"{phone}:{text}"
        if dedupe in seen:
            continue
        seen.add(dedupe)
        name = str(item.get("pushName") or item.get("pushname") or "").strip() or "WhatsApp"
        events.append(
            WhatsAppInboundEvent(
                phone=phone,
                name=name,
                text=text,
                instance=instance,
                message_id=message_id,
            )
        )
    return events


def inbound_to_voice_payload(event: WhatsAppInboundEvent) -> dict[str, Any]:
    """Shape Evolution text as the Voice AI JSON the pipeline already understands."""
    return {
        "caller_phone": event.phone,
        "caller_name": event.name,
        "transcript": event.text,
        "vehicle_interest": event.text,
        "channel": WA_CHANNEL,
    }


def parse_payment_choice(text: str) -> str | None:
    """Map user reply to cash / financing / trade_in / financing+trade_in."""
    from src.lead_routing import parse_payment_intent

    return parse_payment_intent(text).session_key


def _session_payment_for_trade_in(details: Any) -> str:
    """Prefer combined financing + auto a cambio when both flows are active."""
    if getattr(details, "wants_financing", False):
        return PAYMENT_FINANCING_TRADE_IN
    return PAYMENT_TRADE_IN


def _welcome_message(name: str, branch_label: str, initial_message: str) -> str:
    who = (name or "Cliente").strip()
    branch = (branch_label or "Periférico").strip()
    snippet = (initial_message or "").strip()
    intro = f'Vimos tu mensaje: "{snippet}"\n\n' if snippet else ""
    return (
        f"¡Hola {who}! Gracias por comunicarte con Autosell {branch}. 🚗\n\n"
        f"{intro}"
        "Para ayudarte mejor, ¿cómo te gustaría adquirir tu vehículo?\n"
        "1️⃣ Contado / efectivo\n"
        "2️⃣ Financiamiento\n"
        "3️⃣ Auto a cambio\n\n"
        "Responde con el número o escribe contado, financiamiento o a cambio."
    )


def _payment_retry_message() -> str:
    return (
        "No entendí tu respuesta. Por favor elige una opción:\n"
        "1 Contado · 2 Financiamiento · 3 Auto a cambio\n"
        "También puedes combinar: `2 y 3` o `financiamiento y a cambio`."
    )


def _trade_in_prompt() -> str:
    return (
        "Perfecto. ¿Qué vehículo entregarías a cambio?\n"
        "Indica año, marca, modelo, versión y kilometraje "
        "(ej. 2018 Nissan Sentra Sense 90,000 km)."
    )


def _down_payment_prompt() -> str:
    return (
        "¿Con cuánto enganche cuentas aproximadamente?\n"
        "(ej. $50,000 o 20%)"
    )


def build_qualification_notes(session: QualificationSession) -> str:
    """Odoo description block for handoff."""
    lines = [
        "--- WhatsApp qualification ---",
        f"Estado: {STATE_HANDOFF_TO_HUMAN}",
        f"Mensaje inicial: {session.initial_message or 'n/a'}",
    ]
    if session.payment_method:
        lines.append(
            f"Forma de pago: {_PAYMENT_LABELS.get(session.payment_method, session.payment_method)}"
        )
    if session.trade_in_vehicle:
        lines.append(f"Vehículo a cambio: {session.trade_in_vehicle}")
    if session.down_payment:
        lines.append(f"Enganche indicado: {session.down_payment}")
    lines.append(f"Sucursal: {session.physical_location}")
    return "\n".join(lines)


def _handoff_message(session: QualificationSession) -> str:
    name = (session.contact_name or "Cliente").strip()
    branch = (session.physical_location or "Periférico").strip()
    lines = [
        f"Gracias, {name}. Ya registramos tu información y un asesor de "
        f"Autosell {branch} te contactará en breve. 🙌",
        "",
        "Resumen:",
    ]
    if session.payment_method:
        lines.append(
            f"• Forma de pago: {_PAYMENT_LABELS.get(session.payment_method, session.payment_method)}"
        )
    if session.trade_in_vehicle:
        lines.append(f"• Auto a cambio: {session.trade_in_vehicle}")
    if session.down_payment:
        lines.append(f"• Enganche: {session.down_payment}")
    return "\n".join(lines)


def _post_handoff_message(branch_label: str) -> str:
    return (
        f"Tu solicitud ya está con un asesor de Autosell {branch_label}. "
        "Te contactaremos pronto. 🙌"
    )


def _maybe_queue_voice_after_quote(
    *,
    session: QualificationSession,
    event: WhatsAppInboundEvent,
    quote_meta: dict[str, Any],
    routing: dict[str, Any],
) -> dict[str, Any]:
    """Queue outbound AI voice call once a French amortization quote is ready."""
    if not quote_meta.get("estimated_monthly_payment") and not quote_meta.get(
        "monthly_payment"
    ):
        return routing
    if not quote_meta.get("valor_compra") and not quote_meta.get("valuation_amount"):
        return routing
    from src.whatsapp_worker.client import trigger_outbound_voice_after_quote

    voice = trigger_outbound_voice_after_quote(
        phone_number=session.phone or event.phone,
        lead_id=session.lead_id,
        vehicle_of_interest=str(
            quote_meta.get("vehicle_of_interest")
            or session.vehicle_interest
            or session.initial_message
            or ""
        ),
        valuation_amount=str(
            quote_meta.get("valuation_amount") or quote_meta.get("valor_compra") or ""
        ),
        monthly_payment=str(
            quote_meta.get("monthly_payment")
            or quote_meta.get("estimated_monthly_payment")
            or ""
        ),
        branch=session.branch,
        client_name=session.contact_name or event.name,
        payment_method=session.payment_method
        or str(quote_meta.get("payment_method") or ""),
        trade_in_label=str(quote_meta.get("trade_in_label") or session.trade_in_vehicle),
    )
    return {**routing, "voice_outbound": voice}


def process_qualification_turn(
    event: WhatsAppInboundEvent,
    session: QualificationSession | None,
    *,
    branch: str = "periferico",
    branch_id: int | None = None,
    physical_location: str = "Periférico",
    tags: list[str] | None = None,
) -> QualificationTurnResult:
    """Advance one inbound message through the qualification state machine.

    When ``AI_MG_QUOTE_LEADS`` is on (default), MG Quote Lead conversations stay
    with the AI until an appointment / test-drive request triggers human handoff.
    """
    from src.lead_routing import (
        AGENT_AI,
        STAGE_CITA,
        STAGE_PRIMER_CONTACTO,
        ai_mg_quote_enabled,
        detect_appointment_intent,
        format_ai_reply,
        route_inbound_lead,
    )

    now = _utc_now()
    if session is None:
        session = QualificationSession(
            phone=event.phone,
            instance=event.instance,
            state=STATE_NEW_LEAD,
            contact_name=event.name,
            branch=branch,
            branch_id=branch_id,
            physical_location=physical_location,
            initial_message=event.text.strip(),
            vehicle_interest=event.text.strip(),
            updated_at=now,
        )

    use_ai = False
    if ai_mg_quote_enabled():
        if session.handling_agent == AGENT_AI or session.state == STATE_AI_ACTIVE:
            use_ai = True
        elif session.state == STATE_NEW_LEAD:
            # Fresh WhatsApp quote leads are stamped MG Quote Lead → AI first.
            use_ai = True
        elif tags:
            from src.lead_routing import has_mg_quote_tag

            use_ai = has_mg_quote_tag(tags)

    if use_ai and session.state != STATE_HANDOFF_TO_HUMAN:
        return _process_ai_turn(
            event,
            session,
            now=now,
            tags=tags,
            detect_appointment_intent=detect_appointment_intent,
            format_ai_reply=format_ai_reply,
            route_inbound_lead=route_inbound_lead,
            stage_primer=STAGE_PRIMER_CONTACTO,
            stage_cita=STAGE_CITA,
            agent_ai=AGENT_AI,
        )

    if session.state == STATE_HANDOFF_TO_HUMAN:
        session.updated_at = now
        return QualificationTurnResult(
            session=session,
            reply_text=_post_handoff_message(session.physical_location),
        )

    if session.state == STATE_NEW_LEAD:
        session.state = STATE_AWAITING_PAYMENT_METHOD
        session.updated_at = now
        return QualificationTurnResult(
            session=session,
            reply_text=_welcome_message(
                session.contact_name,
                session.physical_location,
                session.initial_message,
            ),
            odoo_create=True,
            odoo_stage="New",
        )

    if session.state == STATE_AWAITING_PAYMENT_METHOD:
        choice = parse_payment_choice(event.text)
        if choice is None:
            session.updated_at = now
            return QualificationTurnResult(
                session=session,
                reply_text=_payment_retry_message(),
            )
        session.payment_method = choice
        session.updated_at = now
        if choice == PAYMENT_CASH:
            session.state = STATE_HANDOFF_TO_HUMAN
            return QualificationTurnResult(
                session=session,
                reply_text=_handoff_message(session),
                odoo_handoff=True,
                odoo_notes=build_qualification_notes(session),
            )
        if choice in {PAYMENT_TRADE_IN, PAYMENT_FINANCING_TRADE_IN}:
            session.state = STATE_AWAITING_TRADE_IN
            return QualificationTurnResult(
                session=session,
                reply_text=_trade_in_prompt(),
            )
        session.state = STATE_AWAITING_DOWN_PAYMENT
        return QualificationTurnResult(
            session=session,
            reply_text=_down_payment_prompt(),
        )

    if session.state == STATE_AWAITING_TRADE_IN:
        detail = event.text.strip()
        if len(detail) < 4:
            session.updated_at = now
            return QualificationTurnResult(
                session=session,
                reply_text=_trade_in_prompt(),
            )
        session.trade_in_vehicle = detail
        session.state = STATE_HANDOFF_TO_HUMAN
        session.updated_at = now
        return QualificationTurnResult(
            session=session,
            reply_text=_handoff_message(session),
            odoo_handoff=True,
            odoo_notes=build_qualification_notes(session),
        )

    if session.state == STATE_AWAITING_DOWN_PAYMENT:
        detail = event.text.strip()
        if len(detail) < 2:
            session.updated_at = now
            return QualificationTurnResult(
                session=session,
                reply_text=_down_payment_prompt(),
            )
        session.down_payment = detail
        session.state = STATE_HANDOFF_TO_HUMAN
        session.updated_at = now
        return QualificationTurnResult(
            session=session,
            reply_text=_handoff_message(session),
            odoo_handoff=True,
            odoo_notes=build_qualification_notes(session),
        )

    session.state = STATE_HANDOFF_TO_HUMAN
    session.updated_at = now
    return QualificationTurnResult(
        session=session,
        reply_text=_post_handoff_message(session.physical_location),
    )


def _process_ai_turn(
    event: WhatsAppInboundEvent,
    session: QualificationSession,
    *,
    now: str,
    tags: list[str] | None,
    detect_appointment_intent: Any,
    format_ai_reply: Any,
    route_inbound_lead: Any,
    stage_primer: str,
    stage_cita: str,
    agent_ai: str,
) -> QualificationTurnResult:
    """MG Quote Lead AI loop — handoff only on appointment / test-drive intent."""
    appointment = detect_appointment_intent(event.text)
    decision = route_inbound_lead(
        {"tags": tags or ["MG Quote Lead"], "handling_agent": agent_ai},
        tags=tags,
        appointment=appointment,
    )

    if session.state == STATE_NEW_LEAD:
        session.state = STATE_AI_ACTIVE
        session.handling_agent = agent_ai
        if not session.vehicle_interest:
            session.vehicle_interest = session.initial_message or event.text.strip()
        session.updated_at = now

        from src.lead_routing import (
            TradeInDetails,
            advance_trade_in_qualification,
        )

        details, trade_reply, quote_meta = advance_trade_in_qualification(
            event.text,
            prior=None,
            lead_name=session.contact_name or event.name,
            vehicle_interest=session.vehicle_interest,
        )
        if trade_reply is not None:
            session.payment_method = _session_payment_for_trade_in(details)
            session.trade_in_vehicle = details.as_label() or session.trade_in_vehicle
            if quote_meta and quote_meta.get("valor_compra"):
                session.down_payment = str(quote_meta["valor_compra"])
            routing = decision.as_dict()
            if quote_meta:
                routing = {**routing, "trade_in_quote": quote_meta}
                routing = _maybe_queue_voice_after_quote(
                    session=session,
                    event=event,
                    quote_meta=quote_meta,
                    routing=routing,
                )
            return QualificationTurnResult(
                session=session,
                reply_text=trade_reply,
                odoo_create=True,
                odoo_stage=stage_primer,
                routing=routing,
            )

        reply = format_ai_reply(
            name=session.contact_name or event.name,
            text=event.text,
            vehicle_interest=session.vehicle_interest,
            branch_name=session.physical_location,
        )
        return QualificationTurnResult(
            session=session,
            reply_text=reply,
            odoo_create=True,
            odoo_stage=stage_primer,
            routing=decision.as_dict(),
        )

    if appointment.requested:
        session.state = STATE_HANDOFF_TO_HUMAN
        session.handling_agent = "human_rep"
        session.appointment_time = appointment.when_text or appointment.raw
        session.updated_at = now
        notes = build_qualification_notes(session)
        notes += f"\nCita solicitada: {session.appointment_time or 'sin horario'}"
        reply = format_ai_reply(
            name=session.contact_name or event.name,
            text=event.text,
            vehicle_interest=session.vehicle_interest or session.initial_message,
            branch_name=session.physical_location,
            appointment=appointment,
        )
        return QualificationTurnResult(
            session=session,
            reply_text=reply,
            odoo_handoff=True,
            appointment_handoff=True,
            odoo_notes=notes,
            odoo_stage=stage_cita,
            routing=decision.as_dict(),
        )

    # Autométrica auto a cambio → Valor Compra as engache (French amortization)
    from src.lead_routing import (
        TradeInDetails,
        advance_trade_in_qualification,
        parse_payment_intent,
        parse_trade_in_details,
    )

    intent = parse_payment_intent(event.text)
    prior_ti: TradeInDetails | None = None
    prior_trade = session.payment_method in {
        PAYMENT_TRADE_IN,
        PAYMENT_FINANCING_TRADE_IN,
    } or bool(session.trade_in_vehicle)
    if prior_trade:
        prior_ti = parse_trade_in_details(
            session.trade_in_vehicle or "",
            prior=TradeInDetails(
                is_trade_in=True,
                wants_financing=session.payment_method
                in {PAYMENT_FINANCING, PAYMENT_FINANCING_TRADE_IN},
            ),
        )
        prior_ti.is_trade_in = True
        if session.payment_method in {PAYMENT_FINANCING, PAYMENT_FINANCING_TRADE_IN}:
            prior_ti.wants_financing = True
    details, trade_reply, quote_meta = advance_trade_in_qualification(
        event.text,
        prior=prior_ti,
        lead_name=session.contact_name or event.name,
        vehicle_interest=session.vehicle_interest or session.initial_message,
    )
    if trade_reply is not None:
        session.state = STATE_AI_ACTIVE
        session.handling_agent = agent_ai
        if intent.financing or (prior_ti and prior_ti.wants_financing):
            details.wants_financing = True
        session.payment_method = _session_payment_for_trade_in(details)
        session.trade_in_vehicle = details.as_label() or session.trade_in_vehicle
        if quote_meta and quote_meta.get("valor_compra"):
            session.down_payment = str(quote_meta["valor_compra"])
        session.updated_at = now
        routing = decision.as_dict()
        if quote_meta:
            routing = {**routing, "trade_in_quote": quote_meta}
        routing = {
            **routing,
            "payment_intent": {
                "financing": bool(details.wants_financing),
                "trade_in": True,
                "combined": bool(details.wants_financing),
            },
        }
        if quote_meta:
            routing = _maybe_queue_voice_after_quote(
                session=session,
                event=event,
                quote_meta=quote_meta,
                routing=routing,
            )
        return QualificationTurnResult(
            session=session,
            reply_text=trade_reply,
            odoo_stage=stage_primer,
            routing=routing,
        )

    session.state = STATE_AI_ACTIVE
    session.handling_agent = agent_ai
    session.updated_at = now
    reply = format_ai_reply(
        name=session.contact_name or event.name,
        text=event.text,
        vehicle_interest=session.vehicle_interest or session.initial_message,
        branch_name=session.physical_location,
    )
    return QualificationTurnResult(
        session=session,
        reply_text=reply,
        odoo_stage=stage_primer,
        routing=decision.as_dict(),
    )


def apply_qualification_to_odoo(
    odoo: Any,
    event: WhatsAppInboundEvent,
    turn: QualificationTurnResult,
) -> int | None:
    """Create/update the CRM lead behind a qualification turn; returns lead id."""
    session = turn.session
    if not (turn.odoo_create or turn.odoo_handoff):
        return session.lead_id

    from src.lead_routing import STAGE_PRIMER_CONTACTO

    branch_id = int(session.branch_id or os.getenv("VOICE_DEFAULT_BRANCH_ID") or 1)
    odoo.authenticate()
    summary = (
        turn.odoo_notes
        if turn.odoo_handoff
        else f"WhatsApp inbound: {session.initial_message or event.text}"
    )
    stage_name = turn.odoo_stage or (
        STAGE_PRIMER_CONTACTO if turn.odoo_create else "New"
    )
    lead_result = odoo.create_or_update_lead(
        session.contact_name or event.name,
        event.phone,
        session.vehicle_interest or session.initial_message or event.text,
        branch_id,
        quote_summary=summary,
        stage_name=stage_name,
        channel=WA_CHANNEL,
        schedule_follow_up=True,
        user_id=None,
    )
    session.lead_id = lead_result.lead_id
    return lead_result.lead_id


def notify_rep_on_handoff(
    turn: QualificationTurnResult,
    *,
    whatsapp_client: Any | None = None,
    odoo: Any | None = None,
) -> dict[str, Any] | None:
    """Alert the round-robin rep when a turn reaches ``HANDOFF_TO_HUMAN``.

    Appointment handoffs also move the Odoo stage to ``Cita/Prueba de manejo``
    and assign the chosen rep. Non-appointment legacy handoffs only WhatsApp
    the rep. Returns ``None`` when the turn is not a handoff; never raises.
    """
    if not turn.odoo_handoff:
        return None

    session = turn.session
    interest = (
        session.vehicle_interest
        or session.initial_message
        or session.trade_in_vehicle
        or ""
    )

    if turn.appointment_handoff:
        from src.lead_routing import detect_appointment_intent, handoff_appointment_to_rep

        appointment = detect_appointment_intent(
            session.appointment_time or interest
        )
        if session.appointment_time and not appointment.when_text:
            from src.lead_routing import AppointmentIntent

            appointment = AppointmentIntent(
                requested=True,
                kind="cita",
                when_text=session.appointment_time,
                raw=session.appointment_time,
            )
        result = handoff_appointment_to_rep(
            lead_id=session.lead_id,
            client_phone=session.phone,
            branch=session.branch,
            vehicle_interest=interest,
            payment_method=session.payment_method or None,
            appointment=appointment,
            client_name=session.contact_name,
            odoo=odoo,
            whatsapp_client=whatsapp_client,
        )
        payload = result.as_dict()
        turn.handoff_result = payload
        notice = dict(payload.get("rep_notification") or {})
        notice.update(
            {
                "appointment_handoff": True,
                "stage_name": payload.get("stage_name"),
                "stage_updated": payload.get("stage_updated"),
                "advisor_assigned": payload.get("advisor_assigned"),
                "assignment": payload.get("assignment"),
                "error": payload.get("error"),
            }
        )
        return notice

    from src.notifications.whatsapp_rep import notify_rep

    result = notify_rep(
        client_phone=session.phone,
        branch=session.branch,
        vehicle_interest=interest,
        payment_method=session.payment_method,
        lead_id=session.lead_id,
        whatsapp_client=whatsapp_client,
        appointment_time=session.appointment_time or None,
    )
    return result.as_dict()


class QualificationStore:
    """SQLite persistence for per-contact WhatsApp qualification state."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS wa_qualification (
        phone TEXT NOT NULL,
        instance TEXT NOT NULL,
        state TEXT NOT NULL,
        contact_name TEXT NOT NULL DEFAULT '',
        branch TEXT NOT NULL DEFAULT 'periferico',
        branch_id INTEGER,
        physical_location TEXT NOT NULL DEFAULT 'Periférico',
        lead_id INTEGER,
        initial_message TEXT NOT NULL DEFAULT '',
        payment_method TEXT NOT NULL DEFAULT '',
        trade_in_vehicle TEXT NOT NULL DEFAULT '',
        down_payment TEXT NOT NULL DEFAULT '',
        handling_agent TEXT NOT NULL DEFAULT '',
        appointment_time TEXT NOT NULL DEFAULT '',
        vehicle_interest TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        PRIMARY KEY (phone, instance)
    );
    """

    _EXTRA_COLUMNS = (
        ("handling_agent", "TEXT NOT NULL DEFAULT ''"),
        ("appointment_time", "TEXT NOT NULL DEFAULT ''"),
        ("vehicle_interest", "TEXT NOT NULL DEFAULT ''"),
    )

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            db_path = os.getenv("WA_QUALIFICATION_DB_PATH") or "data/wa_qualification.db"
        self.db_path = Path(db_path)
        if str(db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(self._SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        existing = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(wa_qualification)").fetchall()
        }
        for name, decl in self._EXTRA_COLUMNS:
            if name not in existing:
                self._conn.execute(
                    f"ALTER TABLE wa_qualification ADD COLUMN {name} {decl}"
                )

    def close(self) -> None:
        self._conn.close()

    def get(self, phone: str, instance: str) -> QualificationSession | None:
        row = self._conn.execute(
            """
            SELECT phone, instance, state, contact_name, branch, branch_id,
                   physical_location, lead_id, initial_message, payment_method,
                   trade_in_vehicle, down_payment, handling_agent,
                   appointment_time, vehicle_interest, updated_at
            FROM wa_qualification
            WHERE phone = ? AND instance = ?
            """,
            (phone, instance or ""),
        ).fetchone()
        if row is None:
            return None
        keys = set(row.keys())
        return QualificationSession(
            phone=str(row["phone"]),
            instance=str(row["instance"]),
            state=str(row["state"]),
            contact_name=str(row["contact_name"] or ""),
            branch=str(row["branch"] or "periferico"),
            branch_id=int(row["branch_id"]) if row["branch_id"] is not None else None,
            physical_location=str(row["physical_location"] or "Periférico"),
            lead_id=int(row["lead_id"]) if row["lead_id"] is not None else None,
            initial_message=str(row["initial_message"] or ""),
            payment_method=str(row["payment_method"] or ""),
            trade_in_vehicle=str(row["trade_in_vehicle"] or ""),
            down_payment=str(row["down_payment"] or ""),
            handling_agent=str(row["handling_agent"] or "") if "handling_agent" in keys else "",
            appointment_time=str(row["appointment_time"] or "") if "appointment_time" in keys else "",
            vehicle_interest=str(row["vehicle_interest"] or "") if "vehicle_interest" in keys else "",
            updated_at=str(row["updated_at"] or ""),
        )

    def save(self, session: QualificationSession) -> None:
        self._conn.execute(
            """
            INSERT INTO wa_qualification (
                phone, instance, state, contact_name, branch, branch_id,
                physical_location, lead_id, initial_message, payment_method,
                trade_in_vehicle, down_payment, handling_agent,
                appointment_time, vehicle_interest, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(phone, instance) DO UPDATE SET
                state = excluded.state,
                contact_name = excluded.contact_name,
                branch = excluded.branch,
                branch_id = excluded.branch_id,
                physical_location = excluded.physical_location,
                lead_id = excluded.lead_id,
                initial_message = excluded.initial_message,
                payment_method = excluded.payment_method,
                trade_in_vehicle = excluded.trade_in_vehicle,
                down_payment = excluded.down_payment,
                handling_agent = excluded.handling_agent,
                appointment_time = excluded.appointment_time,
                vehicle_interest = excluded.vehicle_interest,
                updated_at = excluded.updated_at
            """,
            (
                session.phone,
                session.instance or "",
                session.state,
                session.contact_name,
                session.branch,
                session.branch_id,
                session.physical_location,
                session.lead_id,
                session.initial_message,
                session.payment_method,
                session.trade_in_vehicle,
                session.down_payment,
                session.handling_agent,
                session.appointment_time,
                session.vehicle_interest,
                session.updated_at or _utc_now(),
            ),
        )
        self._conn.commit()
