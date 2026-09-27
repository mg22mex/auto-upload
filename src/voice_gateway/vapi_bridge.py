"""Vapi tool bridge — inventory, financing, trade-in, and CRM lead capture.

Riley tool calls hit::

    POST /vapi/inventory
    POST /vapi/financing
    POST /vapi/tradein
    POST /vapi/lead
    POST /vapi/crm-lead   (alias of /vapi/lead)
    POST /vapi/reset-chat (?phone=…)  — ops: clear HANDOFF → AI_ACTIVE

Run from repo root::

    python -m src.voice_gateway.vapi_bridge
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import xmlrpc.client
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, TypeVar

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover

    def load_dotenv(*_a: Any, **_k: Any) -> bool:
        return False

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env")

logger = logging.getLogger(__name__)
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
    )
logger.setLevel(logging.INFO)

DEFAULT_ODOO_URL = "https://autosellmx.odoo.com"
DEFAULT_ODOO_DB = "autosellmx"
RESULT_LIMIT = 3
INVENTORY_TIMEOUT_SEC = float(os.getenv("VAPI_INVENTORY_TIMEOUT_SEC") or "5.0")
# Brand+model (specific) queries aim for sub-second JSON; keep a tight ceiling.
INVENTORY_SPECIFIC_TIMEOUT_SEC = float(
    os.getenv("VAPI_INVENTORY_SPECIFIC_TIMEOUT_SEC") or "1.5"
)
INVENTORY_TIMEOUT_SPEECH = (
    "El inventario tardó un momento. ¿Agendamos una prueba de manejo "
    "o te envío opciones por WhatsApp?"
)
INVENTORY_NEXT_PROMPT = (
    "Ofrece los detalles del vehículo al cliente y pregúntale si le "
    "gustaría agendar una cita para verlo o probarlo."
)
INVENTORY_NEXT_PROMPT_EMPTY = (
    "Indica que no hay coincidencias disponibles ahora y ofrece buscar "
    "otra marca, modelo o presupuesto."
)
INVENTORY_NEXT_PROMPT_TIMEOUT = (
    "El inventario tardó; ofrece agendar prueba de manejo o enviar "
    "opciones por WhatsApp sin inventar precios."
)
DEFAULT_TERM_MONTHS = 48
LEAD_TITLE_PREFIX = "Llamada Paulina - "
VAPI_LEAD_CHANNEL = "Voice"
# Odoo Beatriz pipeline (see src.lead_routing STAGE_BEATRIZ_*).
STAGE_BEATRIZ_LEAD = "Beatriz Lead"
STAGE_BEATRIZ_CITA = "Beatriz Cita"
CITA_FOLLOWUP_PROMPT = (
    "¿Te gustaría agendar una cita en sucursal {branch} "
    "para ver la unidad o realizar prueba de manejo?"
)
_WA_PHONE_RE = re.compile(
    r"\[whatsapp_phone=([+\d][\d\s-]{7,20})\]", re.IGNORECASE
)
_WA_NAME_RE = re.compile(r"\[customer_name=([^\]]+)\]", re.IGNORECASE)
_WA_BRANCH_RE = re.compile(r"\[branch=([^\]]+)\]", re.IGNORECASE)
_WA_VEHICLE_RE = re.compile(r"\[vehicle_name=([^\]]+)\]", re.IGNORECASE)
_WA_PRICE_RE = re.compile(r"\[vehicle_price=([0-9.]+)\]", re.IGNORECASE)

app = FastAPI(
    title="Autosell Vapi Bridge",
    version="1.2.0",
    description="Inventory / financing / trade-in / CRM lead tools for Vapi Riley",
)


@app.on_event("startup")
def _warm_odoo_connection() -> None:
    """Prime XML-RPC auth so the first /vapi/inventory avoids cold-start lag."""
    try:
        connect_odoo()
        logger.info("odoo connection warmed at startup")
    except Exception as exc:  # noqa: BLE001 — bridge must still boot without Odoo
        logger.warning("odoo warm skipped: %s", exc)

T = TypeVar("T")


class InventoryArgs(BaseModel):
    brand: str | None = None
    model: str | None = None
    max_price: float | None = Field(default=None, ge=0)
    year: int | None = Field(default=None, ge=1950, le=2100)


_EMPTY_TOKENS = frozenset(
    {"", "null", "none", "undefined", "n/a", "na", "-", "unknown", "desconocido"}
)


def _coerce_optional_str(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or text.lower() in _EMPTY_TOKENS:
        return None
    return text


def _coerce_optional_float(value: Any) -> float | None:
    """Lenient Vapi numbers: empty/null/junk → None; ``$400,000`` → 400000."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if number >= 0 else None
    text = str(value).strip().lower().replace(",", "")
    if not text or text in _EMPTY_TOKENS:
        return None
    cleaned = re.sub(r"[^\d.]", "", text)
    if not cleaned or cleaned.count(".") > 1:
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return number if number >= 0 else None


