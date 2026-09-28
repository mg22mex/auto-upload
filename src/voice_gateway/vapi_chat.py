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

from src.voice_gateway.prompts import WA_QUALIFY_INVENTORY_SNIPPET

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
    r"(\d{1,3})\s*mil|"
    r"(\d{1,3})\s*k\b",
    re.IGNORECASE,
)

_MONEY_CONTEXT_RE = re.compile(
    r"(enganche|down\s*payment|anticipo|dejo|dar[eé]|pongo|pago)",
    re.IGNORECASE,
)

# "50 mil km" / "50,000 kilómetros" is mileage, not engache.
_MILEAGE_AMOUNT_RE = re.compile(
    r"(\d{1,3}(?:[,\s]\d{3})+|\d{4,7}|\d{1,3}\s*mil|\d{1,3}\s*k)\s*"
    r"(?:km|kms|kil[oó]metros?)\b",
    re.IGNORECASE,
)

_SESSION_RESET_RE = re.compile(
    r"\b("
    r"reiniciar|reset|restart|"
    r"empezar\s+de\s+nuevo|"
    r"nueva\s+conversaci[oó]n|"
    r"borrar\s+(?:sesi[oó]n|contexto)|"
    r"limpiar\s+(?:sesi[oó]n|contexto)|"
    r"olvidar\s+(?:el\s+)?contexto"
    r")\b",
    re.IGNORECASE,
)

_TERM_MONTHS_RE = re.compile(
    r"(?:a|plazo|financiar|financiamiento)?\s*(?:de\s*)?(\d{1,2})\s*meses",
    re.IGNORECASE,
)

SESSION_RESET_REPLY = (
    "Listo, reinicié la conversación. "
    "¿En qué te puedo ayudar? Puedo buscar inventario, cotizar financiamiento "
    "o valuar tu auto a cuenta."
)

TRADEIN_APPLY_CTA = (
    "Entendido. ¿Deseas aplicar esta valuación como enganche "
    "para cotizar alguna unidad de nuestro catálogo?"
)

