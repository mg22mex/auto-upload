"""Vapi Chat API — WhatsApp text-first brain for Beatriz.

Evolution inbound text → ``POST /chat`` (assistant tools hit ``vapi-bridge``) →
final assistant text returned for Evolution ``sendText``.

When the user sends a down-payment amount, we **force** ``calculate_financing``
(locally via the bridge if Vapi skipped the tool) so ``financing_quote.pdf`` is
delivered immediately. Beatriz stays in-thread and asks for a branch cita —
never closes with “un asesor te contactará”.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ENV_ENABLED = "VAPI_WA_TEXT_FIRST"
ENV_ASSISTANT_ID = "VAPI_ASSISTANT_ID"
ENV_API_KEY = "VAPI_API_KEY"
ENV_TOKEN = "VAPI_TOKEN"
ENV_BASE_URL = "VAPI_BASE_URL"
ENV_CHAT_DB = "VAPI_WA_CHAT_DB_PATH"
ENV_DEFAULT_PRICE = "AI_QUOTE_DEFAULT_PRICE"

DEFAULT_ASSISTANT_ID = "7b4bc492-b94b-40bc-a79c-754fe48c6f9b"
DEFAULT_BASE = "https://api.vapi.ai"
DEFAULT_TERM_MONTHS = 48

CITA_FOLLOWUP = (
    "¿Te gustaría agendar una cita en sucursal {branch} "
    "para ver la unidad o realizar prueba de manejo?"
)

_HANDOFF_CLOSE_RE = re.compile(
    r"un asesor\s+(de nuestra sucursal\s+)?te\s+(pondrá|contactará)|"
    r"quedamos a tus órdenes|gracias por comunicarte",
    re.IGNORECASE,
)

_DOWN_PAYMENT_RE = re.compile(
    r"(?:\$\s*)?(\d{1,3}(?:[,\s]\d{3})+|\d{4,7})(?:\s*(?:pesos|mxn))?|"
    r"(\d{1,3})\s*mil",
    re.IGNORECASE,
)

_MONEY_CONTEXT_RE = re.compile(
    r"(enganche|down\s*payment|anticipo|dejo|dar[eé]|pongo|pago)",
    re.IGNORECASE,
)


@dataclass
class VapiChatResult:
    reply_text: str
    chat_id: str | None = None
    previous_chat_id: str | None = None
    raw: dict[str, Any] | None = None
    error: str | None = None
    financing_sent: bool = False
    financing_forced: bool = False
    tools_called: list[str] = field(default_factory=list)
    vehicle_name: str | None = None
    interested_vehicle: str | None = None

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
    timeout: float = 60.0,
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
    for key in ("response", "text", "message"):
        val = chat_payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def extract_tools_called(chat_payload: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for step in chat_payload.get("output") or []:
        if not isinstance(step, dict):
            continue
        for tc in step.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            name = str(fn.get("name") or tc.get("name") or "").strip()
            if name:
                names.append(name)
    return names


def parse_tool_result_blobs(chat_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Best-effort parse JSON tool results from chat output (inventory, etc.)."""
    out: list[dict[str, Any]] = []
    for step in chat_payload.get("output") or []:
        if not isinstance(step, dict):
            continue
        if step.get("role") not in {"tool", "function"}:
            continue
        content = step.get("content")
        if isinstance(content, dict):
            out.append(content)
            continue
        if not isinstance(content, str):
            continue
        text = content.strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
        elif isinstance(parsed, str):
            try:
                nested = json.loads(parsed)
            except json.JSONDecodeError:
                continue
            if isinstance(nested, dict):
                out.append(nested)
    return out