def _coerce_optional_year(value: Any) -> int | None:
    """Lenient year: empty/null/out-of-range/junk → None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    if isinstance(value, int):
        return value if 1950 <= value <= 2100 else None
    text = str(value).strip().lower()
    if not text or text in _EMPTY_TOKENS:
        return None
    digits = re.sub(r"\D", "", text)
    if len(digits) != 4:
        return None
    try:
        year = int(digits)
    except ValueError:
        return None
    return year if 1950 <= year <= 2100 else None


class FinancingArgs(BaseModel):
    vehicle_price: float = Field(gt=0)
    term_months: int = Field(default=DEFAULT_TERM_MONTHS, gt=0)
    down_payment: float | None = Field(default=None, ge=0)
    net_trade_in_equity: float | None = Field(default=None, ge=0)
    phone: str | None = None
    customer_name: str | None = None
    vehicle_name: str | None = None
    vehicle_year: int | None = Field(default=None, ge=1950, le=2100)
    branch: str | None = None
    send_whatsapp: bool = False


class TradeInArgs(BaseModel):
    brand: str = Field(min_length=1)
    model: str = Field(min_length=1)
    year: int = Field(ge=1950, le=2100)
    mileage: int = Field(default=0, ge=0)


class LeadArgs(BaseModel):
    name: str = Field(min_length=1)
    phone: str = Field(min_length=5)
    interested_vehicle: str | None = None
    financing_summary: str | None = None
    tradein_summary: str | None = None
    appointment_date: str | None = None
    lead_id: int | None = None


class VapiToolResult(BaseModel):
    toolCallId: str
    result: str


class VapiToolResponse(BaseModel):
    results: list[VapiToolResult]


# --- Spanish number → words (TTS) -------------------------------------------------

_ONES = (
    "cero",
    "uno",
    "dos",
    "tres",
    "cuatro",
    "cinco",
    "seis",
    "siete",
    "ocho",
    "nueve",
    "diez",
    "once",
    "doce",
    "trece",
    "catorce",
    "quince",
    "dieciséis",
    "diecisiete",
    "dieciocho",
    "diecinueve",
)
_TENS = (
    "",
    "",
    "veinte",
    "treinta",
    "cuarenta",
    "cincuenta",
    "sesenta",
    "setenta",
    "ochenta",
    "noventa",
)
_HUNDREDS = (
    "",
    "ciento",
    "doscientos",
    "trescientos",
    "cuatrocientos",
    "quinientos",
    "seiscientos",
    "setecientos",
    "ochocientos",
    "novecientos",
)


def _under_1000(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        if ones == 0:
            return _TENS[tens]
        if tens == 2:
            return f"veinti{_ONES[ones]}" if ones != 1 else "veintiuno"
        return f"{_TENS[tens]} y {_ONES[ones]}"
    if n == 100:
        return "cien"
    hundreds, rest = divmod(n, 100)
    head = _HUNDREDS[hundreds]
    if rest == 0:
        return head
    return f"{head} {_under_1000(rest)}"


def number_to_words_es(n: int) -> str:
    """Integer → Mexican Spanish words (0 … 999_999_999)."""
    n = int(n)
    if n < 0:
        return f"menos {number_to_words_es(-n)}"
    if n < 1000:
        return _under_1000(n)
    if n < 1_000_000:
        thousands, rest = divmod(n, 1000)
        if thousands == 1:
            head = "mil"
        else:
            # 21 → veintiún mil (apocope before mil)
            th = _under_1000(thousands)
            if th.endswith("uno"):
                th = th[: -len("uno")] + "ún"
            head = f"{th} mil"
        if rest == 0:
            return head
        return f"{head} {_under_1000(rest)}"
    millions, rest = divmod(n, 1_000_000)
    if millions == 1:
        head = "un millón"
    else:
        head = f"{number_to_words_es(millions)} millones"
    if rest == 0:
        return head
    return f"{head} {number_to_words_es(rest)}"


def format_price_voice_es(amount: float | Decimal | int | str) -> str:
    """Voice-friendly MXN amount in full Spanish words (no currency symbols)."""
    pesos = int(round(float(amount or 0)))
    if pesos <= 0:
        return "precio no disponible"
    return f"{number_to_words_es(pesos)} pesos"


def format_months_voice_es(months: int) -> str:
    m = int(months)
    words = number_to_words_es(m)
    return f"{words} mes" if m == 1 else f"{words} meses"


# --- Vapi payload parsing --------------------------------------------------------


def _coerce_arg_dict(raw: Any) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"tool arguments are not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("tool arguments must be an object")
    return raw


def iter_vapi_arg_dicts(
    payload: dict[str, Any],
    *,
    flat_keys: tuple[str, ...],
) -> list[tuple[str, dict[str, Any]]]:
    """``(toolCallId, raw_args)`` from Vapi envelope or flat JSON body."""
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    tool_calls = (
        message.get("toolCalls")
        or message.get("toolCallList")
        or payload.get("toolCalls")
        or []
    )
    if isinstance(tool_calls, dict):
        tool_calls = [tool_calls]

    pairs: list[tuple[str, dict[str, Any]]] = []
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        call_id = str(
            call.get("id")
            or call.get("toolCallId")
            or call.get("tool_call_id")
            or ""
        ).strip()
        fn = call.get("function") if isinstance(call.get("function"), dict) else {}
        args = _coerce_arg_dict(fn.get("arguments") if fn else call.get("arguments"))
        if not call_id:
            call_id = f"call_{len(pairs) + 1}"
        pairs.append((call_id, args))

    if pairs:
        return pairs

    if any(k in payload for k in flat_keys):
        return [("call_direct", dict(payload))]

    raise ValueError(
        "No toolCalls found — send Vapi message.toolCalls or a flat JSON body"
    )


def extract_typed_tool_calls(
    payload: dict[str, Any],
    *,
    parser: Callable[[dict[str, Any]], T],
    flat_keys: tuple[str, ...],
) -> list[tuple[str, T]]:
    return [
        (call_id, parser(raw))
        for call_id, raw in iter_vapi_arg_dicts(payload, flat_keys=flat_keys)
    ]


def _parse_inventory_dict(raw: dict[str, Any]) -> InventoryArgs:
    """Soft-parse inventory args — never raise on empty/null/mistyped Vapi fields."""
    brand = _coerce_optional_str(
        raw.get("brand") or raw.get("make") or raw.get("marca")
    )
    model = _coerce_optional_str(raw.get("model") or raw.get("modelo"))
    max_price_raw = raw.get("max_price")
    if max_price_raw is None:
        max_price_raw = raw.get("precio_max")
    if max_price_raw is None:
        max_price_raw = raw.get("maxPrice")
    year_raw = raw.get("year")
    if year_raw is None:
        year_raw = raw.get("anio")
    if year_raw is None:
        year_raw = raw.get("año")
    try:
        return InventoryArgs(
            brand=brand,
            model=model,
            max_price=_coerce_optional_float(max_price_raw),
            year=_coerce_optional_year(year_raw),
        )
    except Exception:
        # Last resort: ignore filters rather than 4xx to Vapi.
        return InventoryArgs()


def _parse_financing_dict(raw: dict[str, Any]) -> FinancingArgs:
    price = (
        raw.get("vehicle_price")
        if raw.get("vehicle_price") is not None
        else raw.get("price") or raw.get("precio") or raw.get("list_price")
    )
    term = (
        raw.get("term_months")
        if raw.get("term_months") is not None
        else raw.get("plazo") or raw.get("months") or DEFAULT_TERM_MONTHS
    )
    down = (
        raw.get("down_payment")
        if raw.get("down_payment") is not None
        else raw.get("enganche") or raw.get("down")
    )
    equity = (
        raw.get("net_trade_in_equity")
        if raw.get("net_trade_in_equity") is not None
        else raw.get("trade_in_equity") or raw.get("valor_compra")
    )
    phone = (
        raw.get("phone")
        or raw.get("mobile")
        or raw.get("telefono")
        or raw.get("customer_phone")
        or raw.get("whatsapp_phone")
    )
    send_flag = raw.get("send_whatsapp")
    if send_flag is None:
        send_flag = raw.get("enviar_whatsapp")
    send_whatsapp = False
    if isinstance(send_flag, bool):
        send_whatsapp = send_flag
    elif send_flag is not None:
        send_whatsapp = str(send_flag).strip().lower() in {"1", "true", "yes", "si", "sí"}
    # Phone present ⇒ deliver summary + PDF unless explicitly disabled.
    if phone and send_flag is None:
        send_whatsapp = True
    vehicle_name = _coerce_optional_str(
        raw.get("vehicle_name")
        or raw.get("interested_vehicle")
        or raw.get("vehicle")
    )
    year_raw = raw.get("vehicle_year")
    if year_raw is None:
        year_raw = raw.get("year")
    if year_raw is None:
        year_raw = raw.get("anio")
    if year_raw is None:
        year_raw = raw.get("año")
    vehicle_year = _coerce_optional_year(year_raw)
    if vehicle_year is None and vehicle_name:
        from src.quote_engine.term_limits import extract_model_year

        vehicle_year = extract_model_year(vehicle_name)
    return FinancingArgs.model_validate(
        {
            "vehicle_price": price,
            "term_months": term,
            "down_payment": down,
            "net_trade_in_equity": equity,
            "phone": _coerce_optional_str(phone),
            "customer_name": _coerce_optional_str(
                raw.get("customer_name") or raw.get("name") or raw.get("client_name")
            ),
            "vehicle_name": vehicle_name,
            "vehicle_year": vehicle_year,
            "branch": _coerce_optional_str(raw.get("branch") or raw.get("sucursal")),
            "send_whatsapp": send_whatsapp,
        }
    )


def _merge_wa_context_into_financing(
    args: FinancingArgs, ctx: dict[str, Any]
) -> FinancingArgs:
    """Fill missing financing fields from WhatsApp chat markers."""
    updates: dict[str, Any] = {}
    if not (args.phone or "").strip() and ctx.get("phone"):
        updates["phone"] = str(ctx["phone"])
        updates["send_whatsapp"] = True
    elif (args.phone or "").strip() and not args.send_whatsapp and ctx.get("phone"):
        updates["send_whatsapp"] = True
    if not (args.customer_name or "").strip() and ctx.get("customer_name"):
        updates["customer_name"] = str(ctx["customer_name"])
    if not (args.vehicle_name or "").strip() and ctx.get("vehicle_name"):
        updates["vehicle_name"] = str(ctx["vehicle_name"])
    if args.vehicle_year is None:
        from src.quote_engine.term_limits import extract_model_year

        year = extract_model_year(
            updates.get("vehicle_name") or args.vehicle_name or ctx.get("vehicle_name")
        )
        if year is not None:
            updates["vehicle_year"] = year
    if not (args.branch or "").strip() and ctx.get("branch"):
        updates["branch"] = str(ctx["branch"])
    # WhatsApp text-first: always deliver PDF when we resolved a phone.
    phone = updates.get("phone") or args.phone
    if phone and not args.send_whatsapp and "send_whatsapp" not in updates:
        updates["send_whatsapp"] = True
    if not updates:
        return args
    return args.model_copy(update=updates)


def _parse_tradein_dict(raw: dict[str, Any]) -> TradeInArgs:
    brand = raw.get("brand") or raw.get("make") or raw.get("marca")
    model = raw.get("model") or raw.get("modelo")
    year = raw.get("year") if raw.get("year") is not None else raw.get("anio") or raw.get("año")
    mileage = (
        raw.get("mileage")
        if raw.get("mileage") is not None
        else raw.get("mileage_km")
        or raw.get("kilometraje")
        or raw.get("km")
        or 0
    )
    return TradeInArgs.model_validate(
        {"brand": brand, "model": model, "year": year, "mileage": mileage}
    )


def _optional_lead_id(value: Any) -> int | None:
    if value in (None, "", False):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_context_lead_id(payload: dict[str, Any]) -> int | None:
    """Pull ``lead_id`` from body, query-injected fields, or Vapi call variables."""
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    call = message.get("call") if isinstance(message.get("call"), dict) else {}
    if not call and isinstance(payload.get("call"), dict):
        call = payload["call"]
    overrides = call.get("assistantOverrides") if isinstance(call.get("assistantOverrides"), dict) else {}
    assistant = call.get("assistant") if isinstance(call.get("assistant"), dict) else {}
    vars_map = overrides.get("variableValues") or assistant.get("variableValues") or {}
    if not isinstance(vars_map, dict):
        vars_map = {}
    for candidate in (
        payload.get("lead_id"),
        payload.get("odoo_lead_id"),
        payload.get("crm_lead_id"),
        message.get("lead_id"),
        vars_map.get("lead_id"),
        vars_map.get("odoo_lead_id"),
    ):
        resolved = _optional_lead_id(candidate)
        if resolved is not None:
            return resolved
    return None


def _parse_lead_dict(raw: dict[str, Any]) -> LeadArgs:
    name = raw.get("name") or raw.get("client_name") or raw.get("contact_name")
    phone = raw.get("phone") or raw.get("mobile") or raw.get("telefono")
    vehicle = _coerce_optional_str(
        raw.get("interested_vehicle")
        or raw.get("vehicle")
        or raw.get("vehicle_info")
        or raw.get("vehicle_name")
    )
    financing = _coerce_optional_str(
        raw.get("financing_summary") or raw.get("financing") or raw.get("quote_summary")
    )
    tradein = _coerce_optional_str(
        raw.get("tradein_summary") or raw.get("trade_in_summary") or raw.get("tradein")
    )
    appointment = _coerce_optional_str(
        raw.get("appointment_date")
        or raw.get("appointment")
        or raw.get("cita")
        or raw.get("preferred_visit")
    )
    lead_id = _optional_lead_id(
        raw.get("lead_id") or raw.get("odoo_lead_id") or raw.get("crm_lead_id")
    )
    return LeadArgs.model_validate(
        {
            "name": name,
            "phone": phone,
            "interested_vehicle": vehicle,
            "financing_summary": financing,
            "tradein_summary": tradein,
            "appointment_date": appointment,
            "lead_id": lead_id,
        }
    )


def extract_tool_calls(payload: dict[str, Any]) -> list[tuple[str, InventoryArgs]]:
    """Inventory helper (kept for existing tests)."""
    return extract_typed_tool_calls(
        payload,
        parser=_parse_inventory_dict,
        flat_keys=("brand", "make", "marca", "model", "modelo", "max_price", "year", "anio", "año"),
    )


# --- Odoo inventory --------------------------------------------------------------


def _odoo_settings() -> dict[str, str]:
    url = (os.getenv("ODOO_URL") or DEFAULT_ODOO_URL).strip().rstrip("/")
    db = (os.getenv("ODOO_DB") or DEFAULT_ODOO_DB).strip()
    user = (os.getenv("ODOO_USER") or os.getenv("ODOO_USERNAME") or "").strip()
    password = (
        os.getenv("ODOO_PASS")
        or os.getenv("ODOO_PASSWORD")
        or os.getenv("ODOO_API_KEY")
        or ""
    ).strip()
    return {"url": url, "db": db, "user": user, "password": password}


_odoo_conn_lock = threading.Lock()
_odoo_conn_cache: tuple[str, int, Any, str] | None = None


def connect_odoo(
    *,
    url: str | None = None,
    db: str | None = None,
    user: str | None = None,
    password: str | None = None,
    force_refresh: bool = False,
) -> tuple[str, int, Any, str]:
    """Authenticate once per process; reuse uid/proxies for inventory latency."""
    global _odoo_conn_cache
    cfg = _odoo_settings()
    url = (url or cfg["url"]).rstrip("/")
    db = db or cfg["db"]
    user = user or cfg["user"]
    password = password or cfg["password"]
    if not all((url, db, user, password)):
        raise RuntimeError(
            "Missing Odoo env: need ODOO_URL, ODOO_DB, "
            "ODOO_USER|ODOO_USERNAME, and ODOO_PASS|ODOO_PASSWORD|ODOO_API_KEY"
        )
    with _odoo_conn_lock:
        if _odoo_conn_cache is not None and not force_refresh:
            return _odoo_conn_cache
        common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common", allow_none=True)
        uid = common.authenticate(db, user, password, {})
        if not uid:
            raise RuntimeError("Odoo authenticate failed (check DB/user/API key)")
        models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object", allow_none=True)
        _odoo_conn_cache = (db, int(uid), models, password)
        return _odoo_conn_cache


def build_domain(args: InventoryArgs) -> list[Any]:
    """Domain for Vapi inventory — active available SKUs, indexed scalars only."""
    from src.odoo_sync.inventory import build_inventory_domain

    return build_inventory_domain(
        brand=args.brand,
        model=args.model,
        max_price=float(args.max_price) if args.max_price is not None else None,
        year=args.year,
        available_only=True,
    )


def _clean_name(name: str) -> str:
    """Strip lot markers from title for TTS (keep readable model/brand/year)."""
    text = re.sub(r"\s+", " ", (name or "").strip())
    text = re.sub(r"\s*[\*\+]\s*", " ", text)
    # Mid-title consignment marker " - " only (not hyphens inside words).
    text = re.sub(r"\s+-\s+", " ", text)
    if text[:1] in "*-+":
        text = text[1:].lstrip()
    if text[-1:] in "*-+":
        text = text[:-1].rstrip()
    return re.sub(r"\s+", " ", text).strip()


# Catalog / Odoo title markers → physical lot (Beatriz must voice this verbatim).
# Marker ``-`` is internal consignación stock — never say "Consignación" to the caller.
BRANCH_MARKER_LOCATION: dict[str, str] = {
    "*": "Lote Periférico",
    "+": "Lote San Felipe",
    "-": (
        "Disponible para entrega en la sucursal de tu preferencia "
        "(Periférico o San Felipe)"
    ),
}


def branch_marker_from_title(title: str) -> str | None:
    """Detect ``*`` / ``+`` / ``-`` as leading, trailing, or mid-title lot marker.

    Live Odoo titles look like ``Cx5 * Mazda 2020`` or ``Sport - Mazda 2022``.
    Catalog/Marketplace titles often trail the marker (``… IGT *``).
    """
    text = (title or "").strip()
    if not text:
        return None
    if text[0] in BRANCH_MARKER_LOCATION:
        return text[0]
    if text[-1] in BRANCH_MARKER_LOCATION:
        return text[-1]
    for marker in ("*", "+", "-"):
        token = f" {marker} "
        if token in f" {text} ":
            return marker
    return None


def location_speech_for_title(title: str) -> str:
    """Exact lot / delivery phrase for TTS — never 'Sucursal Autosell' or 'Consignación'."""
    marker = branch_marker_from_title(title)
    if marker is None:
        return "Ubicación: consultar disponibilidad en sucursal"
    place = BRANCH_MARKER_LOCATION[marker]
    if marker == "-":
        return place  # already a full customer-facing sentence
    return f"Ubicación: {place}"


def branch_from_vehicle_title(title: str) -> tuple[str, str | None]:
    """Map vehicle title marker/keywords → (crm branch key, physical_location label).

    ``*`` / internal ``-`` stock → Periférico desk; ``+`` → San Felipe.
    """
    from src.config import BRANCH_LABELS, PRIMARY_BRANCH, branch_for_tag
    from src.odoo_sync.crm import infer_physical_location

    marker = branch_marker_from_title(title)
    if marker:
        key = branch_for_tag(marker)
        return key, BRANCH_LABELS.get(key, "Periférico")
    inferred = infer_physical_location(title)
    if inferred:
        return inferred, BRANCH_LABELS.get(inferred, "Periférico")
    return PRIMARY_BRANCH, None


def format_price_compact_mxn(amount: float | int | None) -> str:
    """Compact price for Vapi JSON tools: ``$285,000 MXN``."""
    pesos = int(round(float(amount or 0)))
    if pesos <= 0:
        return "precio no disponible"
    return f"${pesos:,} MXN"


def location_compact_for_title(title: str) -> str:
    """Compact lot label for JSON (e.g. ``Sucursal Periférico (*)``)."""
    marker = branch_marker_from_title(title)
    if marker == "*":
        return "Sucursal Periférico (*)"
    if marker == "+":
        return "Sucursal San Felipe (+)"
    if marker == "-":
        return "Entrega en Periférico o San Felipe (preferencia del cliente)"
    return "Sucursal por confirmar"


def is_specific_inventory_query(args: InventoryArgs) -> bool:
    """Brand + model both present → targeted indexed ``name`` ilike query."""
    return bool((args.brand or "").strip() and (args.model or "").strip())


def format_inventory_payload(
    rows: list[dict[str, Any]],
    args: InventoryArgs,
    *,
    timed_out: bool = False,
) -> dict[str, Any]:
    """Compact JSON for Vapi ``query_inventory`` (no long TTS essay)."""
    if timed_out:
        return {
            "found": False,
            "count": 0,
            "vehicles": [],
            "next_prompt": INVENTORY_NEXT_PROMPT_TIMEOUT,
        }
    vehicles: list[dict[str, str]] = []
    for row in rows:
        raw_name = str(row.get("name") or "Vehículo")
        vehicles.append(
            {
                "model": _clean_name(raw_name),
                "price": format_price_compact_mxn(row.get("list_price")),
                "location": location_compact_for_title(raw_name),
                "code": str(row.get("default_code") or "").strip() or "",
            }
        )
    found = bool(vehicles)
    return {
        "found": found,
        "count": len(vehicles),
        "vehicles": vehicles,
        "next_prompt": INVENTORY_NEXT_PROMPT if found else INVENTORY_NEXT_PROMPT_EMPTY,
    }


def format_inventory_speech(rows: list[dict[str, Any]], args: InventoryArgs) -> str:
    """Short TTS with explicit lot / delivery location for Beatriz."""
    label_parts = [p for p in (args.brand, args.model) if p]
    filter_txt = " ".join(p.strip().title() for p in label_parts) or "tu búsqueda"
    if args.year is not None:
        filter_txt = f"{filter_txt} {int(args.year)}".strip()

    if not rows:
        return (
            f"No encontré {filter_txt} disponible ahora. "
            "¿Buscamos otra marca o presupuesto?"
        )

    parts = [
        f"Tengo {number_to_words_es(len(rows))} para {filter_txt}. "
        "Di exactamente el nombre del lote cuando diga Ubicación "
        "(Lote Periférico o Lote San Felipe). "
        "Si la unidad dice que está disponible para entrega en la sucursal "
        "de tu preferencia, y el cliente pregunta dónde está, ofrécele "
        "llevarla a Lote Periférico o Lote San Felipe, la que le convenga. "
        "No digas Sucursal Autosell."
    ]
    for index, row in enumerate(rows, start=1):
        raw_name = str(row.get("name") or "Vehículo")
        name = _clean_name(raw_name)
        price = format_price_voice_es(float(row.get("list_price") or 0))
        loc = location_speech_for_title(raw_name)
        parts.append(
            f"{number_to_words_es(index)}: {name}, {price}, {loc}."
        )
    parts.append("¿Cuál te interesa?")
    return " ".join(parts)


def search_inventory(
    args: InventoryArgs,
    *,
    models: Any | None = None,
    db: str | None = None,
    uid: int | None = None,
    password: str | None = None,
    limit: int = RESULT_LIMIT,
) -> list[dict[str, Any]]:
    """Sync Odoo ``search_read`` via ``query_inventory`` (limit ≤ 3)."""
    from src.odoo_sync.inventory import RESULT_LIMIT as INV_LIMIT
    from src.odoo_sync.inventory import query_inventory

    if models is None:
        db, uid, models, password = connect_odoo()
    assert db is not None and uid is not None and password is not None

    def execute_kw(
        model: str,
        method: str,
        args_: list[Any],
        kwargs: dict[str, Any] | None = None,
    ) -> Any:
        return models.execute_kw(db, uid, password, model, method, args_, kwargs or {})

    return query_inventory(
        execute_kw,
        brand=args.brand,
        model=args.model,
        max_price=float(args.max_price) if args.max_price is not None else None,
        year=args.year,
        limit=min(int(limit), INV_LIMIT),
        available_only=True,
        use_cache=False,  # always live Odoo for /vapi/inventory
    )


def _search_inventory_blocking(args: InventoryArgs) -> list[dict[str, Any]]:
    """Full connect + search for thread offload (keeps event loop free)."""
    return search_inventory(args)


async def _search_inventory_live(args: InventoryArgs) -> list[dict[str, Any]]:
    """Live Odoo RPC in a worker thread (no inventory row cache)."""
    return await asyncio.to_thread(_search_inventory_blocking, args)


async def handle_inventory_payload(payload: dict[str, Any]) -> VapiToolResponse:
    calls = extract_tool_calls(payload)
    results: list[VapiToolResult] = []
    for call_id, args in calls:
        specific = is_specific_inventory_query(args)
        timeout_sec = (
            INVENTORY_SPECIFIC_TIMEOUT_SEC if specific else INVENTORY_TIMEOUT_SEC
        )
        logger.info(
            "inventory toolCallId=%s specific=%s timeout=%.2fs "
            "args brand=%r model=%r year=%r max_price=%r",
            call_id,
            specific,
            timeout_sec,
            args.brand,
            args.model,
            args.year,
            args.max_price,
        )
        timed_out = False
        rows: list[dict[str, Any]] = []
        try:
            rows = await asyncio.wait_for(
                _search_inventory_live(args),
                timeout=timeout_sec,
            )
            logger.info(
                "inventory toolCallId=%s pre-TTS rows=%s",
                call_id,
                [
                    {
                        "id": r.get("id"),
                        "name": r.get("name"),
                        "list_price": r.get("list_price"),
                        "default_code": r.get("default_code"),
                    }
                    for r in rows
                ],
            )
        except asyncio.TimeoutError:
            timed_out = True
            logger.warning(
                "inventory search timed out after %.1fs for %s — returning empty JSON",
                timeout_sec,
                call_id,
            )
        except Exception:
            timed_out = True
            logger.exception("inventory search failed for %s", call_id)

        payload_out = format_inventory_payload(rows, args, timed_out=timed_out)
        # Vapi tool result is a string — compact JSON for the LLM (no long TTS).
        result_text = json.dumps(payload_out, ensure_ascii=False, separators=(",", ":"))
        results.append(VapiToolResult(toolCallId=call_id, result=result_text))
    response = VapiToolResponse(results=results)
    logger.info(
        "inventory final response: %s",
        response.model_dump() if hasattr(response, "model_dump") else response,
    )
    return response


# --- Financing -------------------------------------------------------------------


def extract_whatsapp_context(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull ``[whatsapp_phone=…]`` markers from Vapi chat artifact / body text."""
    chunks: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, str):
            chunks.append(node)
            return
        if isinstance(node, dict):
            for key in (
                "message",
                "content",
                "input",
                "text",
                "result",
            ):
                val = node.get(key)
                if isinstance(val, str):
                    chunks.append(val)
                elif isinstance(val, list):
                    for item in val:
                        _walk(item)
            for key in ("artifact", "messages", "messagesOpenAIFormatted"):
                if key in node:
                    _walk(node[key])
            msg = node.get("message")
            if isinstance(msg, dict):
                _walk(msg)
            return
        if isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(payload)
    blob = "\n".join(chunks)
    out: dict[str, Any] = {}
    m = _WA_PHONE_RE.search(blob)
    if m:
        out["phone"] = re.sub(r"\D", "", m.group(1))
    m = _WA_NAME_RE.search(blob)
    if m:
        out["customer_name"] = m.group(1).strip()
    m = _WA_BRANCH_RE.search(blob)
    if m:
        out["branch"] = m.group(1).strip()
    m = _WA_VEHICLE_RE.search(blob)
    if m:
        out["vehicle_name"] = m.group(1).strip()
    m = _WA_PRICE_RE.search(blob)
    if m:
        try:
            out["vehicle_price"] = float(m.group(1))
        except ValueError:
            pass
    return out