# Short / unparseable replies after a trade-in quote (avoid Vapi inventing amounts).
_BRIEF_TRADEIN_FOLLOWUP_RE = re.compile(
    r"^(?:ee+|e+|ok|oke?y?|vale|va+|sip?|s[ií]+|no+|nop|gracias|"
    r"umm+|eh+|aja|ajá|mmm+|bien|perfecto)\W*$",
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
    tradein_sent: bool = False
    tradein_forced: bool = False
    tools_called: list[str] = field(default_factory=list)
    vehicle_name: str | None = None
    interested_vehicle: str | None = None
    tradein_summary: str | None = None

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
    """Extract engache amount from user text (e.g. ``$200,000``, ``200 mil``).

    Mileage figures (``50 mil km``) are ignored so trade-in appraisals do not
    trigger ``calculate_financing``.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    # Appraisal / toma a cuenta never carries an engache in the same phrase.
    if detect_tradein_intent(raw):
        return None
    # Mask mileage spans so "50 mil km" is not parsed as $50,000 engache.
    masked = _MILEAGE_AMOUNT_RE.sub(" ", raw)
    candidates: list[float] = []
    for match in _DOWN_PAYMENT_RE.finditer(masked):
        if match.group(2):
            amount = float(match.group(2)) * 1000.0
        elif match.group(3):
            amount = float(match.group(3)) * 1000.0
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


def detect_session_reset(text: str) -> bool:
    """True when the user asks to wipe conversation / vehicle context."""
    return bool(_SESSION_RESET_RE.search(text or ""))


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


def detect_term_months(text: str) -> int | None:
    """Extract plazo in months from user text (e.g. ``60 meses``)."""
    raw = (text or "").strip()
    if not raw:
        return None
    match = _TERM_MONTHS_RE.search(raw)
    if not match:
        return None
    try:
        months = int(match.group(1))
    except (TypeError, ValueError):
        return None
    if months % 12 != 0:
        months = max(12, round(months / 12) * 12)
    if months < 12 or months > 72:
        return None
    return months


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
                        year = raw_args.get("vehicle_year") or raw_args.get("year")
                        if year not in (None, "") and str(year) not in val:
                            val = f"{val} {year}".strip()
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


def _match_from_tool_blobs(
    blobs: list[dict[str, Any]],
) -> tuple[float | None, str | None, int | None]:
    """Return (price, clean_name, year) from inventory / financing tool results."""
    from src.quote_engine.term_limits import extract_model_year

    for blob in blobs:
        vehicles = blob.get("vehicles")
        if isinstance(vehicles, list) and vehicles:
            first = vehicles[0]
            if isinstance(first, dict):
                name = str(
                    first.get("name") or first.get("model") or ""
                ).strip() or None
                year = extract_model_year(first.get("year")) or extract_model_year(name)
                price_raw = first.get("price") or first.get("list_price")
                price: float | None = None
                if isinstance(price_raw, str):
                    digits = re.sub(r"[^\d.]", "", price_raw.replace(",", ""))
                    try:
                        price = float(digits)
                    except ValueError:
                        price = None
                elif isinstance(price_raw, (int, float)):
                    price = float(price_raw)
                if price is not None or name:
                    return price, name, year
        if blob.get("vehicle_price") is not None:
            try:
                name = str(blob.get("vehicle_name") or "").strip() or None
                year = extract_model_year(blob.get("vehicle_year")) or extract_model_year(
                    name
                )
                return float(blob["vehicle_price"]), name, year
            except (TypeError, ValueError):
                pass
    return None, None, None


def _price_from_tool_blobs(blobs: list[dict[str, Any]]) -> tuple[float | None, str | None]:
    price, name, _year = _match_from_tool_blobs(blobs)
    return price, name


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

    def clear_phone(self, phone: str, instance: str | None = None) -> int:
        """Drop Vapi previousChatId + meta for *phone* (all instances if None)."""
        digits = re.sub(r"\D", "", phone or "")
        if not digits and not (phone or "").strip():
            return 0
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT phone, instance FROM vapi_wa_chats"
                ).fetchall()
                deleted = 0
                for row_phone, row_inst in rows:
                    row_digits = re.sub(r"\D", "", str(row_phone or ""))
                    match_phone = str(row_phone or "") == phone or (
                        digits
                        and (
                            row_digits == digits
                            or (
                                len(digits) >= 10
                                and row_digits.endswith(digits[-10:])
                            )
                        )
                    )
                    if not match_phone:
                        continue
                    if instance is not None and str(row_inst or "") != instance:
                        continue
                    conn.execute(
                        "DELETE FROM vapi_wa_chats WHERE phone=? AND instance=?",
                        (row_phone, row_inst or ""),
                    )
                    deleted += 1
                conn.commit()
            finally:
                conn.close()
        return deleted


_STORE: VapiChatSessionStore | None = None
_STORE_LOCK = threading.Lock()


def get_chat_store() -> VapiChatSessionStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = VapiChatSessionStore()
        return _STORE


def reset_chat_store_for_tests() -> None:
    """Drop the process-wide store singleton (unit tests only)."""
    global _STORE
    with _STORE_LOCK:
        _STORE = None


def detect_tradein_intent(text: str) -> bool:
    """True when the user asks for trade-in / appraisal / toma a cuenta."""
    from src.lead_routing import parse_payment_intent

    return bool(parse_payment_intent(text).trade_in)


def extract_tradein_version(text: str) -> str:
    """Pull trim/version from clarifying replies (e.g. ``es versión LE``)."""
    from src.lead_routing import extract_trade_in_version

    return extract_trade_in_version(text)


def is_brief_tradein_followup(text: str) -> bool:
    """True for tiny / ack-only messages that must not invent Autométrica amounts."""
    raw = (text or "").strip()
    if not raw:
        return True
    if len(raw) <= 2:
        return True
    return bool(_BRIEF_TRADEIN_FOLLOWUP_RE.match(raw))


def _prior_tradein_from_meta(meta: dict[str, Any]) -> Any | None:
    """Rebuild TradeInDetails from chat meta after a prior Autométrica quote."""
    make = str(meta.get("tradein_make") or "").strip()
    model = str(meta.get("tradein_model") or "").strip()
    year_raw = meta.get("tradein_year")
    if not (make and model and year_raw not in (None, "")):
        summary = str(meta.get("tradein_summary") or "")
        if not summary and not meta.get("valor_compra"):
            return None
        from src.lead_routing import parse_trade_in_details

        recovered = parse_trade_in_details(summary)
        if recovered.year and recovered.make and recovered.model:
            return recovered
        return None
    from src.lead_routing import TradeInDetails

    try:
        year = int(year_raw)
    except (TypeError, ValueError):
        return None
    km_raw = meta.get("tradein_mileage_km")
    try:
        km = int(km_raw) if km_raw not in (None, "") else None
    except (TypeError, ValueError):
        km = None
    return TradeInDetails(
        year=year,
        make=make,
        model=model,
        version=str(meta.get("tradein_version") or meta.get("tradein_trim") or "").strip(),
        mileage_km=km,
        is_trade_in=True,
    )


def _tradein_meta_fields(details: dict[str, Any], speech: str) -> dict[str, Any]:
    """Persist Autométrica trade-in only — never overwrite inventory interest."""
    out: dict[str, Any] = {"tradein_summary": speech}
    if details.get("make"):
        out["tradein_make"] = details["make"]
    if details.get("model"):
        out["tradein_model"] = details["model"]
    if details.get("year") not in (None, ""):
        out["tradein_year"] = details["year"]
    if details.get("version"):
        out["tradein_version"] = details["version"]
        out["tradein_trim"] = details["version"]
    if details.get("mileage_km") not in (None, ""):
        out["tradein_mileage_km"] = details["mileage_km"]
    amount_m = re.search(r"~\$([0-9,]+)", speech)
    if amount_m:
        try:
            amount = float(amount_m.group(1).replace(",", ""))
            out["valor_compra"] = amount
            out["net_trade_in_equity"] = amount
        except ValueError:
            pass
    label = (
        f"{details.get('make', '')} {details.get('model', '')} {details.get('year', '')}"
    ).strip()
    if label:
        # trade_in_label only — interested_vehicle stays the inventory unit.
        out["trade_in_label"] = label
    return out


def clear_wa_session_context(
    phone: str,
    *,
    instance: str | None = None,
) -> dict[str, Any]:
    """Wipe qualification + Vapi chat memory for clean test / user reset."""
    cleared_q: list[dict[str, Any]] = []
    try:
        from src.whatsapp_worker.inbound import QualificationStore

        cleared_q = QualificationStore().clear_conversation(
            phone, instance=instance
        )
    except Exception as exc:
        cleared_q = [{"error": str(exc)}]
    chat_deleted = 0
    try:
        chat_deleted = get_chat_store().clear_phone(phone, instance)
    except Exception:
        chat_deleted = 0
    return {
        "phone": phone,
        "qualification": cleared_q,
        "vapi_chats_deleted": chat_deleted,
    }


def force_get_tradein_valuation(
    *,
    text: str,
    phone: str = "",
    prior: Any | None = None,
    version: str | None = None,
    whatsapp_client: Any | None = None,
) -> dict[str, Any]:
    """Parse trade-in vehicle from chat and run Autométrica via bridge."""
    del whatsapp_client  # reserved
    from src.lead_routing import apply_baseline_trim, parse_trade_in_details
    from src.voice_gateway.vapi_bridge import handle_tradein_payload

    details = parse_trade_in_details(text, prior=prior)
    if (version or "").strip():
        details.version = version.strip()
    details = apply_baseline_trim(details)
    details.is_trade_in = True
    if details.year is None or not details.make or not details.model:
        return {
            "speech": (
                "Para valuar tu auto a cuenta con Autométrica necesito "
                "marca, modelo, año y kilometraje. "
                "Ejemplo: Toyota Corolla 2020 con 50 mil km."
            ),
            "ok": False,
            "details": details.__dict__,
        }
    payload = {
        "brand": details.make,
        "model": details.model,
        "year": int(details.year),
        "mileage": int(details.mileage_km or 0),
        "version": details.version or "",
        "phone": phone or None,
        "message": {
            "toolCalls": [
                {
                    "id": "wa_forced_tradein",
                    "function": {
                        "name": "get_tradein_valuation",
                        "arguments": {
                            "brand": details.make,
                            "model": details.model,
                            "year": int(details.year),
                            "mileage": int(details.mileage_km or 0),
                            "version": details.version or "",
                            "phone": phone or "",
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
                            f"[vehicle_name={details.make} {details.model} {details.year}]"
                        ),
                    }
                ]
            },
        },
    }
    resp = handle_tradein_payload(payload)
    speech = resp.results[0].result if resp.results else ""
    return {
        "speech": speech,
        "ok": bool(speech),
        "tool": "get_tradein_valuation",
        "results": [r.model_dump() for r in resp.results],
        "details": {
            "make": details.make,
            "model": details.model,
            "year": details.year,
            "version": details.version,
            "mileage_km": details.mileage_km,
            "trim": details.version,
        },
    }


def _finish_forced_tradein(
    *,
    forced_ti: dict[str, Any],
    phone: str,
    instance: str,
    branch: str | None,
    session: VapiChatSessionStore,
    previous_chat_id: str | None,
) -> VapiChatResult | None:
    speech = str(forced_ti.get("speech") or "").strip()
    if not speech:
        return None
    if forced_ti.get("ok") is False:
        reply = rewrite_reply_keep_interactive(speech, branch=branch)
        return VapiChatResult(
            reply_text=reply,
            tradein_sent=False,
            tradein_forced=True,
            tools_called=["get_tradein_valuation"],
            tradein_summary=speech,
        )
    details = forced_ti.get("details") if isinstance(forced_ti, dict) else {}
    if not isinstance(details, dict):
        details = {}
    meta_update = _tradein_meta_fields(details, speech)
    try:
        session.update_meta(phone, instance, **meta_update)
    except Exception:
        pass
    # Keep inventory interest sticky; expose trade-in separately.
    try:
        existing = session.get_meta(phone, instance)
        inventory = str(
            existing.get("interested_vehicle") or existing.get("vehicle_name") or ""
        ).strip()
    except Exception:
        inventory = ""
    reply = rewrite_reply_keep_interactive(speech, branch=branch)
    return VapiChatResult(
        reply_text=reply,
        previous_chat_id=previous_chat_id or session.get_chat_id(phone, instance),
        tradein_sent=True,
        tradein_forced=True,
        tools_called=["get_tradein_valuation"],
        vehicle_name=inventory or None,
        interested_vehicle=inventory or None,
        tradein_summary=speech,
    )



def force_book_appointment(
    *,
    text: str,
    phone: str,
    customer_name: str = "",
    branch: str | None = None,
    instance: str = "",
    meta: dict[str, Any] | None = None,
    prior_tradein: Any | None = None,
    vehicle_interest: str = "",
    whatsapp_client: Any | None = None,
    manager: Any | None = None,
    channel_branch: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Confirm cita without calculate_financing (trade-in / inspection visits OK).

    Cross-branch catalog hits first return a soft ask (``pending_confirmation``)
    without CRM writes until ``confirm=True`` (sí / confirma / …).
    """
    from src.lead_routing import (
        detect_appointment_confirmation,
        detect_appointment_intent,
    )
    from src.voice_gateway.vapi_bridge import LeadArgs, create_vapi_lead, format_lead_speech

    del whatsapp_client  # reserved; inbound / CRM path alerts the rep
    appt = detect_appointment_intent(text)
    if not appt.requested and confirm:
        appt = detect_appointment_confirmation(text)
    when = (appt.when_text or "").strip()
    meta = meta or {}
    if not when:
        when = str(
            meta.get("pending_appointment_when")
            or meta.get("last_appointment")
            or ""
        ).strip()
    if not when:
        when = "el horario que prefieras"
    explicit_tradein = detect_tradein_intent(text) or bool(
        re.search(
            r"valuaci[oó]n\s+f[ií]sica|inspecci[oó]n\s+f[ií]sica|"
            r"toma\s+a\s+cuenta|a\s+cambio|permuta",
            text or "",
            re.IGNORECASE,
        )
    )
    trade_label = ""
    if explicit_tradein and prior_tradein is not None:
        bits = [
            str(getattr(prior_tradein, "make", "") or "").strip(),
            str(getattr(prior_tradein, "model", "") or "").strip(),
            str(getattr(prior_tradein, "year", "") or "").strip(),
            str(getattr(prior_tradein, "version", "") or "").strip(),
        ]
        trade_label = " ".join(b for b in bits if b).strip()
    if explicit_tradein and not trade_label:
        trade_label = str(meta.get("trade_in_label") or "").strip()
    inventory = str(
        meta.get("interested_vehicle")
        or meta.get("vehicle_name")
        or meta.get("pending_appointment_vehicle")
        or vehicle_interest
        or ""
    ).strip()
    # If sticky meta still equals trade-in (legacy), treat as trade-in-only.
    if (
        explicit_tradein
        and inventory
        and trade_label
        and inventory.casefold() == trade_label.casefold()
    ):
        inventory = ""
    amount_f = None
    if explicit_tradein:
        amount = meta.get("valor_compra") or meta.get("net_trade_in_equity")
        try:
            amount_f = float(amount) if amount not in (None, "") else None
        except (TypeError, ValueError):
            amount_f = None
        if amount_f is None:
            summary = str(meta.get("tradein_summary") or "")
            m = re.search(r"~\$([0-9,]+)", summary)
            if m:
                try:
                    amount_f = float(m.group(1).replace(",", ""))
                except ValueError:
                    amount_f = None

    if explicit_tradein and trade_label and amount_f is not None:
        tradein_note = (
            f"{trade_label} · Autométrica ~${amount_f:,.0f} "
            f"(valuación física / prueba de manejo)"
        )
    elif explicit_tradein and trade_label:
        tradein_note = f"{trade_label} (valuación física / prueba de manejo)"
    elif explicit_tradein and amount_f is not None:
        tradein_note = f"Auto a cambio · Autométrica ~${amount_f:,.0f}"
    elif explicit_tradein:
        tradein_note = str(meta.get("tradein_summary") or "").strip() or None
    else:
        # Catalog viewing / purchase cita — never reattach sticky Autométrica.
        tradein_note = None

    if inventory:
        vehicle_for_crm = inventory
    elif trade_label:
        vehicle_for_crm = trade_label
    else:
        vehicle_for_crm = "Consulta general"

    from src.config import branch_label as cfg_branch_label
    from src.odoo_sync.crm import normalize_crm_branch
    from src.voice_gateway.vapi_bridge import branch_from_vehicle_title

    channel_key = normalize_crm_branch(channel_branch or branch)
    vehicle_branch = str(
        meta.get("pending_appointment_branch")
        or meta.get("vehicle_branch")
        or meta.get("physical_location")
        or ""
    ).strip()
    if vehicle_branch:
        vehicle_branch = normalize_crm_branch(vehicle_branch)
    else:
        lot_branch, _ = branch_from_vehicle_title(vehicle_for_crm)
        vehicle_branch = lot_branch or None

    # Prefer the vehicle's physical lot over the WhatsApp channel branch.
    if vehicle_branch:
        branch_key = vehicle_branch
    else:
        branch_key = channel_key
    cross_branch = bool(
        vehicle_branch
        and channel_key
        and vehicle_branch != channel_key
    )

    name = (customer_name or "Cliente").strip() or "Cliente"
    dest = cfg_branch_label(branch_key)

    # Soft ask — stash pending context; do not CRM / RR yet.
    if cross_branch and not confirm:
        speech = (
            f"El {vehicle_for_crm} está físicamente en nuestra sucursal {dest}. "
            f"¿Te confirmo esa visita en {dest} {when}?"
        )
        return {
            "ok": True,
            "speech": speech,
            "tool": "pending_appointment_confirmation",
            "when": when,
            "vehicle": vehicle_for_crm,
            "tradein_note": None,
            "crm": {},
            "branch": branch_key,
            "channel_branch": channel_key,
            "cross_branch": True,
            "pending_confirmation": True,
            "vehicle_price": meta.get("vehicle_price"),
        }

    args = LeadArgs(
        name=name,
        phone=phone,
        interested_vehicle=vehicle_for_crm,
        tradein_summary=tradein_note,
        appointment_date=when,
        branch=branch_key,
    )
    crm: dict[str, Any] = {}
    try:
        crm = create_vapi_lead(args, manager=manager)
    except Exception as exc:
        print(f"WARN force_book_appointment CRM failed phone={phone}: {exc}", flush=True)
        crm = {"error": str(exc)}

    if confirm or cross_branch:
        speech = (
            f"¡Cita confirmada, {name}! Te esperamos {when} en nuestra "
            f"sucursal {dest} para ver el {vehicle_for_crm}."
        )
    else:
        speech = format_lead_speech(
            args, dry_run=bool(crm.get("dry_run")), status=str(crm.get("status") or "created")
        )
        # Keep confirmation short — no financing prompts.
        if "financi" in speech.casefold() or "enganche" in speech.casefold():
            speech = (
                f"¡Perfecto, {name}! Agendamos tu cita en Autosell "
                f"{branch_label(branch_key)} ({when}). Te esperamos."
            )
    return {
        "ok": True,
        "speech": speech,
        "tool": "book_appointment",
        "when": when,
        "vehicle": vehicle_for_crm,
        "tradein_note": tradein_note,
        "crm": crm,
        "branch": branch_key,
        "channel_branch": channel_key,
        "cross_branch": cross_branch,
        "pending_confirmation": False,
        "vehicle_price": meta.get("vehicle_price"),
    }


def force_calculate_financing(
    *,
    phone: str,
    down_payment: float,
    customer_name: str = "",
    vehicle_name: str = "",
    vehicle_price: float | None = None,
    vehicle_year: int | None = None,
    term_months: int = DEFAULT_TERM_MONTHS,
    branch: str | None = None,
    whatsapp_client: Any | None = None,
    manager: Any | None = None,
) -> dict[str, Any]:
    """Run bridge financing path synchronously → PDF + Beatriz Lead CRM."""
    from src.pdf_engine.generator import sanitize_vehicle_title
    from src.quote_engine.term_limits import extract_model_year
    from src.voice_gateway.vapi_bridge import handle_financing_payload

    price = float(vehicle_price) if vehicle_price else _default_vehicle_price()
    clean_name = sanitize_vehicle_title(vehicle_name or "Vehículo")
    year = vehicle_year if vehicle_year is not None else extract_model_year(clean_name)
    term = int(term_months) or DEFAULT_TERM_MONTHS
    args_body: dict[str, Any] = {
        "vehicle_price": price,
        "term_months": term,
        "down_payment": float(down_payment),
        "phone": phone,
        "customer_name": customer_name or "Cliente",
        "vehicle_name": clean_name,
        "branch": branch or "periferico",
        "send_whatsapp": True,
    }
    if year is not None:
        args_body["vehicle_year"] = int(year)
        args_body["year"] = int(year)
    markers = (
        f"[whatsapp_phone={phone}] "
        f"[customer_name={customer_name}] "
        f"[branch={branch or 'periferico'}] "
        f"[vehicle_name={clean_name}] "
        f"[vehicle_price={price}]"
    )
    if year is not None:
        markers += f" [vehicle_year={int(year)}]"
    payload = {
        "vehicle_price": price,
        "term_months": term,
        "down_payment": float(down_payment),
        "phone": phone,
        "customer_name": customer_name or "Cliente",
        "vehicle_name": clean_name,
        "vehicle_year": year,
        "branch": branch or "periferico",
        "send_whatsapp": True,
        "message": {
            "toolCalls": [
                {
                    "id": "wa_forced_financing",
                    "function": {
                        "name": "calculate_financing",
                        "arguments": args_body,
                    },
                }
            ],
            "artifact": {
                "messages": [
                    {
                        "role": "user",
                        "message": markers,
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
    message = (text or "").strip()
    if not message:
        return VapiChatResult(reply_text="", error="empty message")

    session = store or get_chat_store()
    label = branch_label(branch)

    # Session reset — wipe Vapi thread + qualification sticky vehicle/trade-in.
    if detect_session_reset(message):
        try:
            clear_wa_session_context(phone, instance=instance or None)
        except Exception:
            try:
                session.clear_phone(phone, instance or None)
            except Exception:
                pass
        return VapiChatResult(
            reply_text=SESSION_RESET_REPLY,
            tools_called=["session_reset"],
        )

    meta_early = session.get_meta(phone, instance)
    prior_tradein = _prior_tradein_from_meta(meta_early)
    version_followup = extract_tradein_version(message)

    # Explicit purchase interest ("quiero un Aveo 2020" / "para ver un Corolla")
    # → lock interested_vehicle from live catalog BEFORE trade-in handling.
    try:
        from src.inventory.catalog_match import match_desired_in_catalog, match_to_meta
        from src.lead_routing import extract_desired_vehicle
        from src.voice_gateway.session_vehicle import remember_interested_vehicle

        desired = extract_desired_vehicle(message)
        if desired:
            catalog_hit = match_desired_in_catalog(desired)
            bind_label = catalog_hit.display_name if catalog_hit else desired
            remember_interested_vehicle(
                bind_label,
                phone=phone,
                instance=instance,
                price=catalog_hit.price if catalog_hit else None,
                vehicle_year=(
                    int(catalog_hit.vehicle.year)
                    if catalog_hit and str(catalog_hit.vehicle.year).isdigit()
                    else None
                ),
                qualification_store=None,
                chat_store=session,
                clear_tradein=True,
            )
            meta_patch: dict[str, Any] = {
                "interested_vehicle": bind_label,
                "vehicle_name": bind_label,
                # Wipe sticky Autométrica so catalog viewing cannot reattach it.
                "tradein_summary": "",
                "trade_in_label": "",
                "valor_compra": "",
                "net_trade_in_equity": "",
                "tradein_make": "",
                "tradein_model": "",
                "tradein_year": "",
                "tradein_mileage_km": "",
            }
            if catalog_hit:
                meta_patch.update(match_to_meta(catalog_hit))
            try:
                session.update_meta(phone, instance, **meta_patch)
            except Exception:
                pass
            meta_early = {**meta_early, **meta_patch}
            vehicle_interest = bind_label
            prior_tradein = None
    except Exception as exc:
        print(
            f"WARN desired-vehicle bind failed phone={phone}: {exc}",
            flush=True,
        )

    # Clarifying trim after a prior Autométrica quote → re-run guide lookup.
    if prior_tradein is not None and version_followup and not detect_tradein_intent(message):
        try:
            forced_ti = force_get_tradein_valuation(
                text=message,
                phone=phone,
                prior=prior_tradein,
                version=version_followup,
            )
            finished = _finish_forced_tradein(
                forced_ti=forced_ti,
                phone=phone,
                instance=instance,
                branch=branch,
                session=session,
                previous_chat_id=previous_chat_id,
            )
            if finished is not None:
                return finished
        except Exception as exc:
            print(
                f"WARN tradein version follow-up failed phone={phone}: {exc}",
                flush=True,
            )

    # Brief/unparseable follow-up after valuation — never invent guide amounts.
    if (
        prior_tradein is not None
        and (meta_early.get("tradein_summary") or meta_early.get("valor_compra"))
        and is_brief_tradein_followup(message)
        and not detect_tradein_intent(message)
        and not detect_session_reset(message)
    ):
        return VapiChatResult(
            reply_text=TRADEIN_APPLY_CTA,
            previous_chat_id=previous_chat_id or session.get_chat_id(phone, instance),
            tradein_sent=True,
            tradein_summary=str(meta_early.get("tradein_summary") or "") or None,
            tools_called=["tradein_apply_cta"],
            vehicle_name=str(meta_early.get("interested_vehicle") or "") or None,
            interested_vehicle=str(meta_early.get("interested_vehicle") or "") or None,
        )

    # Trade-in / valuation takes strict precedence over sticky financing context.
    # Short-circuit BEFORE Vapi/API key so previousChatId (e.g. Mustang) cannot
    # fire calculate_financing / PDF side-effects.
    if detect_tradein_intent(message):
        try:
            forced_ti = force_get_tradein_valuation(
                text=message,
                phone=phone,
                prior=prior_tradein,
            )
            finished = _finish_forced_tradein(
                forced_ti=forced_ti,
                phone=phone,
                instance=instance,
                branch=branch,
                session=session,
                previous_chat_id=previous_chat_id,
            )
            if finished is not None:
                return finished
        except Exception as exc:
            print(
                f"WARN force_get_tradein_valuation (early) failed phone={phone}: {exc}",
                flush=True,
            )

    # Appointment / valuación física — never force calculate_financing.
    from src.lead_routing import (
        detect_appointment_confirmation,
        detect_appointment_intent,
    )

    pending_confirm = str(
        meta_early.get("pending_appointment_confirmation") or ""
    ).strip() in {"1", "true", "yes", "pending"}
    confirm_intent = detect_appointment_confirmation(message) if pending_confirm else None
    appointment = detect_appointment_intent(message)
    if pending_confirm and confirm_intent and confirm_intent.requested:
        appointment = confirm_intent

    if (
        (appointment.requested or (pending_confirm and confirm_intent and confirm_intent.requested))
        and not detect_tradein_intent(message)
    ):
        try:
            # Re-read meta so early Aveo bind is visible to CRM booking.
            meta_book = session.get_meta(phone, instance) or meta_early
            is_confirm = bool(
                pending_confirm
                and confirm_intent
                and confirm_intent.requested
            )
            booked = force_book_appointment(
                text=message,
                phone=phone,
                customer_name=customer_name,
                branch=branch,
                channel_branch=branch,
                instance=instance,
                meta=meta_book,
                prior_tradein=None if meta_book.get("interested_vehicle") else prior_tradein,
                vehicle_interest=vehicle_interest
                or str(meta_book.get("interested_vehicle") or "")
                or str(meta_book.get("pending_appointment_vehicle") or ""),
                manager=manager,
                confirm=is_confirm,
            )
            speech = str(booked.get("speech") or "").strip()
            if speech:
                try:
                    if booked.get("pending_confirmation"):
                        session.update_meta(
                            phone,
                            instance,
                            pending_appointment_confirmation="1",
                            pending_appointment_when=booked.get("when") or "",
                            pending_appointment_vehicle=booked.get("vehicle") or "",
                            pending_appointment_branch=booked.get("branch") or "",
                            vehicle_branch=booked.get("branch") or "",
                            interested_vehicle=booked.get("vehicle") or "",
                            vehicle_name=booked.get("vehicle") or "",
                            last_appointment=booked.get("when") or "",
                        )
                    else:
                        session.update_meta(
                            phone,
                            instance,
                            pending_appointment_confirmation="",
                            pending_appointment_when="",
                            pending_appointment_vehicle="",
                            pending_appointment_branch="",
                            last_appointment=booked.get("when"),
                            appointment_vehicle=booked.get("vehicle"),
                            tradein_cita_note=booked.get("tradein_note") or "",
                            vehicle_branch=booked.get("branch"),
                            interested_vehicle=booked.get("vehicle") or "",
                        )
                except Exception:
                    pass
                tool_name = str(booked.get("tool") or "book_appointment")
                return VapiChatResult(
                    reply_text=speech,
                    previous_chat_id=previous_chat_id
                    or session.get_chat_id(phone, instance),
                    tools_called=[tool_name],
                    vehicle_name=str(booked.get("vehicle") or "") or None,
                    interested_vehicle=str(booked.get("vehicle") or "") or None,
                    tradein_summary=None,
                )
        except Exception as exc:
            print(
                f"WARN force_book_appointment failed phone={phone}: {exc}",
                flush=True,
            )

    key = _api_key()
    if not key:
        return VapiChatResult(reply_text="", error="missing VAPI_API_KEY")
    assistant = _assistant_id()
    if not assistant:
        return VapiChatResult(reply_text="", error="missing VAPI_ASSISTANT_ID")

    prev = previous_chat_id or session.get_chat_id(phone, instance)
    meta = session.get_meta(phone, instance)
    from src.pdf_engine.generator import sanitize_vehicle_title
    from src.quote_engine.term_limits import extract_model_year

    # Prefer last quoted vehicle from chat meta over stale qualification seed.
    vehicle_name = sanitize_vehicle_title(
        str(meta.get("interested_vehicle") or meta.get("vehicle_name") or "").strip()
        or (vehicle_interest or "").strip()
        or "Vehículo"
    )
    vehicle_price = meta.get("vehicle_price")
    try:
        vehicle_price_f = float(vehicle_price) if vehicle_price not in (None, "") else None
    except (TypeError, ValueError):
        vehicle_price_f = None
    vehicle_year = extract_model_year(meta.get("vehicle_year")) or extract_model_year(
        vehicle_name
    )

    down = detect_down_payment_amount(message)
    # Appointment-only turns must never treat leftover engache / invent financing.
    if appointment.requested:
        down = None
    term_from_msg = detect_term_months(message)

    context_bits = [
        f"[whatsapp_phone={phone}]",
        f"[customer_name={customer_name or 'Cliente'}]",
        f"[branch={branch or 'periferico'}]",
        f"[vehicle_name={vehicle_name}]",
        f"[interested_vehicle={vehicle_name}]",
    ]
    if vehicle_price_f:
        context_bits.append(f"[vehicle_price={vehicle_price_f}]")
    if vehicle_year is not None:
        context_bits.append(f"[vehicle_year={int(vehicle_year)}]")

    financing_hint = (
        f"con phone={phone}, down_payment, vehicle_price"
        + (f"={vehicle_price_f}" if vehicle_price_f else "")
        + f", vehicle_name={vehicle_name!r}"
        + (f", vehicle_year={int(vehicle_year)}" if vehicle_year is not None else "")
        + (
            f", term_months={int(term_from_msg)}"
            if term_from_msg is not None
            else ""
        )
        + ", send_whatsapp=true. "
    )
    instructions = (
        "Eres Beatriz de Autosell en WhatsApp. "
        + WA_QUALIFY_INVENTORY_SNIPPET
        + "Si el cliente pide valuación / avalúo / 'cuánto me dan por' / 'a cuenta' / "
        "'estimas' / 'toman' / 'versión LE|Base|Sense', DEBES llamar get_tradein_valuation "
        "(o estimate_tradein) con brand, model, year, mileage y version — "
        "NUNCA inventes montos Autométrica ni digas 'ciento cincuenta mil' u otras "
        "cifras fijas: solo usa el resultado exacto de la herramienta. "
        "Si ya hay tradein_summary / valor_compra en contexto, reutilízalo o "
        "re-llama get_tradein_valuation al cambiar la versión. "
        "Si el cliente quiere agendar cita / valuación física / prueba de manejo, "
        "confirma la cita (create_crm_lead / book) SIN llamar calculate_financing "
        "aunque falte precio o enganche — la cita de trade-in no requiere corrida. "
        "NUNCA reutilices calculate_financing ni el vehículo anterior (Mustang, etc.) "
        "para una valuación. "
        "Si el cliente da un enganche/cantidad (y NO está solo agendando cita), "
        "DEBES llamar calculate_financing "
        + financing_hint
        + "USA el year del inventario (vehicle_year) en calculate_financing. "
        "Si el cliente cambia de unidad (otra marca/modelo), actualiza vehicle_name "
        "al vehículo NUEVO en calculate_financing / query_inventory — no reutilices "
        "un auto anterior. "
        "Después de la cotización/PDF, NO digas que un asesor contactará. "
        f"Pregunta exactamente: {CITA_FOLLOWUP.format(branch=label)}"
    )
    if meta.get("tradein_summary"):
        instructions += (
            f" Valuación Autométrica vigente (NO inventes otra): "
            f"{meta.get('tradein_summary')}. "
        )
    if meta.get("valor_compra") not in (None, ""):
        instructions += f" Valor Compra numérico: {meta.get('valor_compra')}. "
    if down is not None:
        instructions += (
            f" Enganche detectado en este mensaje: {down:.0f}. "
            "Llama calculate_financing YA."
        )
    if term_from_msg is not None:
        instructions += f" Plazo detectado: {term_from_msg} meses."

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
    price_from_tools, name_from_tools, year_from_tools = _match_from_tool_blobs(blobs)
    name_from_args = _vehicle_from_tool_call_args(payload)
    if price_from_tools:
        vehicle_price_f = price_from_tools
    if name_from_args:
        vehicle_name = sanitize_vehicle_title(name_from_args)
    elif name_from_tools:
        vehicle_name = sanitize_vehicle_title(name_from_tools)
    if year_from_tools is not None:
        vehicle_year = year_from_tools
    else:
        vehicle_year = extract_model_year(vehicle_name) or vehicle_year

    meta_update: dict[str, Any] = {}
    if vehicle_price_f:
        meta_update["vehicle_price"] = vehicle_price_f
    if vehicle_name and vehicle_name != "Vehículo":
        meta_update["vehicle_name"] = vehicle_name
        meta_update["interested_vehicle"] = vehicle_name
    if vehicle_year is not None:
        meta_update["vehicle_year"] = int(vehicle_year)
    if down is not None:
        meta_update["last_down_payment"] = down
    if term_from_msg is not None:
        meta_update["last_term_months"] = int(term_from_msg)

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
                vehicle_year=vehicle_year,
            )
        except Exception:
            pass

    financing_forced = False
    financing_sent = "calculate_financing" in tools or "get_financing" in tools
    tradein_forced = False
    tradein_sent = any(
        t in tools
        for t in (
            "get_tradein_valuation",
            "estimate_tradein",
            "get_tradein",
            "tradein",
        )
    )
    forced_speech = ""
    tradein_summary: str | None = None
    term_for_quote = (
        term_from_msg or int(meta.get("last_term_months") or 0) or DEFAULT_TERM_MONTHS
    )

    # Fallback trade-in force (early path above is preferred).
    if detect_tradein_intent(message) and not tradein_sent:
        try:
            forced_ti = force_get_tradein_valuation(
                text=message,
                phone=phone,
                prior=_prior_tradein_from_meta(meta),
            )
            if forced_ti.get("speech") and forced_ti.get("ok") is not False:
                forced_speech = str(forced_ti["speech"])
                tradein_summary = forced_speech
                tradein_forced = True
                tradein_sent = True
                tools = [*tools, "get_tradein_valuation"]
                details = forced_ti.get("details") if isinstance(forced_ti, dict) else {}
                if isinstance(details, dict):
                    meta_update.update(_tradein_meta_fields(details, forced_speech))
                    # Do not overwrite inventory vehicle_name with trade-in.
        except Exception as exc:
            print(
                f"WARN force_get_tradein_valuation failed phone={phone}: {exc}",
                flush=True,
            )

    if down is not None and not financing_sent and not tradein_forced and not appointment.requested:
        try:
            forced = force_calculate_financing(
                phone=phone,
                down_payment=down,
                customer_name=customer_name,
                vehicle_name=vehicle_name,
                vehicle_price=vehicle_price_f,
                vehicle_year=vehicle_year,
                term_months=term_for_quote,
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
                    vehicle_year=vehicle_year,
                )
            except Exception:
                pass
        except Exception as exc:
            print(
                f"WARN force_calculate_financing failed phone={phone}: {exc}",
                flush=True,
            )

    reply = extract_assistant_text(payload)
    if (financing_forced or tradein_forced) and forced_speech:
        reply = forced_speech
    # Scrub stale LLM guide inventions when we already have Autométrica speech.
    stale_amt = re.search(
        r"ciento\s+cincuenta\s+mil|\$?\s*150[\s.,]?000",
        reply or "",
        re.IGNORECASE,
    )
    if stale_amt:
        real = (tradein_summary or str(meta.get("tradein_summary") or "")).strip()
        if real:
            reply = real
        elif forced_speech:
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
            tradein_sent=tradein_sent,
            tradein_forced=tradein_forced,
            tools_called=tools,
            vehicle_name=active_vehicle,
            interested_vehicle=active_vehicle,
            tradein_summary=tradein_summary,
        )
    return VapiChatResult(
        reply_text=reply,
        chat_id=chat_id,
        previous_chat_id=prev,
        raw=payload,
        financing_sent=financing_sent,
        financing_forced=financing_forced,
        tradein_sent=tradein_sent,
        tradein_forced=tradein_forced,
        tools_called=tools,
        vehicle_name=active_vehicle,
        interested_vehicle=active_vehicle,
        tradein_summary=tradein_summary,
    )



__all__ = [
    "CITA_FOLLOWUP",
    "DEFAULT_ASSISTANT_ID",
    "ENV_ENABLED",
    "SESSION_RESET_REPLY",
    "TRADEIN_APPLY_CTA",
    "VapiChatResult",
    "VapiChatSessionStore",
    "chat_with_beatriz",
    "clear_wa_session_context",
    "detect_down_payment_amount",
    "detect_session_reset",
    "detect_term_months",
    "detect_tradein_intent",
    "extract_assistant_text",
    "extract_tools_called",
    "extract_tradein_version",
    "force_book_appointment",
    "force_calculate_financing",
    "force_get_tradein_valuation",
    "get_chat_store",
    "is_brief_tradein_followup",
    "reset_chat_store_for_tests",
    "rewrite_reply_keep_interactive",
    "vapi_wa_text_first_enabled",
]