def detect_down_payment_amount(text: str) -> float | None:
    """Extract engache amount from user text (e.g. ``$200,000``, ``200 mil``)."""
    raw = (text or "").strip()
    if not raw:
        return None
    # Prefer amounts near financing keywords; else any large money figure.
    candidates: list[float] = []
    for match in _DOWN_PAYMENT_RE.finditer(raw):
        if match.group(2):
            amount = float(match.group(2)) * 1000.0
        else:
            digits = re.sub(r"[^\d]", "", match.group(1) or "")
            if not digits:
                continue
            amount = float(digits)
        if amount < 1000:
            continue
        candidates.append(amount)
    if not candidates:
        return None
    if _MONEY_CONTEXT_RE.search(raw) or "$" in raw or "enganche" in raw.casefold():
        return max(candidates)
    # Bare large number (≥ 50k) treated as engache in financing threads.
    big = [c for c in candidates if c >= 50000]
    return max(big) if big else None


def branch_label(branch: str | None) -> str:
    key = (branch or "").strip().lower()
    if key in {"san_felipe", "san felipe", "+"}:
        return "San Felipe"
    return "Periférico"


def rewrite_reply_keep_interactive(reply: str, *, branch: str | None) -> str:
    """Strip early-handoff closers; ensure cita CTA remains."""
    text = (reply or "").strip()
    label = branch_label(branch)
    cita = CITA_FOLLOWUP.format(branch=label)
    if _HANDOFF_CLOSE_RE.search(text):
        # Drop closing sentences that hand off to a human prematurely.
        parts = re.split(r"(?<=[.!?])\s+", text)
        kept = [p for p in parts if p and not _HANDOFF_CLOSE_RE.search(p)]
        text = " ".join(kept).strip()
    if "agendar una cita" not in text.casefold():
        text = f"{text} {cita}".strip() if text else cita
    return text


def _default_vehicle_price() -> float:
    raw = (os.getenv(ENV_DEFAULT_PRICE) or "450000").strip()
    try:
        return float(raw)
    except ValueError:
        return 450000.0


def _vehicle_from_tool_call_args(chat_payload: dict[str, Any]) -> str | None:
    """Pull vehicle_name from calculate_financing / inventory tool arguments."""
    for step in chat_payload.get("output") or []:
        if not isinstance(step, dict):
            continue
        for call in step.get("tool_calls") or step.get("toolCalls") or []:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") if isinstance(call.get("function"), dict) else call
            name = str(fn.get("name") or "").strip().lower()
            raw_args = fn.get("arguments")
            if isinstance(raw_args, str):
                try:
                    raw_args = json.loads(raw_args)
                except json.JSONDecodeError:
                    raw_args = {}
            if not isinstance(raw_args, dict):
                continue
            if name in {"calculate_financing", "get_financing"}:
                for key in ("vehicle_name", "interested_vehicle", "vehicle"):
                    val = str(raw_args.get(key) or "").strip()
                    if val:
                        return val
            if name in {"query_inventory", "get_inventory", "search_inventory"}:
                parts = [
                    str(raw_args.get(k) or "").strip()
                    for k in ("brand", "make", "marca", "model", "modelo")
                    if str(raw_args.get(k) or "").strip()
                ]
                year = raw_args.get("year") or raw_args.get("anio") or raw_args.get("año")
                label = " ".join(parts)
                if year not in (None, ""):
                    label = f"{label} {year}".strip()
                if label:
                    return label
    return None


def _price_from_tool_blobs(blobs: list[dict[str, Any]]) -> tuple[float | None, str | None]:
    for blob in blobs:
        vehicles = blob.get("vehicles")
        if isinstance(vehicles, list) and vehicles:
            first = vehicles[0]
            if isinstance(first, dict):
                name = str(first.get("model") or first.get("name") or "").strip() or None
                price_raw = first.get("price") or first.get("list_price")
                if isinstance(price_raw, str):
                    digits = re.sub(r"[^\d.]", "", price_raw.replace(",", ""))
                    try:
                        return float(digits), name
                    except ValueError:
                        pass
                if isinstance(price_raw, (int, float)):
                    return float(price_raw), name
        if blob.get("vehicle_price") is not None:
            try:
                return float(blob["vehicle_price"]), str(
                    blob.get("vehicle_name") or ""
                ).strip() or None
            except (TypeError, ValueError):
                pass
    return None, None