def branch_label_for_prompt(branch: str | None) -> str:
    key = (branch or "").strip().lower()
    if key in {"san_felipe", "san felipe", "+"}:
        return "San Felipe"
    return "Periférico"


def resolve_beatriz_stage(
    *,
    appointment_date: str | None = None,
    financing: bool = False,
) -> str:
    """Map tool outcome → Odoo stage: cita wins over financing / interest."""
    del financing  # reserved — financing alone stays Beatriz Lead
    if (appointment_date or "").strip():
        return STAGE_BEATRIZ_CITA
    return STAGE_BEATRIZ_LEAD


def format_financing_speech(args: FinancingArgs, quote: Any) -> str:
    down = format_price_voice_es(quote.down_payment)
    months = format_months_voice_es(quote.term_months)
    monthly = format_price_voice_es(quote.estimated_monthly_payment)
    branch = branch_label_for_prompt(args.branch)
    cita = CITA_FOLLOWUP_PROMPT.format(branch=branch)
    base = (
        f"Con un enganche de {down} a {months}, tu mensualidad estimada "
        f"con Scotiabank sería de {monthly}."
    )
    note = getattr(quote, "term_cap_note", None)
    if note:
        base = f"{base} {note}"
    if args.send_whatsapp and (args.phone or "").strip():
        return (
            f"{base} Te envié el resumen y la tabla de amortización por WhatsApp. "
            f"{cita}"
        )
    return f"{base} {cita}"


