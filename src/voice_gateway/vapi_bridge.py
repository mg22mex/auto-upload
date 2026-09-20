"""Vapi tool bridge — inventory, financing, trade-in, and CRM lead capture.

Riley tool calls hit::

    POST /vapi/inventory
    POST /vapi/financing
    POST /vapi/tradein
    POST /vapi/lead
    POST /vapi/crm-lead   (alias of /vapi/lead)

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
from pydantic import BaseModel, Field

_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env")

logger = logging.getLogger(__name__)

DEFAULT_ODOO_URL = "https://autosellmx.odoo.com"
DEFAULT_ODOO_DB = "autosellmx"
RESULT_LIMIT = 3
INVENTORY_TIMEOUT_SEC = 1.5
INVENTORY_TIMEOUT_SPEECH = (
    "El inventario tardó un momento. ¿Agendamos una prueba de manejo "
    "o te envío opciones por WhatsApp?"
)
DEFAULT_TERM_MONTHS = 48
LEAD_TITLE_PREFIX = "Llamada Paulina - "
VAPI_LEAD_CHANNEL = "Voice"

app = FastAPI(
    title="Autosell Vapi Bridge",
    version="1.2.0",
    description="Inventory / financing / trade-in / CRM lead tools for Vapi Riley",
)

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
    return FinancingArgs.model_validate(
        {
            "vehicle_price": price,
            "term_months": term,
            "down_payment": down,
            "net_trade_in_equity": equity,
        }
    )


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
    )


def _search_inventory_blocking(args: InventoryArgs) -> list[dict[str, Any]]:
    """Full connect + search for thread offload (keeps event loop free)."""
    return search_inventory(args)


async def _search_inventory_cached(args: InventoryArgs) -> list[dict[str, Any]]:
    """Serve TTL cache hits inline; otherwise Odoo RPC in a worker thread."""
    from src.odoo_sync.inventory import RESULT_LIMIT as INV_LIMIT
    from src.odoo_sync.inventory import cache_get, cache_key

    key = cache_key(
        brand=args.brand,
        model=args.model,
        max_price=float(args.max_price) if args.max_price is not None else None,
        year=args.year,
        limit=INV_LIMIT,
        available_only=True,
    )
    cached = cache_get(key)
    if cached is not None:
        logger.debug("inventory cache hit key=%s rows=%s", key[:3], len(cached))
        return cached
    return await asyncio.to_thread(_search_inventory_blocking, args)


async def handle_inventory_payload(payload: dict[str, Any]) -> VapiToolResponse:
    calls = extract_tool_calls(payload)
    results: list[VapiToolResult] = []
    for call_id, args in calls:
        try:
            rows = await asyncio.wait_for(
                _search_inventory_cached(args),
                timeout=INVENTORY_TIMEOUT_SEC,
            )
            speech = format_inventory_speech(rows, args)
        except asyncio.TimeoutError:
            logger.warning(
                "inventory search timed out after %.1fs for %s — returning fallback",
                INVENTORY_TIMEOUT_SEC,
                call_id,
            )
            speech = INVENTORY_TIMEOUT_SPEECH
        except Exception as exc:
            logger.exception("inventory search failed for %s", call_id)
            speech = INVENTORY_TIMEOUT_SPEECH
            logger.warning(
                "inventory error %s for %s — using timeout fallback speech",
                type(exc).__name__,
                call_id,
            )
        results.append(VapiToolResult(toolCallId=call_id, result=speech))
    return VapiToolResponse(results=results)


# --- Financing -------------------------------------------------------------------


def format_financing_speech(args: FinancingArgs, quote: Any) -> str:
    down = format_price_voice_es(quote.down_payment)
    months = format_months_voice_es(quote.term_months)
    monthly = format_price_voice_es(quote.estimated_monthly_payment)
    return (
        f"Con un enganche de {down} a {months}, tu mensualidad estimada "
        f"con Scotiabank sería de {monthly}. "
        "¿Te interesa que te enviemos la cotización formal por WhatsApp?"
    )


def run_financing_quote(args: FinancingArgs) -> Any:
    from src.quote_engine.engine import CalibratedQuoteEngine

    # Scotiabank path requires term_months % 12 == 0
    term = int(args.term_months)
    if term % 12 != 0:
        term = max(12, round(term / 12) * 12)

    engine = CalibratedQuoteEngine()
    return engine.calculate(
        args.vehicle_price,
        term,
        down_payment=args.down_payment,
        net_trade_in_equity=args.net_trade_in_equity,
    )


def handle_financing_payload(payload: dict[str, Any]) -> VapiToolResponse:
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
        ),
    )
    results: list[VapiToolResult] = []
    for call_id, args in calls:
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
        "stage_name": "Cita/Prueba de manejo",
        "assign_round_robin": True,
        "preserve_salesperson": True,
    }
    if physical_label:
        payload["physical_location"] = physical_label
    if args.lead_id is not None:
        payload["lead_id"] = int(args.lead_id)
    logger.warning(
        "crm-lead branch=%s physical_location=%s vehicle=%s",
        branch_key,
        physical_label,
        vehicle[:80],
    )
    result = crm.create_or_update_lead(payload, branch=branch_key)
    return {**result, "opportunity_name": title}


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
                if background_tasks is not None:
                    background_tasks.add_task(
                        dispatch_lead_whatsapp,
                        args,
                        branch=branch,
                        whatsapp_client=whatsapp_client,
                    )
                else:
                    dispatch_lead_whatsapp(
                        args,
                        branch=branch,
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


@app.post("/vapi/inventory", response_model=VapiToolResponse)
async def vapi_inventory(request: Request) -> VapiToolResponse:
    payload = await _read_json_object(request)
    try:
        return await handle_inventory_payload(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/vapi/financing", response_model=VapiToolResponse)
async def vapi_financing(request: Request) -> VapiToolResponse:
    """Scotiabank-calibrated French amortization for Riley."""
    payload = await _read_json_object(request)
    try:
        return handle_financing_payload(payload)
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
