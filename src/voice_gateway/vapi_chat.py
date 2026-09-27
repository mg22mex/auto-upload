"""Vapi Chat API — WhatsApp text-first brain for Beatriz.

Evolution inbound text → ``POST /chat`` (assistant tools hit ``vapi-bridge``) →
final assistant text returned for Evolution ``sendText``. Financing PDF delivery
still happens inside ``/vapi/financing`` when the tool receives the customer phone.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ENV_ENABLED = "VAPI_WA_TEXT_FIRST"
ENV_ASSISTANT_ID = "VAPI_ASSISTANT_ID"
ENV_API_KEY = "VAPI_API_KEY"
ENV_TOKEN = "VAPI_TOKEN"
ENV_BASE_URL = "VAPI_BASE_URL"
ENV_CHAT_DB = "VAPI_WA_CHAT_DB_PATH"

DEFAULT_ASSISTANT_ID = "7b4bc492-b94b-40bc-a79c-754fe48c6f9b"
DEFAULT_BASE = "https://api.vapi.ai"


@dataclass
class VapiChatResult:
    reply_text: str
    chat_id: str | None = None
    previous_chat_id: str | None = None
    raw: dict[str, Any] | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.reply_text) and not self.error


def vapi_wa_text_first_enabled() -> bool:
    """Opt-in Evolution → Vapi Chat brain (``VAPI_WA_TEXT_FIRST=true``)."""
    raw = (os.getenv(ENV_ENABLED) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _api_key() -> str:
    return (os.getenv(ENV_API_KEY) or os.getenv(ENV_TOKEN) or "").strip()


def _base_url() -> str:
    return (os.getenv(ENV_BASE_URL) or DEFAULT_BASE).strip().rstrip("/")


def _assistant_id() -> str:
    return (os.getenv(ENV_ASSISTANT_ID) or DEFAULT_ASSISTANT_ID).strip()


def _http_json(
    method: str,
    url: str,
    *,
    api_key: str,
    body: dict[str, Any] | None = None,
    timeout: float = 45.0,
) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Autosell-VapiWA/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Vapi {method} {url} → HTTP {exc.code}: {detail}") from exc
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"unexpected Vapi chat payload: {type(parsed)}")
    return parsed


def extract_assistant_text(chat_payload: dict[str, Any]) -> str:
    """Pick the last non-empty assistant content from a Vapi /chat response."""
    chunks: list[str] = []
    for step in chat_payload.get("output") or []:
        if not isinstance(step, dict):
            continue
        if step.get("role") not in {"assistant", "bot"}:
            continue
        if step.get("tool_calls"):
            continue
        content = step.get("content") or step.get("message") or ""
        text = str(content).strip()
        if text:
            chunks.append(text)
    if chunks:
        return chunks[-1]
    # Fallback: some payloads put reply at top level
    for key in ("response", "text", "message"):
        val = chat_payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


class VapiChatSessionStore:
    """Persist ``previousChatId`` per WhatsApp phone + instance."""

    _DDL = """
    CREATE TABLE IF NOT EXISTS vapi_wa_chats (
        phone TEXT NOT NULL,
        instance TEXT NOT NULL DEFAULT '',
        chat_id TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (phone, instance)
    )
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        path = Path(
            db_path
            or os.getenv(ENV_CHAT_DB)
            or "data/vapi_wa_chats.db"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.execute(self._DDL)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def get_chat_id(self, phone: str, instance: str = "") -> str | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT chat_id FROM vapi_wa_chats WHERE phone=? AND instance=?",
                    (phone, instance or ""),
                ).fetchone()
            finally:
                conn.close()
        return str(row[0]) if row else None

    def set_chat_id(self, phone: str, chat_id: str, instance: str = "") -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO vapi_wa_chats (phone, instance, chat_id, updated_at)
                    VALUES (?, ?, ?, datetime('now'))
                    ON CONFLICT(phone, instance) DO UPDATE SET
                        chat_id=excluded.chat_id,
                        updated_at=datetime('now')
                    """,
                    (phone, instance or "", chat_id),
                )
                conn.commit()
            finally:
                conn.close()


_STORE: VapiChatSessionStore | None = None
_STORE_LOCK = threading.Lock()


def get_chat_store() -> VapiChatSessionStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = VapiChatSessionStore()
        return _STORE


def chat_with_beatriz(
    *,
    text: str,
    phone: str,
    customer_name: str = "",
    instance: str = "",
    branch: str | None = None,
    store: VapiChatSessionStore | None = None,
    previous_chat_id: str | None = None,
) -> VapiChatResult:
    """Send one WhatsApp turn to Vapi Chat; tools execute on server URLs."""
    key = _api_key()
    if not key:
        return VapiChatResult(reply_text="", error="missing VAPI_API_KEY")
    assistant = _assistant_id()
    if not assistant:
        return VapiChatResult(reply_text="", error="missing VAPI_ASSISTANT_ID")

    message = (text or "").strip()
    if not message:
        return VapiChatResult(reply_text="", error="empty message")

    session = store or get_chat_store()
    prev = previous_chat_id or session.get_chat_id(phone, instance)
    # Nudge tools with phone context so financing/CRM can WhatsApp + CRM-upsert.
    context_bits = [f"[whatsapp_phone={phone}]"]
    if customer_name:
        context_bits.append(f"[customer_name={customer_name}]")
    if branch:
        context_bits.append(f"[branch={branch}]")
    input_text = f"{' '.join(context_bits)}\n{message}"

    body: dict[str, Any] = {
        "assistantId": assistant,
        "input": input_text,
    }
    if prev:
        body["previousChatId"] = prev

    try:
        payload = _http_json("POST", f"{_base_url()}/chat", api_key=key, body=body)
    except Exception as exc:
        return VapiChatResult(reply_text="", error=str(exc), previous_chat_id=prev)

    chat_id = str(payload.get("id") or "").strip() or None
    if chat_id:
        try:
            session.set_chat_id(phone, chat_id, instance)
        except Exception:
            pass

    reply = extract_assistant_text(payload)
    if not reply:
        return VapiChatResult(
            reply_text="",
            chat_id=chat_id,
            previous_chat_id=prev,
            raw=payload,
            error="empty assistant reply",
        )
    return VapiChatResult(
        reply_text=reply,
        chat_id=chat_id,
        previous_chat_id=prev,
        raw=payload,
    )


__all__ = [
    "DEFAULT_ASSISTANT_ID",
    "ENV_ENABLED",
    "VapiChatResult",
    "VapiChatSessionStore",
    "chat_with_beatriz",
    "extract_assistant_text",
    "get_chat_store",
    "vapi_wa_text_first_enabled",
]