def run_financing_quote(args: FinancingArgs) -> Any:
    from src.quote_engine.engine import CalibratedQuoteEngine
    from src.quote_engine.term_limits import extract_model_year

    year = args.vehicle_year
    if year is None:
        year = extract_model_year(args.vehicle_name)

    engine = CalibratedQuoteEngine()
    return engine.calculate(
        args.vehicle_price,
        int(args.term_months),
        down_payment=args.down_payment,
        net_trade_in_equity=args.net_trade_in_equity,
        vehicle_year=year,
    )


def dispatch_financing_whatsapp(
    args: FinancingArgs,
    quote: Any,
    *,
    whatsapp_client: Any | None = None,
) -> dict[str, Any]:
    """Generate CrediAuto PDF and send text + document to the customer."""
    from src.notifications.whatsapp import notify_financing_quote
    from src.pdf_engine.generator import (
        generate_financing_quote_pdf,
        sanitize_vehicle_title,
    )

    phone = (args.phone or "").strip()
    if not phone:
        return {"sent": False, "skipped_reason": "missing phone"}

    try:
        pdf_path = generate_financing_quote_pdf(
            quote,
            vehicle_data={
                "name": sanitize_vehicle_title(
                    (args.vehicle_name or "Vehículo").strip() or "Vehículo"
                )
            },
            customer_name=args.customer_name,
            contact={"branch_label": (args.branch or "Autosell").strip() or "Autosell"},
            filename="financing_quote.pdf",
        )
    except Exception as exc:
        logger.exception("financing PDF generation failed")
        return {"sent": False, "error": f"pdf: {exc}"}

    try:
        result = notify_financing_quote(
            phone=phone,
            pdf_path=pdf_path,
            name=args.customer_name,
            vehicle_price=float(getattr(quote, "vehicle_price", args.vehicle_price)),
            down_payment=float(getattr(quote, "down_payment", args.down_payment or 0)),
            term_months=int(getattr(quote, "term_months", args.term_months)),
            monthly_payment=float(getattr(quote, "estimated_monthly_payment", 0)),
            vehicle_name=args.vehicle_name,
            branch=args.branch,
            whatsapp_client=whatsapp_client,
            caption="Autosell — financing_quote.pdf (Scotiabank CrediAuto)",
        )
        logger.warning(
            "dispatch_financing_whatsapp sent=%s skipped=%s error=%s pdf=%s",
            result.sent,
            result.skipped_reason,
            result.error,
            pdf_path,
        )
        return {**result.as_dict(), "pdf_path": str(pdf_path)}
    except Exception as exc:
        logger.exception("dispatch_financing_whatsapp failed: %s", exc)
        return {"sent": False, "error": str(exc), "pdf_path": str(pdf_path)}


