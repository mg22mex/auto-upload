"""Parse Evolution API v2 webhook payloads (messages.upsert)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

WA_CHANNEL = "WhatsApp"

_JID_SKIP = ("@g.us", "@broadcast", "@newsletter", "status@broadcast")


@dataclass(frozen=True)
class WhatsAppInboundEvent:
    phone: str
    name: str
    text: str
    instance: str
    message_id: str


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
        # Some payloads nest the message under data.messages or data.key
        if "key" in data or "message" in data:
            return [data]
        messages = data.get("messages")
        if isinstance(messages, list):
            return [item for item in messages if isinstance(item, dict)]
        if isinstance(messages, dict):
            return [messages]
    return []


def parse_evolution_inbound(payload: dict[str, Any]) -> list[WhatsAppInboundEvent]:
    """Extract inbound user texts from an Evolution webhook body.

    Ignores fromMe, groups, status broadcasts, and empty bodies.
    """
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
        text = _message_text(item.get("message") if isinstance(item.get("message"), dict) else item)
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