class VapiChatSessionStore:
    """Persist ``previousChatId`` + quote context per WhatsApp phone."""

    _DDL = """
    CREATE TABLE IF NOT EXISTS vapi_wa_chats (
        phone TEXT NOT NULL,
        instance TEXT NOT NULL DEFAULT '',
        chat_id TEXT NOT NULL,
        meta_json TEXT NOT NULL DEFAULT '{}',
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
        conn = self._connect()
        try:
            conn.execute(self._DDL)
            cols = {
                str(r[1])
                for r in conn.execute("PRAGMA table_info(vapi_wa_chats)").fetchall()
            }
            if "meta_json" not in cols:
                conn.execute(
                    "ALTER TABLE vapi_wa_chats ADD COLUMN meta_json TEXT NOT NULL DEFAULT '{}'"
                )
            conn.commit()
        finally:
            conn.close()

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

    def get_meta(self, phone: str, instance: str = "") -> dict[str, Any]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT meta_json FROM vapi_wa_chats WHERE phone=? AND instance=?",
                    (phone, instance or ""),
                ).fetchone()
            finally:
                conn.close()
        if not row or not row[0]:
            return {}
        try:
            data = json.loads(row[0])
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def set_chat_id(
        self,
        phone: str,
        chat_id: str,
        instance: str = "",
        *,
        meta: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            conn = self._connect()
            try:
                existing = {}
                row = conn.execute(
                    "SELECT meta_json FROM vapi_wa_chats WHERE phone=? AND instance=?",
                    (phone, instance or ""),
                ).fetchone()
                if row and row[0]:
                    try:
                        parsed = json.loads(row[0])
                        if isinstance(parsed, dict):
                            existing = parsed
                    except (TypeError, ValueError):
                        existing = {}
                merged = {**existing, **(meta or {})}
                conn.execute(
                    """
                    INSERT INTO vapi_wa_chats (phone, instance, chat_id, meta_json, updated_at)
                    VALUES (?, ?, ?, ?, datetime('now'))
                    ON CONFLICT(phone, instance) DO UPDATE SET
                        chat_id=excluded.chat_id,
                        meta_json=excluded.meta_json,
                        updated_at=datetime('now')
                    """,
                    (phone, instance or "", chat_id, json.dumps(merged, ensure_ascii=False)),
                )
                conn.commit()
            finally:
                conn.close()

    def update_meta(self, phone: str, instance: str = "", **fields: Any) -> None:
        chat_id = self.get_chat_id(phone, instance) or ""
        self.set_chat_id(phone, chat_id or "pending", instance, meta=fields)


_STORE: VapiChatSessionStore | None = None
_STORE_LOCK = threading.Lock()


def get_chat_store() -> VapiChatSessionStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = VapiChatSessionStore()
        return _STORE


def force_calculate_financing(
    *,
    phone: str,
    down_payment: float,
    customer_name: str = "",
    vehicle_name: str = "",
    vehicle_price: float | None = None,
    term_months: int = DEFAULT_TERM_MONTHS,
    branch: str | None = None,
    whatsapp_client: Any | None = None,
    manager: Any | None = None,
) -> dict[str, Any]:
    """Run bridge financing path synchronously → PDF + Beatriz Lead CRM."""
    from src.voice_gateway.vapi_bridge import handle_financing_payload

    price = float(vehicle_price) if vehicle_price else _default_vehicle_price()
    payload = {
        "vehicle_price": price,
        "term_months": int(term_months) or DEFAULT_TERM_MONTHS,
        "down_payment": float(down_payment),
        "phone": phone,
        "customer_name": customer_name or "Cliente",
        "vehicle_name": vehicle_name or "Vehículo",
        "branch": branch or "periferico",
        "send_whatsapp": True,
        # Markers so extract_whatsapp_context also sees them if nested.
        "message": {
            "toolCalls": [
                {
                    "id": "wa_forced_financing",
                    "function": {
                        "name": "calculate_financing",
                        "arguments": {
                            "vehicle_price": price,
                            "term_months": int(term_months) or DEFAULT_TERM_MONTHS,
                            "down_payment": float(down_payment),
                            "phone": phone,
                            "customer_name": customer_name or "Cliente",
                            "vehicle_name": vehicle_name or "Vehículo",
                            "branch": branch or "periferico",
                            "send_whatsapp": True,
                        },
                    },
                }
            ],
            "artifact": {
                "messages": [
                    {
                        "role": "user",
                        "message": (
                            f"[whatsapp_phone={phone}] "
                            f"[customer_name={customer_name}] "
                            f"[branch={branch or 'periferico'}] "
                            f"[vehicle_name={vehicle_name}] "
                            f"[vehicle_price={price}]"
                        ),
                    }
                ]
            },
        },
    }
    resp = handle_financing_payload(
        payload,
        whatsapp_client=whatsapp_client,
        manager=manager,
    )
    speech = resp.results[0].result if resp.results else ""
    return {"speech": speech, "results": [r.model_dump() for r in resp.results]}


def chat_with_beatriz(
    *,
    text: str,
    phone: str,
    customer_name: str = "",
    instance: str = "",
    branch: str | None = None,
    vehicle_interest: str = "",
    store: VapiChatSessionStore | None = None,
    previous_chat_id: str | None = None,
    whatsapp_client: Any | None = None,
    manager: Any | None = None,
) -> VapiChatResult:
    """Send one WhatsApp turn to Vapi Chat; force financing PDF on engache."""
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
    meta = session.get_meta(phone, instance)
    # Prefer last quoted vehicle from chat meta over stale qualification seed.
    vehicle_name = (
        str(meta.get("interested_vehicle") or meta.get("vehicle_name") or "").strip()
        or (vehicle_interest or "").strip()
        or "Vehículo"
    )
    vehicle_price = meta.get("vehicle_price")
    try:
        vehicle_price_f = float(vehicle_price) if vehicle_price not in (None, "") else None
    except (TypeError, ValueError):
        vehicle_price_f = None

    down = detect_down_payment_amount(message)
    label = branch_label(branch)

    context_bits = [
        f"[whatsapp_phone={phone}]",
        f"[customer_name={customer_name or 'Cliente'}]",
        f"[branch={branch or 'periferico'}]",
        f"[vehicle_name={vehicle_name}]",
        f"[interested_vehicle={vehicle_name}]",
    ]
    if vehicle_price_f:
        context_bits.append(f"[vehicle_price={vehicle_price_f}]")

    instructions = (
        "Eres Beatriz de Autosell en WhatsApp. "
        "Si el cliente da un enganche/cantidad, DEBES llamar calculate_financing "
        f"con phone={phone}, down_payment, vehicle_price"
        + (f"={vehicle_price_f}" if vehicle_price_f else "")
        + f", vehicle_name={vehicle_name!r}, send_whatsapp=true. "
        "Si el cliente cambia de unidad (otra marca/modelo), actualiza vehicle_name "
        "al vehículo NUEVO en calculate_financing / query_inventory — no reutilices "
        "un auto anterior. "
        "Después de la cotización/PDF, NO digas que un asesor contactará. "
        f"Pregunta exactamente: {CITA_FOLLOWUP.format(branch=label)}"
    )
    if down is not None:
        instructions += (
            f" Enganche detectado en este mensaje: {down:.0f}. "
            "Llama calculate_financing YA."
        )

    input_text = f"{' '.join(context_bits)}\n{instructions}\n\nCliente: {message}"

    body: dict[str, Any] = {
        "assistantId": assistant,
        "input": input_text,
    }
    if prev:
        body["previousChatId"] = prev

    try:
        payload = _http_json("POST", f"{_base_url()}/chat", api_key=key, body=body)
    except Exception as exc:
        # Still try local financing if engache present.
        if down is None:
            return VapiChatResult(reply_text="", error=str(exc), previous_chat_id=prev)
        payload = {"output": [], "error": str(exc)}

    chat_id = str(payload.get("id") or "").strip() or None
    tools = extract_tools_called(payload)
    blobs = parse_tool_result_blobs(payload)
    price_from_tools, name_from_tools = _price_from_tool_blobs(blobs)
    name_from_args = _vehicle_from_tool_call_args(payload)
    if price_from_tools:
        vehicle_price_f = price_from_tools
    if name_from_args:
        vehicle_name = name_from_args
    elif name_from_tools:
        vehicle_name = name_from_tools

    meta_update: dict[str, Any] = {}
    if vehicle_price_f:
        meta_update["vehicle_price"] = vehicle_price_f
    if vehicle_name and vehicle_name != "Vehículo":
        meta_update["vehicle_name"] = vehicle_name
        meta_update["interested_vehicle"] = vehicle_name
    if down is not None:
        meta_update["last_down_payment"] = down

    if chat_id:
        try:
            session.set_chat_id(phone, chat_id, instance, meta=meta_update or None)
        except Exception:
            pass
    elif meta_update:
        try:
            session.update_meta(phone, instance, **meta_update)
        except Exception:
            pass

    if vehicle_name and vehicle_name != "Vehículo":
        try:
            from src.voice_gateway.session_vehicle import remember_interested_vehicle

            remember_interested_vehicle(
                vehicle_name,
                phone=phone,
                instance=instance,
                price=vehicle_price_f,
            )
        except Exception:
            pass

    financing_forced = False
    financing_sent = "calculate_financing" in tools or "get_financing" in tools
    forced_speech = ""

    if down is not None and not financing_sent:
        try:
            forced = force_calculate_financing(
                phone=phone,
                down_payment=down,
                customer_name=customer_name,
                vehicle_name=vehicle_name,
                vehicle_price=vehicle_price_f,
                branch=branch,
                whatsapp_client=whatsapp_client,
                manager=manager,
            )
            forced_speech = str(forced.get("speech") or "")
            financing_forced = True
            financing_sent = True
            tools = [*tools, "calculate_financing"]
            try:
                from src.voice_gateway.session_vehicle import remember_interested_vehicle

                remember_interested_vehicle(
                    vehicle_name,
                    phone=phone,
                    instance=instance,
                    price=vehicle_price_f,
                )
            except Exception:
                pass
        except Exception as exc:
            print(
                f"WARN force_calculate_financing failed phone={phone}: {exc}",
                flush=True,
            )

    reply = extract_assistant_text(payload)
    if financing_forced and forced_speech:
        reply = forced_speech
    reply = rewrite_reply_keep_interactive(reply, branch=branch)

    active_vehicle = (
        vehicle_name if vehicle_name and vehicle_name != "Vehículo" else None
    )
    if not reply:
        return VapiChatResult(
            reply_text="",
            chat_id=chat_id,
            previous_chat_id=prev,
            raw=payload,
            error="empty assistant reply",
            financing_sent=financing_sent,
            financing_forced=financing_forced,
            tools_called=tools,
            vehicle_name=active_vehicle,
            interested_vehicle=active_vehicle,
        )
    return VapiChatResult(
        reply_text=reply,
        chat_id=chat_id,
        previous_chat_id=prev,
        raw=payload,
        financing_sent=financing_sent,
        financing_forced=financing_forced,
        tools_called=tools,
        vehicle_name=active_vehicle,
        interested_vehicle=active_vehicle,
    )


__all__ = [
    "CITA_FOLLOWUP",
    "DEFAULT_ASSISTANT_ID",
    "ENV_ENABLED",
    "VapiChatResult",
    "VapiChatSessionStore",
    "chat_with_beatriz",
    "detect_down_payment_amount",
    "extract_assistant_text",
    "extract_tools_called",
    "force_calculate_financing",
    "get_chat_store",
    "rewrite_reply_keep_interactive",
    "vapi_wa_text_first_enabled",
]