def format_financing_crm_summary(args: FinancingArgs, quote: Any) -> str:
    monthly = getattr(quote, "estimated_monthly_payment", None)
    down = getattr(quote, "down_payment", args.down_payment)
    term = getattr(quote, "term_months", args.term_months)
    bits = [
        f"precio ${float(args.vehicle_price):,.0f}",
        f"enganche ${float(down or 0):,.0f}",
        f"{int(term)} meses",
    ]
    if monthly is not None:
        bits.append(f"mensualidad ${float(monthly):,.2f}")
    return " / ".join(bits)


def upsert_financing_crm_lead(
    args: FinancingArgs,
    quote: Any,
    *,
    manager: Any | None = None,
) -> dict[str, Any]:
    """Stamp Beatriz Lead + MG Quote Lead when financing is calculated."""
    from src.odoo_sync.crm import CRMLeadManager

    phone = (args.phone or "").strip()
    if not phone:
        return {"skipped": True, "reason": "missing phone"}

    name = (args.customer_name or "").strip() or "Cliente WhatsApp"
    vehicle = (args.vehicle_name or "").strip() or "Consulta financiamiento"
    branch_key, physical_label = branch_from_vehicle_title(vehicle)
    summary = format_financing_crm_summary(args, quote)
    stage = resolve_beatriz_stage(financing=True)
    notes = (
        f"Canal: Vapi / Beatriz (financing)\n"
        f"Cliente: {name}\n"
        f"Teléfono: {phone}\n"
        f"Vehículo: {vehicle}\n"
        f"Financiamiento: {summary}\n"
    )
    title = f"{LEAD_TITLE_PREFIX}{name}"[:128]
    payload: dict[str, Any] = {
        "name": name,
        "client_name": name,
        "phone": phone,
        "vehicle_info": vehicle,
        "vehicle_name": vehicle,
        "description": notes,
        "notes": notes,
        "channel": VAPI_LEAD_CHANNEL,
        "opportunity_name": title,
        "stage_name": stage,
        "assign_round_robin": True,
        "preserve_salesperson": True,
    }
    if physical_label:
        payload["physical_location"] = physical_label
    crm = manager or CRMLeadManager()
    result = crm.create_or_update_lead(payload, branch=branch_key)
    return {
        **result,
        "opportunity_name": title,
        "stage_name": stage,
        "financing_summary": summary,
    }


