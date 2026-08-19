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
STATE_HANDOFF_TO_HUMAN = "HANDOFF_TO_HUMAN"

PAYMENT_CASH = "cash"
PAYMENT_FINANCING = "financing"
PAYMENT_TRADE_IN = "trade_in"

_PAYMENT_LABELS = {
    PAYMENT_CASH: "Contado / efectivo",
    PAYMENT_FINANCING: "Financiamiento",
    PAYMENT_TRADE_IN: "Permuta (trade-in)",
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
    updated_at: str = ""


@dataclass
class QualificationTurnResult:
    session: QualificationSession
    reply_text: str
    odoo_create: bool = False
    odoo_handoff: bool = False
    odoo_notes: str = ""


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
    """Map user reply to cash / financing / trade_in."""
    t = (text or "").strip().lower()
    if not t:
        return None
    if t in {"1", "contado", "efectivo", "cash", "de contado", "al contado"}:
        return PAYMENT_CASH
    if t in {
        "2",
        "financiamiento",
        "financiar",
        "credito",
        "crédito",
        "mensualidades",
        "financiado",
    }:
        return PAYMENT_FINANCING
    if t in {
        "3",
        "permuta",
        "trade",
        "trade-in",
        "trade in",
        "cambio",
        "entregar mi auto",
        "entregar auto",
    }:
        return PAYMENT_TRADE_IN
    if "contado" in t or "efectivo" in t:
        return PAYMENT_CASH
    if "financ" in t or "crédit" in t or "credit" in t or "mensual" in t:
        return PAYMENT_FINANCING
    if "permuta" in t or "trade" in t or "cambio" in t or "entregar" in t:
        return PAYMENT_TRADE_IN
    return None


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
        "3️⃣ Permuta (trade-in)\n\n"
        "Responde con el número o escribe contado, financiamiento o permuta."
    )


def _payment_retry_message() -> str:
    return (
        "No entendí tu respuesta. Por favor elige una opción:\n"
        "1 Contado · 2 Financiamiento · 3 Permuta"
    )


def _trade_in_prompt() -> str:
    return (
        "Perfecto. ¿Qué vehículo entregarías en permuta?\n"
        "Indica año, marca y modelo (ej. 2018 Nissan Sentra)."
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
        lines.append(f"Vehículo permuta: {session.trade_in_vehicle}")
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
        lines.append(f"• Permuta: {session.trade_in_vehicle}")
    if session.down_payment:
        lines.append(f"• Enganche: {session.down_payment}")
    return "\n".join(lines)


def _post_handoff_message(branch_label: str) -> str:
    return (
        f"Tu solicitud ya está con un asesor de Autosell {branch_label}. "
        "Te contactaremos pronto. 🙌"
    )


def process_qualification_turn(
    event: WhatsAppInboundEvent,
    session: QualificationSession | None,
    *,
    branch: str = "periferico",
    branch_id: int | None = None,
    physical_location: str = "Periférico",
) -> QualificationTurnResult:
    """Advance one inbound message through the qualification state machine."""
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
            updated_at=now,
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
        if choice == PAYMENT_TRADE_IN:
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

    session.state = STATE_HANDOFF
    session.updated_at = now
    return QualificationTurnResult(
        session=session,
        reply_text=_post_handoff_message(session.physical_location),
    )


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
        updated_at TEXT NOT NULL,
        PRIMARY KEY (phone, instance)
    );
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            db_path = os.getenv("WA_QUALIFICATION_DB_PATH") or "data/wa_qualification.db"
        self.db_path = Path(db_path)
        if str(db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(self._SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def get(self, phone: str, instance: str) -> QualificationSession | None:
        row = self._conn.execute(
            """
            SELECT phone, instance, state, contact_name, branch, branch_id,
                   physical_location, lead_id, initial_message, payment_method,
                   trade_in_vehicle, down_payment, updated_at
            FROM wa_qualification
            WHERE phone = ? AND instance = ?
            """,
            (phone, instance or ""),
        ).fetchone()
        if row is None:
            return None
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
            updated_at=str(row["updated_at"] or ""),
        )

    def save(self, session: QualificationSession) -> None:
        self._conn.execute(
            """
            INSERT INTO wa_qualification (
                phone, instance, state, contact_name, branch, branch_id,
                physical_location, lead_id, initial_message, payment_method,
                trade_in_vehicle, down_payment, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                session.updated_at or _utc_now(),
            ),
        )
        self._conn.commit()