def handle_financing_payload(
    payload: dict[str, Any],
    *,
    background_tasks: BackgroundTasks | None = None,
    whatsapp_client: Any | None = None,
    manager: Any | None = None,
) -> VapiToolResponse:
    wa_ctx = extract_whatsapp_context(payload)
    calls = extract_typed_tool_calls(
        payload,
        parser=_parse_financing_dict,
        flat_keys=(
            "vehicle_price",
            "price",
            "precio",
            "list_price",
            "term_months",
            "plazo",
            "down_payment",
            "enganche",
            "phone",
            "mobile",
            "telefono",
            "customer_name",
            "name",
            "vehicle_name",
            "vehicle_year",
            "year",
            "anio",
            "send_whatsapp",
        ),
    )
    results: list[VapiToolResult] = []
    for call_id, args in calls:
        args = _merge_wa_context_into_financing(args, wa_ctx)
        quote: Any | None = None
        try:
            quote = run_financing_quote(args)
            speech = format_financing_speech(args, quote)
        except Exception as exc:
            logger.exception("financing quote failed for %s", call_id)
            speech = (
                "No pude calcular la corrida de financiamiento en este momento. "
                f"Detalle técnico: {type(exc).__name__}. "
                "¿Me confirmas el precio del vehículo, el enganche y el plazo en meses?"
            )
            results.append(VapiToolResult(toolCallId=call_id, result=speech))
            continue

        phone = (args.phone or "").strip()
        # Always push PDF on WhatsApp when phone is known (text-first path).
        if phone:
            args = args.model_copy(update={"send_whatsapp": True})
        crm_result: dict[str, Any] | None = None
        if phone and quote is not None:
            try:
                crm_result = upsert_financing_crm_lead(
                    args, quote, manager=manager
                )
            except Exception:
                logger.exception("financing CRM upsert failed for %s", call_id)
                crm_result = None

        def _queue_financing_side_effects(
            fin_args: FinancingArgs = args,
            fin_quote: Any = quote,
            fin_crm: dict[str, Any] | None = crm_result,
            fin_phone: str = phone,
        ) -> None:
            if fin_args.send_whatsapp and fin_phone and fin_quote is not None:
                dispatch_financing_whatsapp(
                    fin_args, fin_quote, whatsapp_client=whatsapp_client
                )
            if fin_crm and not fin_crm.get("skipped") and not fin_crm.get("dry_run"):
                dispatch_appointment_rep_alert(
                    LeadArgs(
                        name=(fin_args.customer_name or "Cliente").strip() or "Cliente",
                        phone=fin_phone,
                        interested_vehicle=fin_args.vehicle_name,
                        financing_summary=fin_crm.get("financing_summary"),
                        appointment_date=None,
                    ),
                    branch=str(
                        fin_crm.get("branch") or fin_args.branch or "periferico"
                    ),
                    lead_id=fin_crm.get("lead_id"),
                    assignment=fin_crm.get("assignment")
                    if isinstance(fin_crm.get("assignment"), dict)
                    else None,
                    stage_name=str(
                        fin_crm.get("stage_name") or STAGE_BEATRIZ_LEAD
                    ),
                    whatsapp_client=whatsapp_client,
                )

        if phone and quote is not None:
            if background_tasks is not None:
                background_tasks.add_task(_queue_financing_side_effects)
            else:
                _queue_financing_side_effects()
        results.append(VapiToolResult(toolCallId=call_id, result=speech))
    return VapiToolResponse(results=results)


# --- Trade-in (Autométrica via quote_engine.trade_in) -----------------------------


def format_tradein_speech(args: TradeInArgs, valuation: Any) -> str:
    label = f"{args.brand.strip().title()} {args.model.strip()} {args.year}"
    amount = format_price_voice_es(valuation.net_equity)
    matched_note = ""
    raw = getattr(valuation, "raw", None) or {}
    if isinstance(raw, dict) and raw.get("matched") is False:
        matched_note = (
            " Esta es una estimación aproximada porque no hubo coincidencia "
            "exacta en la guía."
        )
    return (
        f"Basado en la guía Autométrica, el valor estimado a cuenta para tu "
        f"{label} es de aproximadamente {amount}, sujeto a inspección física "
        f"en la agencia.{matched_note}"
    )


def run_tradein_valuation(args: TradeInArgs) -> Any:
    # Live path: src.quote_engine.trade_in (not src.trade_in)
    from src.quote_engine.trade_in import TradeInEngine, TradeInVehicle

    vehicle = TradeInVehicle(
        year=int(args.year),
        make=args.brand.strip(),
        model=args.model.strip(),
        mileage_km=int(args.mileage or 0),
    )
    return TradeInEngine().value(vehicle)


def handle_tradein_payload(payload: dict[str, Any]) -> VapiToolResponse:
    calls = extract_typed_tool_calls(
        payload,
        parser=_parse_tradein_dict,
        flat_keys=(
            "brand",
            "make",
            "marca",
            "model",
            "modelo",
            "year",
            "anio",
            "año",
            "mileage",
            "mileage_km",
            "kilometraje",
        ),
    )
    results: list[VapiToolResult] = []
    for call_id, args in calls:
        try:
            valuation = run_tradein_valuation(args)
            speech = format_tradein_speech(args, valuation)
        except Exception as exc:
            logger.exception("trade-in valuation failed for %s", call_id)
            speech = (
                "No pude estimar el valor a cuenta en este momento. "
                f"Detalle técnico: {type(exc).__name__}. "
                "¿Me das marca, modelo, año y kilometraje aproximado de tu auto?"
            )
        results.append(VapiToolResult(toolCallId=call_id, result=speech))
    return VapiToolResponse(results=results)


# --- CRM lead (Odoo via CRMLeadManager) ------------------------------------------


def spell_digits_in_text(text: str) -> str:
    """Replace digit runs with Spanish words for TTS (leave other text intact)."""

    def _repl(match: re.Match[str]) -> str:
        return number_to_words_es(int(match.group(0)))

    return re.sub(r"\d+", _repl, text or "")


def build_lead_notes(args: LeadArgs) -> str:
    lines = [
        "Canal: Vapi / Paulina (voz)",
        f"Cliente: {args.name.strip()}",
        f"Teléfono: {args.phone.strip()}",
    ]
    if args.interested_vehicle:
        lines.append(f"Vehículo de interés: {args.interested_vehicle.strip()}")
    if args.financing_summary:
        lines.append(f"Financiamiento: {args.financing_summary.strip()}")
    if args.tradein_summary:
        lines.append(f"Auto a cambio: {args.tradein_summary.strip()}")
    if args.appointment_date:
        lines.append(f"Cita preferida: {args.appointment_date.strip()}")
    return "\n".join(lines)


def format_lead_speech(
    args: LeadArgs,
    *,
    dry_run: bool = False,
    status: str = "created",
) -> str:
    name = args.name.strip()
    if dry_run:
        prefix = "Cita registrada en modo prueba para "
    elif status == "updated":
        prefix = "Actualicé tu expediente y confirmé la cita para "
    else:
        prefix = "Cita registrada con éxito para "
    if args.appointment_date and args.appointment_date.strip():
        when = spell_digits_in_text(args.appointment_date.strip())
        return f"{prefix}{name}, {when}. Te esperamos en Autosell."
    return f"{prefix}{name}. Te esperamos en Autosell."


def create_vapi_lead(
    args: LeadArgs,
    *,
    manager: Any | None = None,
) -> dict[str, Any]:
    """Upsert crm.lead: new → Paulina title + RR; existing → chatter + stage.

    Branch / ``team_id`` comes from vehicle lot marker (``*`` Periférico,
    ``+`` San Felipe, ``-`` consignación → Periférico desk) so Odoo Round Robin
    assigns the correct branch seller.
    """
    from src.odoo_sync.crm import CRMLeadManager

    crm = manager or CRMLeadManager()
    vehicle = (args.interested_vehicle or "").strip() or "Consulta general"
    branch_key, physical_label = branch_from_vehicle_title(vehicle)
    notes = build_lead_notes(args)
    title = f"{LEAD_TITLE_PREFIX}{args.name.strip()}"[:128]
    stage = resolve_beatriz_stage(
        appointment_date=args.appointment_date,
    )
    payload: dict[str, Any] = {
        "name": args.name.strip(),
        "client_name": args.name.strip(),
        "phone": args.phone.strip(),
        "vehicle_info": vehicle,
        "vehicle_name": vehicle,
        "description": notes,
        "notes": notes,
        "channel": VAPI_LEAD_CHANNEL,
        "appointment_date": (args.appointment_date or "").strip() or None,
        "opportunity_name": title,
        "stage_name": stage,
        "assign_round_robin": True,
        "preserve_salesperson": True,
    }
    if physical_label:
        payload["physical_location"] = physical_label
    if args.lead_id is not None:
        payload["lead_id"] = int(args.lead_id)
    logger.warning(
        "crm-lead branch=%s physical_location=%s vehicle=%s stage=%s",
        branch_key,
        physical_label,
        vehicle[:80],
        stage,
    )
    result = crm.create_or_update_lead(payload, branch=branch_key)
    return {**result, "opportunity_name": title, "stage_name": stage}


def _safe_optional_text(value: str | None) -> str | None:
    """Coerce None / blank / literal 'null' → None for WA templates."""
    return _coerce_optional_str(value)


def dispatch_lead_whatsapp(
    args: LeadArgs,
    *,
    branch: str = "periferico",
    whatsapp_client: Any | None = None,
) -> dict[str, Any]:
    """Background-safe customer confirmation (never raises)."""
    from src.notifications.whatsapp import notify_lead_confirmation

    vehicle = _safe_optional_text(args.interested_vehicle)
    financing = _safe_optional_text(args.financing_summary)
    tradein = _safe_optional_text(args.tradein_summary)
    appointment = _safe_optional_text(args.appointment_date)

    missing = [
        field
        for field, value in (
            ("interested_vehicle", vehicle),
            ("financing_summary", financing),
            ("tradein_summary", tradein),
            ("appointment_date", appointment),
        )
        if not value
    ]
    if missing:
        logger.warning(
            "dispatch_lead_whatsapp missing optional fields=%s name=%s phone=%s branch=%s",
            ",".join(missing),
            (args.name or "")[:40],
            (args.phone or "")[:20],
            branch,
        )
    else:
        logger.warning(
            "dispatch_lead_whatsapp full payload name=%s phone=%s branch=%s",
            (args.name or "")[:40],
            (args.phone or "")[:20],
            branch,
        )

    try:
        result = notify_lead_confirmation(
            name=args.name or "",
            phone=args.phone or "",
            interested_vehicle=vehicle,
            financing_summary=financing,
            tradein_summary=tradein,
            appointment_date=appointment,
            branch=branch,
            whatsapp_client=whatsapp_client,
        )
        logger.warning(
            "dispatch_lead_whatsapp result sent=%s skipped=%s error=%s",
            result.sent,
            result.skipped_reason,
            result.error,
        )
        return result.as_dict()
    except Exception as exc:
        logger.exception("dispatch_lead_whatsapp failed: %s", exc)
        return {"sent": False, "error": str(exc)}


def dispatch_appointment_rep_alert(
    args: LeadArgs,
    *,
    branch: str = "periferico",
    lead_id: int | None = None,
    assignment: dict[str, Any] | None = None,
    stage_name: str | None = None,
    whatsapp_client: Any | None = None,
) -> dict[str, Any]:
    """Round-robin agent WhatsApp when Beatriz registers a lead/cita (never raises)."""
    from src.notifications.whatsapp_rep import notify_appointment_rep
    from src.odoo_sync.crm import RepAssignment

    appointment = _safe_optional_text(args.appointment_date)
    stage = (stage_name or "").strip() or resolve_beatriz_stage(
        appointment_date=appointment,
        financing=bool(_safe_optional_text(args.financing_summary)),
    )

    pick: RepAssignment | None = None
    if isinstance(assignment, dict) and (
        assignment.get("phone") or assignment.get("odoo_id")
    ):
        pick = RepAssignment(
            branch=str(assignment.get("branch") or branch),
            phone=str(assignment.get("phone") or ""),
            odoo_id=(
                int(assignment["odoo_id"])
                if assignment.get("odoo_id") not in (None, "", False)
                else None
            ),
            rep_name=str(assignment.get("rep_name") or ""),
            fell_back=bool(assignment.get("fell_back")),
            rotation_index=int(assignment.get("rotation_index") or 0),
        )

    try:
        result = notify_appointment_rep(
            customer_name=args.name or "",
            client_phone=args.phone or "",
            branch=branch,
            interested_vehicle=_safe_optional_text(args.interested_vehicle),
            appointment_date=appointment,
            financing_summary=_safe_optional_text(args.financing_summary),
            stage_name=stage,
            lead_id=lead_id,
            assignment=pick,
            whatsapp_client=whatsapp_client,
        )
        logger.warning(
            "dispatch_appointment_rep_alert sent=%s stage=%s rep=%s odoo_id=%s err=%s",
            result.sent,
            stage,
            result.phone,
            result.odoo_id,
            result.error or result.skipped_reason,
        )
        return {**result.as_dict(), "stage_name": stage}
    except Exception as exc:
        logger.exception("dispatch_appointment_rep_alert failed: %s", exc)
        return {"sent": False, "error": str(exc), "stage_name": stage}


def handle_lead_payload(
    payload: dict[str, Any],
    *,
    manager: Any | None = None,
    lead_id: int | None = None,
    background_tasks: BackgroundTasks | None = None,
    whatsapp_client: Any | None = None,
) -> VapiToolResponse:
    context_lead_id = lead_id if lead_id is not None else _extract_context_lead_id(payload)
    calls = extract_typed_tool_calls(
        payload,
        parser=_parse_lead_dict,
        flat_keys=(
            "name",
            "client_name",
            "contact_name",
            "phone",
            "mobile",
            "telefono",
            "interested_vehicle",
            "financing_summary",
            "tradein_summary",
            "appointment_date",
            "lead_id",
            "odoo_lead_id",
        ),
    )
    results: list[VapiToolResult] = []
    for call_id, args in calls:
        if args.lead_id is None and context_lead_id is not None:
            args = args.model_copy(update={"lead_id": context_lead_id})
        try:
            result = create_vapi_lead(args, manager=manager)
            speech = format_lead_speech(
                args,
                dry_run=bool(result.get("dry_run")),
                status=str(result.get("status") or "created"),
            )
            if not result.get("dry_run"):
                branch = str(result.get("branch") or "periferico")
                lead_pk = result.get("lead_id")
                assignment = result.get("assignment")
                stage = str(
                    result.get("stage_name")
                    or resolve_beatriz_stage(appointment_date=args.appointment_date)
                )
                if background_tasks is not None:
                    background_tasks.add_task(
                        dispatch_lead_whatsapp,
                        args,
                        branch=branch,
                        whatsapp_client=whatsapp_client,
                    )
                    background_tasks.add_task(
                        dispatch_appointment_rep_alert,
                        args,
                        branch=branch,
                        lead_id=int(lead_pk) if lead_pk is not None else None,
                        assignment=assignment if isinstance(assignment, dict) else None,
                        stage_name=stage,
                        whatsapp_client=whatsapp_client,
                    )
                else:
                    dispatch_lead_whatsapp(
                        args,
                        branch=branch,
                        whatsapp_client=whatsapp_client,
                    )
                    dispatch_appointment_rep_alert(
                        args,
                        branch=branch,
                        lead_id=int(lead_pk) if lead_pk is not None else None,
                        assignment=assignment if isinstance(assignment, dict) else None,
                        stage_name=stage,
                        whatsapp_client=whatsapp_client,
                    )
        except Exception as exc:
            logger.exception("CRM lead create failed for %s", call_id)
            speech = (
                "No pude registrar la cita en este momento. "
                f"Detalle técnico: {type(exc).__name__}. "
                "¿Me confirmas tu nombre completo y un teléfono de contacto?"
            )
        results.append(VapiToolResult(toolCallId=call_id, result=speech))
    return VapiToolResponse(results=results)


# --- HTTP ------------------------------------------------------------------------


async def _read_json_object(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")
    return payload


@app.get("/health")
@app.get("/")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "vapi-bridge"}


def _safe_request_headers(request: Request) -> dict[str, str]:
    """Loggable headers — strip Authorization / Cookie secrets."""
    redact = {"authorization", "cookie", "x-api-key", "proxy-authorization"}
    out: dict[str, str] = {}
    for key, value in request.headers.items():
        if key.lower() in redact:
            out[key] = "***"
        else:
            out[key] = value
    return out


@app.post("/vapi/inventory")
async def vapi_inventory(request: Request) -> JSONResponse:
    """Live Odoo inventory for Vapi — compact JSON tool result, no background work."""
    payload = await _read_json_object(request)
    logger.info(
        "POST /vapi/inventory headers=%s body=%s",
        _safe_request_headers(request),
        json.dumps(payload, ensure_ascii=False, default=str)[:4000],
    )
    try:
        body = await handle_inventory_payload(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Immediate JSON close — no BackgroundTasks / secondary awaits on this path.
    return JSONResponse(
        content=body.model_dump(),
        media_type="application/json",
        headers={"Connection": "close", "Cache-Control": "no-store"},
    )


@app.post("/vapi/financing", response_model=VapiToolResponse)
async def vapi_financing(
    request: Request,
    background_tasks: BackgroundTasks,
) -> VapiToolResponse:
    """Scotiabank-calibrated French amortization; optional WhatsApp PDF."""
    payload = await _read_json_object(request)
    try:
        return handle_financing_payload(
            payload,
            background_tasks=background_tasks,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/vapi/tradein", response_model=VapiToolResponse)
async def vapi_tradein(request: Request) -> VapiToolResponse:
    """Autométrica Valor Compra estimate (local guide + mileage)."""
    payload = await _read_json_object(request)
    try:
        return handle_tradein_payload(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _vapi_lead_handler(
    request: Request,
    background_tasks: BackgroundTasks,
) -> VapiToolResponse:
    """Create/update Odoo CRM lead; queue customer WhatsApp in the background."""
    payload = await _read_json_object(request)
    query_lead_id = _optional_lead_id(
        request.query_params.get("lead_id")
        or request.query_params.get("odoo_lead_id")
        or request.query_params.get("crm_lead_id")
    )
    try:
        return handle_lead_payload(
            payload,
            lead_id=query_lead_id,
            background_tasks=background_tasks,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/vapi/lead", response_model=VapiToolResponse)
async def vapi_lead(
    request: Request,
    background_tasks: BackgroundTasks,
) -> VapiToolResponse:
    return await _vapi_lead_handler(request, background_tasks)


@app.post("/vapi/crm-lead", response_model=VapiToolResponse)
async def vapi_crm_lead(
    request: Request,
    background_tasks: BackgroundTasks,
) -> VapiToolResponse:
    """Alias of ``/vapi/lead`` — CRM upsert + Evolution WhatsApp confirmation."""
    return await _vapi_lead_handler(request, background_tasks)


@app.post("/vapi/reset-chat")
async def vapi_reset_chat(request: Request) -> dict[str, Any]:
    """Clear HANDOFF / force ``AI_ACTIVE`` for a WhatsApp phone (ops / testing).

    Query: ``?phone=5216...`` (required). Optional ``instance=``, ``clear_appointment=1``.
    Body JSON may also supply ``phone`` / ``instance``.
    """
    payload: dict[str, Any] = {}
    try:
        raw = await request.json()
        if isinstance(raw, dict):
            payload = raw
    except Exception:
        payload = {}

    phone = str(
        request.query_params.get("phone")
        or payload.get("phone")
        or payload.get("whatsapp_phone")
        or ""
    ).strip()
    if not phone:
        raise HTTPException(status_code=400, detail="phone is required")

    instance_raw = request.query_params.get("instance")
    if instance_raw is None:
        instance_raw = payload.get("instance")
    instance = None if instance_raw is None else str(instance_raw)

    clear_raw = (
        request.query_params.get("clear_appointment")
        if "clear_appointment" in request.query_params
        else payload.get("clear_appointment", True)
    )
    if isinstance(clear_raw, str):
        clear_appointment = clear_raw.strip().lower() not in {"0", "false", "no", "off"}
    else:
        clear_appointment = bool(clear_raw)

    from src.whatsapp_worker.inbound import QualificationStore

    store = QualificationStore()
    try:
        updated = store.reset_to_ai_active(
            phone,
            instance=instance,
            clear_appointment=clear_appointment,
            create_if_missing=True,
        )
    finally:
        store.close()

    logger.info(
        "POST /vapi/reset-chat phone=%s instance=%s updated=%s",
        phone,
        instance,
        len(updated),
    )
    return {
        "ok": True,
        "phone": phone,
        "instance": instance,
        "updated": updated,
        "vapi_wa_text_first": (
            os.getenv("VAPI_WA_TEXT_FIRST") or ""
        ).strip().lower()
        in {"1", "true", "yes", "on"},
    }


@app.post("/webhook/whatsapp")
async def whatsapp_webhook_alias(request: Request) -> JSONResponse:
    """Evolution ``MESSAGES_UPSERT`` alias on the bridge port (:8000).

    Prefer the voice gateway on :8080 in production; this route exists so
    ``scripts/disable_evolution_autoreply.py`` can target either port.
    """
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be a JSON object")

    from src.whatsapp_worker.client import WhatsAppWorkerClient
    from src.whatsapp_worker.inbound import QualificationStore
    from src.whatsapp_worker.webhook import handle_inbound_payload

    try:
        from src.odoo_sync.client import OdooCRMClient

        odoo: Any = OdooCRMClient()
    except Exception as exc:  # noqa: BLE001
        logger.warning("odoo client unavailable for WA alias: %s", exc)
        odoo = None

    store = QualificationStore()
    try:
        body = handle_inbound_payload(
            payload,
            store=store,
            odoo=odoo,
            whatsapp=WhatsAppWorkerClient(),
        )
    finally:
        store.close()
    return JSONResponse(status_code=200, content=body)


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("VAPI_BRIDGE_PORT", "8000"))
    host = (os.getenv("VAPI_BRIDGE_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    reload = (os.getenv("VAPI_BRIDGE_RELOAD") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    uvicorn.run(
        "src.voice_gateway.vapi_bridge:app",
        host=host,
        port=port,
        reload=reload,
    )
