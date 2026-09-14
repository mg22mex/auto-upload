"""AI-first routing for ``MG Quote Lead`` — human only on appointment intent.

Leads tagged ``MG Quote Lead`` stay with the WhatsApp AI responder at stage
``Primer contacto``. Round-robin assignment and the rep WhatsApp alert fire
only when the customer asks for an in-person visit / test drive
(``Cita/Prueba de manejo``).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from src.config import branch_label
from src.odoo_sync.client import OdooCRMClient
from src.odoo_sync.crm import RepAssignment, assign_lead_owner, normalize_crm_branch

MG_QUOTE_LEAD_TAG = "MG Quote Lead"
STAGE_PRIMER_CONTACTO = "Primer contacto"
STAGE_CITA = "Cita/Prueba de manejo"

AGENT_AI = "ai_whatsapp"
AGENT_HUMAN = "human_rep"

ENV_AI_MG_QUOTE = "AI_MG_QUOTE_LEADS"


@dataclass(frozen=True)
class AppointmentIntent:
    """Parsed appointment / test-drive request from free text."""

    requested: bool
    kind: str = "cita"  # cita | prueba_manejo | inspeccion
    when_text: str = ""
    raw: str = ""


@dataclass
class LeadRoutingDecision:
    """How an inbound lead / turn should be handled."""

    agent: str
    stage_name: str
    assign_human: bool
    tags: list[str] = field(default_factory=list)
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "stage_name": self.stage_name,
            "assign_human": self.assign_human,
            "tags": list(self.tags),
            "reason": self.reason,
        }


@dataclass
class AppointmentHandoffResult:
    lead_id: int | None
    stage_name: str
    assignment: RepAssignment | None
    rep_notification: dict[str, Any] | None
    stage_updated: bool = False
    advisor_assigned: bool = False
    handoff_to_advisor: bool = True
    channel_alerts: dict[str, Any] | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "lead_id": self.lead_id,
            "stage_name": self.stage_name,
            "stage_updated": self.stage_updated,
            "advisor_assigned": self.advisor_assigned,
            "handoff_to_advisor": self.handoff_to_advisor,
            "assignment": self.assignment.as_dict() if self.assignment else None,
            "rep_notification": self.rep_notification,
            "channel_alerts": self.channel_alerts,
            "error": self.error,
        }


_APPOINTMENT_PATTERNS = (
    r"\bcita\b",
    r"\bagendar\b",
    r"\bagenda\b",
    r"\bagendemos\b",
    r"\bvisita\b",
    r"\bvisitar\b",
    r"\bsucursal\b",
    r"prueba\s+de\s+manejo",
    r"\btest\s*drive\b",
    r"\bmanejo\b",
    r"\binspecci[oó]n\b",
    r"\brevisar\s+(el\s+)?auto\b",
    r"\bver\s+(el\s+)?auto\b",
    r"\bpasar\s+(por|a)\b",
    r"\bir\s+a\s+(la\s+)?sucursal\b",
    r"\bcuando\s+(puedo|podemos)\s+(ir|pasar|visitar)\b",
)

_WHEN_RE = re.compile(
    r"(?P<when>"
    r"ma[nñ]ana|"
    r"hoy|"
    r"pasado\s+ma[nñ]ana|"
    r"el\s+\w+|"
    r"este\s+\w+|"
    r"la\s+pr[oó]xima\s+semana|"
    r"\d{1,2}[:.]\d{2}\s*(?:am|pm|hrs?|horas?)?|"
    r"\d{1,2}\s*(?:am|pm|hrs?|horas?)|"
    r"a\s+las\s+\d{1,2}(?::\d{2})?"
    r")",
    re.IGNORECASE,
)


def ai_mg_quote_enabled() -> bool:
    raw = (os.getenv(ENV_AI_MG_QUOTE) or "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def normalize_tag_name(value: Any) -> str:
    if isinstance(value, (list, tuple)) and value:
        # Odoo many2many often returns [id, name]
        value = value[-1]
    return str(value or "").strip()


def extract_tags(payload: dict[str, Any] | None) -> list[str]:
    """Collect tag display names from an Odoo lead / webhook payload."""
    if not isinstance(payload, dict):
        return []
    tags: list[str] = []
    for key in ("tag_names", "tags", "crm_tags"):
        raw = payload.get(key)
        if isinstance(raw, str) and raw.strip():
            tags.extend(part.strip() for part in raw.split(",") if part.strip())
        elif isinstance(raw, (list, tuple)):
            for item in raw:
                if isinstance(item, dict):
                    name = normalize_tag_name(item.get("name") or item.get("display_name"))
                else:
                    name = normalize_tag_name(item)
                if name and not name.isdigit():
                    tags.append(name)
    tag_ids = payload.get("tag_ids")
    if isinstance(tag_ids, (list, tuple)):
        for item in tag_ids:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                name = normalize_tag_name(item[1])
                if name and not name.isdigit():
                    tags.append(name)
            elif isinstance(item, dict):
                name = normalize_tag_name(item.get("name"))
                if name:
                    tags.append(name)
    # Deduplicate, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for tag in tags:
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
    return out


def has_mg_quote_tag(tags: list[str] | None = None, *, payload: dict[str, Any] | None = None) -> bool:
    names = list(tags or [])
    if payload is not None:
        names.extend(extract_tags(payload))
    needle = MG_QUOTE_LEAD_TAG.casefold()
    return any(normalize_tag_name(t).casefold() == needle for t in names)


def should_defer_human_assignment(
    tags: list[str] | None = None,
    *,
    payload: dict[str, Any] | None = None,
) -> bool:
    """True when MG Quote Lead AI policy owns the lead (no immediate RR)."""
    if not ai_mg_quote_enabled():
        return False
    if has_mg_quote_tag(tags, payload=payload):
        return True
    # Soft-capture / WhatsApp quote path often tags after create; honor explicit flag.
    if isinstance(payload, dict) and payload.get("defer_advisor"):
        return True
    if isinstance(payload, dict) and payload.get("handling_agent") == AGENT_AI:
        return True
    return False


def route_inbound_lead(
    payload: dict[str, Any] | None = None,
    *,
    tags: list[str] | None = None,
    appointment: AppointmentIntent | None = None,
) -> LeadRoutingDecision:
    """Decide agent + stage for an inbound lead / turn."""
    tag_list = list(tags or [])
    if payload is not None:
        tag_list = extract_tags(payload) or tag_list
    if not tag_list and ai_mg_quote_enabled():
        # WhatsApp / quote pipeline always stamps MG Quote Lead on create.
        tag_list = [MG_QUOTE_LEAD_TAG]

    if appointment and appointment.requested:
        return LeadRoutingDecision(
            agent=AGENT_HUMAN,
            stage_name=STAGE_CITA,
            assign_human=True,
            tags=tag_list,
            reason="appointment_requested",
        )

    if should_defer_human_assignment(tag_list, payload=payload):
        return LeadRoutingDecision(
            agent=AGENT_AI,
            stage_name=STAGE_PRIMER_CONTACTO,
            assign_human=False,
            tags=tag_list,
            reason="mg_quote_lead_ai",
        )

    return LeadRoutingDecision(
        agent=AGENT_HUMAN,
        stage_name=STAGE_PRIMER_CONTACTO,
        assign_human=True,
        tags=tag_list,
        reason="default_human",
    )


def detect_appointment_intent(text: str) -> AppointmentIntent:
    """Detect in-person visit / test-drive / inspection intent."""
    raw = (text or "").strip()
    if not raw:
        return AppointmentIntent(requested=False, raw=raw)
    lowered = raw.casefold()
    matched = False
    kind = "cita"
    for pattern in _APPOINTMENT_PATTERNS:
        if re.search(pattern, lowered, re.IGNORECASE):
            matched = True
            if "prueba" in pattern or "manejo" in pattern or "test" in pattern:
                kind = "prueba_manejo"
            elif "inspec" in pattern or "revisar" in pattern:
                kind = "inspeccion"
            break
    if not matched:
        return AppointmentIntent(requested=False, raw=raw)

    when = ""
    match = _WHEN_RE.search(raw)
    if match:
        when = match.group("when").strip()
    return AppointmentIntent(requested=True, kind=kind, when_text=when, raw=raw)


def format_ai_reply(
    *,
    name: str = "",
    text: str = "",
    vehicle_interest: str = "",
    branch_name: str = "",
    appointment: AppointmentIntent | None = None,
) -> str:
    """Rule-based WhatsApp AI reply (financing / requirements / vehicle / CTA)."""
    who = (name or "Cliente").strip() or "Cliente"
    branch = (branch_name or "Periférico").strip()
    interest = (vehicle_interest or text or "").strip()
    lowered = (text or "").casefold()

    if appointment and appointment.requested:
        when = appointment.when_text or "el horario que prefieras"
        return (
            f"¡Perfecto, {who}! Agendamos tu "
            f"{'prueba de manejo' if appointment.kind == 'prueba_manejo' else 'cita'} "
            f"en Autosell {branch} ({when}).\n\n"
            "Un asesor de la sucursal te confirmará en breve por WhatsApp. 🙌"
        )

    if any(token in lowered for token in ("requisito", "documento", "papeles", "ine", "comprobante")):
        return (
            "Para financiamiento usualmente pedimos: identificación oficial (INE), "
            "comprobante de domicilio reciente y comprobante de ingresos. "
            "Para compra de contado basta identificación y datos de facturación.\n\n"
            f"¿Te interesa ver un vehículo en Autosell {branch}? "
            "Puedo agendar *cita o prueba de manejo* cuando gustes."
        )

    if detect_forma_pago_permuta(text or "") or detect_forma_pago_permuta(interest):
        return (
            f"Perfecto, {who}. Para *Forma de pago: Auto a cambio (trade-in)* necesito "
            "los datos de tu auto a cuenta: año, marca, modelo, *versión* y "
            "*kilometraje*. Con Autométrica calculamos el *Valor Compra* y lo "
            "aplicamos como enganche en la cotización a financiamiento.\n\n"
            "Ejemplo: `Toyota Corolla 2020 LE 85,000 km`"
        )

    if any(token in lowered for token in ("financ", "crédito", "credito", "enganche", "mensual")):
        return (
            f"Con gusto te ayudo con el financiamiento, {who}. "
            "En Autosell cotizamos a plazos (12–60 meses) con enganche flexible "
            "y opción de auto a cambio.\n\n"
            f"{'Sobre tu interés: ' + interest + chr(10) + chr(10) if interest else ''}"
            "¿Quieres que te prepare una cotización estimada, o prefieres "
            "agendar una *cita / prueba de manejo* en sucursal?"
        )

    if interest:
        return (
            f"¡Hola {who}! Gracias por escribir a Autosell {branch}. "
            f'Recibimos tu mensaje sobre: "{interest}".\n\n'
            "Puedo ayudarte con:\n"
            "• Precio y disponibilidad\n"
            "• Financiamiento / enganche\n"
            "• Requisitos y documentación\n"
            "• Agendar *cita o prueba de manejo* en sucursal\n\n"
            "¿Qué te gustaría saber primero?"
        )

    return (
        f"¡Hola {who}! Soy el asistente de Autosell {branch}. "
        "Puedo orientarte sobre vehículos, financiamiento y requisitos. "
        "Cuando quieras visitar la sucursal, pide una *cita o prueba de manejo* "
        "y te conecto con un asesor."
    )


# --- Autométrica trade-in / Auto a cambio qualification --------------------------

PAYMENT_LABEL_AUTO_A_CAMBIO = "Auto a cambio (trade-in)"
PAYMENT_LABEL_PERMUTA = PAYMENT_LABEL_AUTO_A_CAMBIO  # backward-compatible alias

PAYMENT_CASH = "cash"
PAYMENT_FINANCING = "financing"
PAYMENT_TRADE_IN = "trade_in"
PAYMENT_FINANCING_TRADE_IN = "financing_trade_in"

_TRADE_IN_TOKENS = (
    "auto a cambio",
    "a cambio",
    "permuta",
    "trade-in",
    "trade in",
    "tradein",
    "auto a cuenta",
    "a cuenta",
    "cambio de auto",
    "entregar mi",
    "forma de pago: permuta",
    "forma de pago: auto a cambio",
)

_FINANCING_TOKENS = (
    "financiamiento",
    "financiar",
    "financiado",
    "crédito",
    "credito",
    "mensualidades",
)

_COMBINED_NUM_RE = re.compile(
    r"(?<!\d)([123])\s*(?:y|e|,|/|&|\+|con)\s*([123])(?!\d)",
    re.IGNORECASE,
)
_COMBINED_WORDS_RE = re.compile(
    r"(?:financ\w*.{0,40}(?:a\s+cambio|auto\s+a\s+cambio|permuta|trade[\s\-]?in))"
    r"|(?:(?:a\s+cambio|auto\s+a\s+cambio|permuta|trade[\s\-]?in).{0,40}financ\w*)",
    re.IGNORECASE,
)

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_KM_RE = re.compile(
    r"(?P<km>\d{1,3}(?:[,\s]\d{3})*|\d+)\s*(?:km|kms|kil[oó]metros?)\b",
    re.IGNORECASE,
)
_KM_BARE_RE = re.compile(r"\b(?P<km>\d{5,6})\b")
_VERSION_HINTS = (
    "base",
    "le",
    "xle",
    "se",
    "sense",
    "advance",
    "comfortline",
    "highline",
    "i sport",
    "isport",
    "touring",
    "sport",
    "premium",
    "exclusive",
)


@dataclass(frozen=True)
class PaymentIntent:
    """Parsed forma de pago — supports financing + Auto a cambio together."""

    cash: bool = False
    financing: bool = False
    trade_in: bool = False
    raw: str = ""

    @property
    def is_combined_financing_trade_in(self) -> bool:
        return self.financing and self.trade_in

    @property
    def session_key(self) -> str | None:
        if self.cash and not self.financing and not self.trade_in:
            return PAYMENT_CASH
        if self.financing and self.trade_in:
            return PAYMENT_FINANCING_TRADE_IN
        if self.trade_in:
            return PAYMENT_TRADE_IN
        if self.financing:
            return PAYMENT_FINANCING
        if self.cash:
            return PAYMENT_CASH
        return None


@dataclass
class TradeInDetails:
    """Parsed trade-in vehicle fields for Autométrica Valor Compra."""

    year: int | None = None
    make: str = ""
    model: str = ""
    version: str = ""
    mileage_km: int | None = None
    is_trade_in: bool = False
    wants_financing: bool = False
    raw: str = ""

    @property
    def is_permuta(self) -> bool:
        """Alias kept for older callers."""
        return self.is_trade_in

    @is_permuta.setter
    def is_permuta(self, value: bool) -> None:
        self.is_trade_in = bool(value)

    def missing_fields(self) -> list[str]:
        missing: list[str] = []
        if not self.year:
            missing.append("Año")
        if not self.make:
            missing.append("Marca")
        if not self.model:
            missing.append("Modelo")
        if not (self.version or "").strip():
            missing.append("Versión")
        if self.mileage_km is None:
            missing.append("Kilometraje")
        return missing

    def as_label(self) -> str:
        bits = [str(self.year or ""), self.make, self.model, self.version]
        label = " ".join(b for b in bits if b).strip()
        if self.mileage_km is not None:
            label = f"{label} ({self.mileage_km:,} km)"
        return label.strip()


def _mentions_trade_in(lowered: str) -> bool:
    if not lowered:
        return False
    if "forma de pago" in lowered and (
        "permuta" in lowered or "a cambio" in lowered or "trade" in lowered
    ):
        return True
    return any(token in lowered for token in _TRADE_IN_TOKENS)


def _mentions_financing(lowered: str) -> bool:
    if not lowered:
        return False
    if any(token in lowered for token in _FINANCING_TOKENS):
        return True
    return "financ" in lowered or "crédit" in lowered or "credit" in lowered


def _mentions_cash(lowered: str) -> bool:
    return any(
        token in lowered
        for token in ("contado", "efectivo", "de contado", "al contado", "cash")
    )


def parse_payment_intent(text: str) -> PaymentIntent:
    """Detect cash / financing / Auto a cambio, including combined replies."""
    raw = (text or "").strip()
    lowered = raw.casefold()
    if not lowered:
        return PaymentIntent(raw=raw)

    cash = False
    financing = False
    trade_in = False

    # Numeric combos: "2 y 3", "3/2", "2+3"
    for match in _COMBINED_NUM_RE.finditer(lowered):
        nums = {match.group(1), match.group(2)}
        if "1" in nums:
            cash = True
        if "2" in nums:
            financing = True
        if "3" in nums:
            trade_in = True

    if _COMBINED_WORDS_RE.search(raw):
        financing = True
        trade_in = True

    # Exact / short replies
    if lowered in {"1", "contado", "efectivo", "cash", "de contado", "al contado"}:
        cash = True
    if lowered in {
        "2",
        "financiamiento",
        "financiar",
        "credito",
        "crédito",
        "mensualidades",
        "financiado",
    }:
        financing = True
    if lowered in {
        "3",
        "permuta",
        "trade",
        "trade-in",
        "trade in",
        "cambio",
        "a cambio",
        "auto a cambio",
        "entregar mi auto",
        "entregar auto",
    }:
        trade_in = True

    if not (cash or financing or trade_in):
        if _mentions_cash(lowered):
            cash = True
        if _mentions_financing(lowered):
            financing = True
        if _mentions_trade_in(lowered):
            trade_in = True

    # Phrase-level boost for combined wording without regex hit
    if _mentions_financing(lowered) and _mentions_trade_in(lowered):
        financing = True
        trade_in = True

    return PaymentIntent(
        cash=cash,
        financing=financing,
        trade_in=trade_in,
        raw=raw,
    )


def detect_forma_pago_permuta(text: str) -> bool:
    """True when customer selected / mentioned Auto a cambio (trade-in)."""
    return parse_payment_intent(text).trade_in


def detect_forma_pago_financing(text: str) -> bool:
    return parse_payment_intent(text).financing


def _extract_mileage_km(text: str) -> int | None:
    match = _KM_RE.search(text or "")
    if match:
        digits = re.sub(r"\D", "", match.group("km"))
        return int(digits) if digits else None
    # Bare 5–6 digit figure when version already present (e.g. "LE 85000")
    # Skip 4-digit values — those are almost always model years.
    bare = _KM_BARE_RE.search(text or "")
    if bare:
        km = int(bare.group("km"))
        if 1900 <= km <= 2100:
            return None
        return km
    return None


def _extract_version(text: str) -> str:
    lowered = (text or "").casefold()
    for hint in sorted(_VERSION_HINTS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(hint)}\b", lowered):
            return hint.upper() if len(hint) <= 3 else hint.title()
    # Explicit "versión X" / "version X"
    m = re.search(r"versi[oó]n\s*[:\-]?\s*([A-Za-z0-9][\w\s\-]{0,24})", text or "", re.I)
    if m:
        return m.group(1).strip()
    return ""


def parse_trade_in_details(
    text: str,
    *,
    prior: TradeInDetails | None = None,
) -> TradeInDetails:
    """Merge free-text vehicle clues into TradeInDetails (year/make/model/ver/km)."""
    base = TradeInDetails(
        year=prior.year if prior else None,
        make=prior.make if prior else "",
        model=prior.model if prior else "",
        version=prior.version if prior else "",
        mileage_km=prior.mileage_km if prior else None,
        is_trade_in=bool(prior.is_trade_in) if prior else False,
        wants_financing=bool(prior.wants_financing) if prior else False,
        raw=(prior.raw if prior else "") or "",
    )
    raw = (text or "").strip()
    if not raw:
        return base
    combined = f"{base.raw} {raw}".strip()
    base.raw = combined
    intent = parse_payment_intent(raw)
    intent_combined = parse_payment_intent(combined)
    if intent.trade_in or intent_combined.trade_in:
        base.is_trade_in = True
    if intent.financing or intent_combined.financing:
        base.wants_financing = True

    year_m = _YEAR_RE.search(raw)
    if year_m:
        base.year = int(year_m.group(0))

    km = _extract_mileage_km(raw)
    if km is not None:
        base.mileage_km = km

    ver = _extract_version(raw)
    if ver:
        base.version = ver

    # Common "Make Model" after year / Auto a cambio:
    make_model = re.search(
        r"(?:trade[\s\-]?in|permuta|auto\s+a\s+cambio|a\s+cambio|entregar(?:[ií]a)?|cambio)?\s*[:\-]?\s*"
        r"(?:\b(?:19|20)\d{2}\b\s+)?"
        r"(?P<make>toyota|nissan|mazda|volkswagen|vw|honda|ford|chevrolet|kia|hyundai|mg|bmw|mercedes|audi)\s+"
        r"(?P<model>[A-Za-z0-9][\w\-]*(?:\s+[A-Za-z0-9][\w\-]*){0,2})",
        raw,
        re.IGNORECASE,
    )
    if make_model:
        make = make_model.group("make").strip()
        model = make_model.group("model").strip()
        # Strip trailing version tokens / year from model
        model_bits = []
        for part in model.split():
            if _YEAR_RE.fullmatch(part):
                if base.year is None:
                    base.year = int(part)
                continue
            if part.casefold() in _VERSION_HINTS or part.casefold() in {"km", "kms"}:
                if not base.version and part.casefold() in _VERSION_HINTS:
                    base.version = part.upper() if len(part) <= 3 else part.title()
                break
            model_bits.append(part)
        base.make = make.title() if make.casefold() != "vw" else "Volkswagen"
        if model_bits:
            base.model = " ".join(model_bits).title()

    return base


def prompt_missing_trade_in_fields(details: TradeInDetails) -> str:
    """Ask for Versión / Kilometraje (and any other missing identity fields)."""
    missing = details.missing_fields()
    known = details.as_label() or "tu auto a cambio"
    focus = []
    if "Versión" in missing:
        focus.append("*Versión* (ej. Base, LE, Sense)")
    if "Kilometraje" in missing:
        focus.append("*Kilometraje* en km")
    for field in missing:
        if field not in {"Versión", "Kilometraje"}:
            focus.append(f"*{field}*")
    ask = " y ".join(focus) if focus else "Versión y Kilometraje"
    financing_note = (
        " Lo aplicamos como *enganche* sobre el financiamiento del vehículo nuevo."
        if details.wants_financing
        else " Con eso calculo el *Valor Compra* y lo aplico como enganche en la cotización."
    )
    return (
        f"Para valuar tu *auto a cambio* ({known}) con Autométrica necesito {ask}.\n\n"
        "Ejemplo: `LE 85,000 km`\n\n"
        f"{financing_note.strip()}"
    )


def build_trade_in_quote_message(
    *,
    details: TradeInDetails,
    lead_name: str = "",
    vehicle_interest: str = "",
    vehicle_price: int | float | str | None = None,
    term_months: int = 36,
) -> tuple[str, dict[str, Any]]:
    """Lookup Autométrica Valor Compra → French amortization → WhatsApp quote text."""
    from src.quote_engine.autometrica import lookup_valor_compra
    from src.quote_engine.calculator import calculate_quote
    from src.whatsapp_worker.client import QUOTE_DISCLAIMER, format_quote_message

    if details.year is None or not details.make or not details.model:
        raise ValueError("trade-in year/make/model required before quote")

    valuation = lookup_valor_compra(
        year=int(details.year),
        make=details.make,
        model=details.model,
        version=details.version or "",
        mileage_km=int(details.mileage_km or 0),
    )
    price_raw = vehicle_price
    if price_raw is None:
        price_raw = os.getenv("AI_QUOTE_DEFAULT_PRICE") or "450000"
    price = int(str(price_raw).replace(",", "").split(".")[0])
    quote = calculate_quote(
        price,
        int(term_months),
        net_trade_in_equity=valuation.valor_compra,
    )
    interest = (vehicle_interest or "vehículo de interés").strip()
    vehicle_name = "vehículo Autosell"
    for token in ("camioneta", "pickup", "sedán", "sedan", "suv", "hatchback"):
        if token in interest.casefold():
            vehicle_name = "SUV" if token == "suv" else token.capitalize()
            break
    else:
        short = interest.split(".")[0].strip()
        if short and len(short) <= 48 and not short.casefold().startswith("hola"):
            vehicle_name = short
    text = format_quote_message(lead_name or "Cliente", vehicle_name, quote)
    meta = {
        "valor_compra": str(valuation.valor_compra),
        "valor_venta": str(valuation.valor_venta),
        "mileage_adjustment": str(valuation.mileage_adjustment),
        "matched": valuation.matched,
        "source": valuation.source,
        "trade_in_label": details.as_label(),
        "vehicle_of_interest": vehicle_name,
        "valuation_amount": str(valuation.valor_compra),
        "monthly_payment": str(quote.estimated_monthly_payment),
        "down_payment": str(quote.down_payment),
        "financed_principal": str(quote.financed_principal),
        "estimated_monthly_payment": str(quote.estimated_monthly_payment),
        "disclaimer_present": QUOTE_DISCLAIMER in text,
        "financing": True,
        "trade_in": True,
        "payment_method": (
            PAYMENT_FINANCING_TRADE_IN
            if details.wants_financing
            else PAYMENT_TRADE_IN
        ),
    }
    enganche_note = (
        "Ese *Valor Compra* se aplica como *enganche* en el financiamiento "
        "(amortización francesa del saldo restante):\n\n"
        if details.wants_financing
        else "Ese monto se aplica como *enganche* (auto a cambio) en la amortización francesa:\n\n"
    )
    preface = (
        f"Valuación Autométrica (*Valor Compra*): ${valuation.valor_compra:,.2f}\n"
        f"Ajuste por km: ${valuation.mileage_adjustment:,.2f} "
        f"(base {valuation.baseline_km:,} km).\n"
        f"{enganche_note}"
    )
    return preface + text, meta


def advance_trade_in_qualification(
    text: str,
    *,
    prior: TradeInDetails | None = None,
    lead_name: str = "",
    vehicle_interest: str = "",
    vehicle_price: int | float | str | None = None,
    term_months: int = 36,
) -> tuple[TradeInDetails, str | None, dict[str, Any] | None]:
    """Parse turn → ask missing Año/Marca/Modelo/Versión/km or return quote.

    Returns ``(details, reply_text, quote_meta)``. ``reply_text`` is None when
    this turn is not a trade-in path.
    """
    details = parse_trade_in_details(text, prior=prior)
    intent = parse_payment_intent(text)
    if intent.trade_in:
        details.is_trade_in = True
    if intent.financing:
        details.wants_financing = True
    if not details.is_trade_in and not (prior and prior.is_trade_in):
        return details, None, None
    details.is_trade_in = True
    # Combined financing + auto a cambio always runs French amortization with
    # Valor Compra as enganche once vehicle identity is complete.
    if details.wants_financing or (prior and prior.wants_financing):
        details.wants_financing = True
    missing = details.missing_fields()
    if missing:
        return details, prompt_missing_trade_in_fields(details), None
    message, meta = build_trade_in_quote_message(
        details=details,
        lead_name=lead_name,
        vehicle_interest=vehicle_interest,
        vehicle_price=vehicle_price,
        term_months=term_months,
    )
    return details, message, meta


def update_lead_stage(
    odoo: Any,
    lead_id: int,
    stage_name: str,
) -> bool:
    """Best-effort stage write; returns True when a stage_id was applied."""
    stage_id = odoo._resolve_crm_stage_id(stage_name)
    if stage_id is None:
        print(f"WARN lead_routing: stage {stage_name!r} not found for lead {lead_id}", flush=True)
        return False
    odoo.execute_kw("crm.lead", "write", [[int(lead_id)], {"stage_id": int(stage_id)}])
    return True


# --- WhatsApp quote → outbound AI voice → advisor handoff ----------------------

ENV_VOICE_OUTBOUND = "VOICE_OUTBOUND_ENABLED"
ENV_VOICE_OUTBOUND_URL = "VOICE_OUTBOUND_URL"
ENV_VOICE_OUTBOUND_DRY = "VOICE_OUTBOUND_DRY_RUN"
DEFAULT_VOICE_OUTBOUND_PATH = "/api/v1/voice/outbound-call"


@dataclass(frozen=True)
class QuoteVoiceContext:
    """Context passed to the outbound voice agent after a WhatsApp quote."""

    lead_id: int | None
    phone: str
    vehicle_of_interest: str
    valuation_amount: str
    monthly_payment: str
    branch: str = ""
    client_name: str = ""
    payment_method: str = ""
    trade_in_label: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "lead_id": self.lead_id,
            "phone": self.phone,
            "vehicle_of_interest": self.vehicle_of_interest,
            "valuation_amount": self.valuation_amount,
            "monthly_payment": self.monthly_payment,
            "branch": self.branch,
            "client_name": self.client_name,
            "payment_method": self.payment_method,
            "trade_in_label": self.trade_in_label,
        }


def voice_outbound_enabled() -> bool:
    raw = (os.getenv(ENV_VOICE_OUTBOUND) or "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def voice_outbound_dry_run() -> bool:
    raw = (os.getenv(ENV_VOICE_OUTBOUND_DRY) or "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def build_voice_agent_script(context: QuoteVoiceContext) -> str:
    """Opening script for the outbound AI voice agent after WhatsApp quote."""
    vehicle = (context.vehicle_of_interest or "vehículo").strip()
    return (
        f"Hola! Te acabamos de enviar la cotización de tu {vehicle} por WhatsApp. "
        "¿Te gustaría agendar una cita hoy o mañana para ver el auto y hacer "
        "la valuación física de tu auto a cambio?"
    )


def build_advisor_handoff_summary(
    *,
    client_name: str = "",
    client_phone: str = "",
    vehicle_of_interest: str = "",
    valuation_amount: str = "",
    monthly_payment: str = "",
    appointment_time: str = "",
    branch_name: str = "",
    payment_method: str = "",
    lead_id: int | None = None,
) -> str:
    """Compiled summary for WhatsApp / Slack / Telegram advisor channels."""
    from src.notifications.whatsapp_rep import odoo_lead_url, payment_label

    lines = [
        "🎯 *Handoff a asesor — paquete completo*",
        f"👤 *Cliente:* {client_name or client_phone or 'n/d'}",
        f"📞 *Tel:* {client_phone or 'n/d'}",
        f"🚘 *Vehículo objetivo:* {vehicle_of_interest or 'Por confirmar'}",
        f"💵 *Valor auto a cambio:* {valuation_amount or 'n/d'}",
        f"📅 *Mensualidad estimada:* {monthly_payment or 'n/d'}",
        f"💳 *Modalidad:* {payment_label(payment_method)}",
        f"📍 *Sucursal:* {branch_name or 'Periférico'}",
    ]
    if appointment_time:
        lines.append(f"🗓️ *Cita:* {appointment_time}")
    lines.append(f"🔗 *Odoo Lead:* {odoo_lead_url(lead_id) or 'n/d'}")
    lines.append("handoff_to_advisor=True")
    return "\n".join(lines)


def queue_outbound_voice_call(
    context: QuoteVoiceContext,
    *,
    dry_run: bool | None = None,
    http_post: Any | None = None,
) -> dict[str, Any]:
    """Queue POST ``/api/v1/voice/outbound-call`` with quote context.

    Best-effort: never raises. Dry-run (default) returns the payload without
    dialing so local tests and CI stay offline-safe.
    """
    script = build_voice_agent_script(context)
    payload = {
        **context.as_dict(),
        "agent_script": script,
        "goal": "capture_appointment_datetime",
        "instructions": (
            "Referencia la cotización recién enviada por WhatsApp. "
            "Captura día/hora preferidos para cita y valuación física del auto a cambio. "
            "Si el cliente pide un humano o confirma la cita, marca handoff_to_advisor."
        ),
    }
    if not voice_outbound_enabled():
        return {
            "queued": False,
            "skipped_reason": f"{ENV_VOICE_OUTBOUND}=false",
            "payload": payload,
            "agent_script": script,
        }

    use_dry = voice_outbound_dry_run() if dry_run is None else bool(dry_run)
    if use_dry:
        return {
            "queued": True,
            "dry_run": True,
            "endpoint": DEFAULT_VOICE_OUTBOUND_PATH,
            "payload": payload,
            "agent_script": script,
        }

    url = (os.getenv(ENV_VOICE_OUTBOUND_URL) or "").strip()
    if not url:
        base = (os.getenv("VOICE_GATEWAY_BASE_URL") or "http://127.0.0.1:8080").rstrip("/")
        url = f"{base}{DEFAULT_VOICE_OUTBOUND_PATH}"

    post = http_post
    if post is None:
        import urllib.request

        def post(target: str, body: dict[str, Any]) -> dict[str, Any]:
            req = urllib.request.Request(
                target,
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8")
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                parsed = {"raw": raw}
            return parsed if isinstance(parsed, dict) else {"ok": True, "data": parsed}

    try:
        response = post(url, payload)
        return {
            "queued": True,
            "dry_run": False,
            "endpoint": url,
            "payload": payload,
            "agent_script": script,
            "provider_response": response,
        }
    except Exception as exc:
        print(f"WARN voice outbound queue failed: {type(exc).__name__}: {exc}", flush=True)
        return {
            "queued": False,
            "dry_run": False,
            "endpoint": url,
            "payload": payload,
            "agent_script": script,
            "error": str(exc),
        }


def handle_outbound_voice_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Server-side handler for ``POST /api/v1/voice/outbound-call``."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    ctx = QuoteVoiceContext(
        lead_id=int(payload["lead_id"]) if payload.get("lead_id") not in (None, "") else None,
        phone=str(payload.get("phone") or "").strip(),
        vehicle_of_interest=str(
            payload.get("vehicle_of_interest") or payload.get("vehicle_interest") or ""
        ).strip(),
        valuation_amount=str(
            payload.get("valuation_amount") or payload.get("valor_compra") or ""
        ).strip(),
        monthly_payment=str(
            payload.get("monthly_payment") or payload.get("estimated_monthly_payment") or ""
        ).strip(),
        branch=str(payload.get("branch") or "").strip(),
        client_name=str(payload.get("client_name") or payload.get("name") or "").strip(),
        payment_method=str(payload.get("payment_method") or "").strip(),
        trade_in_label=str(payload.get("trade_in_label") or "").strip(),
    )
    if not ctx.phone:
        raise ValueError("phone is required")
    script = payload.get("agent_script") or build_voice_agent_script(ctx)
    return {
        "status": "queued",
        "handoff_to_advisor": False,
        "lead_id": ctx.lead_id,
        "phone": ctx.phone,
        "vehicle_of_interest": ctx.vehicle_of_interest,
        "valuation_amount": ctx.valuation_amount,
        "monthly_payment": ctx.monthly_payment,
        "agent_script": script,
        "context": ctx.as_dict(),
    }


def complete_voice_appointment_handoff(
    *,
    lead_id: int | None,
    client_phone: str,
    appointment_time: str = "",
    vehicle_of_interest: str = "",
    valuation_amount: str = "",
    monthly_payment: str = "",
    payment_method: str | None = None,
    branch: str | None = None,
    client_name: str = "",
    odoo: Any | None = None,
    whatsapp_client: Any | None = None,
    handoff_to_advisor: bool = True,
    request_human: bool = False,
) -> AppointmentHandoffResult:
    """Lock appointment from voice (or human request) and notify advisor channels."""
    when = (appointment_time or "").strip()
    if request_human and not when:
        when = "cliente solicitó asesor"
    intent = AppointmentIntent(
        requested=True,
        kind="cita",
        when_text=when,
        raw=when or "voice_appointment",
    )
    interest = vehicle_of_interest
    if valuation_amount:
        interest = f"{interest} | Auto a cambio valuado: {valuation_amount}".strip(" |")
    if monthly_payment:
        interest = f"{interest} | Mensualidad: {monthly_payment}".strip(" |")

    result = handoff_appointment_to_rep(
        lead_id=lead_id,
        client_phone=client_phone,
        branch=branch,
        vehicle_interest=interest,
        payment_method=payment_method,
        appointment=intent,
        client_name=client_name,
        odoo=odoo,
        whatsapp_client=whatsapp_client,
        valuation_amount=valuation_amount,
        monthly_payment=monthly_payment,
        vehicle_of_interest=vehicle_of_interest,
    )
    result.handoff_to_advisor = bool(handoff_to_advisor)

    summary = build_advisor_handoff_summary(
        client_name=client_name,
        client_phone=client_phone,
        vehicle_of_interest=vehicle_of_interest or interest,
        valuation_amount=valuation_amount,
        monthly_payment=monthly_payment,
        appointment_time=when,
        branch_name=branch_label(normalize_crm_branch(branch)),
        payment_method=payment_method or "",
        lead_id=lead_id,
    )
    channel_alerts: dict[str, Any] = {"summary": summary}
    try:
        from src.alerts import send_alert

        alert = send_alert(summary, subject="Handoff asesor — cita confirmada")
        channel_alerts["alerts"] = {
            "sent": alert.sent,
            "failed": alert.failed,
            "skipped_reason": alert.skipped_reason,
        }
    except Exception as exc:
        channel_alerts["alerts"] = {"error": str(exc)}
    result.channel_alerts = channel_alerts
    if result.rep_notification is not None:
        result.rep_notification = {
            **result.rep_notification,
            "handoff_to_advisor": True,
            "summary": summary,
        }
    return result


def handle_voice_appointment_result(payload: dict[str, Any]) -> dict[str, Any]:
    """Server-side handler for voice appointment confirmation / human request."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    confirmed = bool(payload.get("appointment_confirmed") or payload.get("confirmed"))
    request_human = bool(payload.get("request_human") or payload.get("handoff_to_advisor"))
    when = str(
        payload.get("appointment_time")
        or payload.get("when_text")
        or payload.get("preferred_datetime")
        or ""
    ).strip()
    if not (confirmed or request_human or when):
        return {
            "status": "ignored",
            "handoff_to_advisor": False,
            "reason": "no appointment confirmation or human request",
        }
    result = complete_voice_appointment_handoff(
        lead_id=int(payload["lead_id"]) if payload.get("lead_id") not in (None, "") else None,
        client_phone=str(payload.get("phone") or payload.get("client_phone") or ""),
        appointment_time=when,
        vehicle_of_interest=str(
            payload.get("vehicle_of_interest") or payload.get("vehicle_interest") or ""
        ),
        valuation_amount=str(
            payload.get("valuation_amount") or payload.get("valor_compra") or ""
        ),
        monthly_payment=str(
            payload.get("monthly_payment") or payload.get("estimated_monthly_payment") or ""
        ),
        payment_method=str(payload.get("payment_method") or "") or None,
        branch=str(payload.get("branch") or "") or None,
        client_name=str(payload.get("client_name") or payload.get("name") or ""),
        handoff_to_advisor=True,
        request_human=request_human and not confirmed,
    )
    body = result.as_dict()
    body["status"] = "handoff_complete" if result.handoff_to_advisor else "ok"
    return body


def handoff_appointment_to_rep(
    *,
    lead_id: int | None,
    client_phone: str,
    branch: str | None = None,
    vehicle_interest: str = "",
    payment_method: str | None = None,
    appointment: AppointmentIntent | None = None,
    client_name: str = "",
    odoo: Any | None = None,
    whatsapp_client: Any | None = None,
    assigner_assignment: RepAssignment | None = None,
    valuation_amount: str = "",
    monthly_payment: str = "",
    vehicle_of_interest: str = "",
) -> AppointmentHandoffResult:
    """Move lead to Cita stage, round-robin a rep, and WhatsApp the alert."""
    from src.notifications.whatsapp_rep import notify_rep

    branch_key = normalize_crm_branch(branch)
    assignment = assigner_assignment or assign_lead_owner(branch_key)
    stage_updated = False
    advisor_assigned = False
    error: str | None = None

    client = odoo
    if client is None and lead_id:
        try:
            client = OdooCRMClient()
            client.authenticate()
        except Exception as exc:
            error = f"odoo init failed: {exc}"
            client = None

    if client is not None and lead_id:
        try:
            stage_updated = update_lead_stage(client, int(lead_id), STAGE_CITA)
            if assignment.odoo_id:
                try:
                    client.assign_lead_advisor(int(lead_id), int(assignment.odoo_id))
                    advisor_assigned = True
                except Exception as exc:
                    print(
                        f"WARN lead_routing: assign advisor failed lead={lead_id}: {exc}",
                        flush=True,
                    )
            note_bits = [
                "--- AI appointment handoff ---",
                f"Cliente: {client_name or client_phone}",
                f"Sucursal: {branch_label(branch_key)}",
                "handoff_to_advisor=True",
            ]
            if appointment and appointment.when_text:
                note_bits.append(f"Horario solicitado: {appointment.when_text}")
            if appointment and appointment.kind:
                note_bits.append(f"Tipo: {appointment.kind}")
            if vehicle_of_interest or vehicle_interest:
                note_bits.append(
                    f"Interés: {vehicle_of_interest or vehicle_interest}"
                )
            if valuation_amount:
                note_bits.append(f"Valor auto a cambio: {valuation_amount}")
            if monthly_payment:
                note_bits.append(f"Mensualidad estimada: {monthly_payment}")
            try:
                client.post_quote_to_chatter(int(lead_id), "\n".join(note_bits))
            except Exception:
                pass
        except Exception as exc:
            error = str(exc)

    when = (appointment.when_text if appointment else "") or ""
    notice = notify_rep(
        client_phone=client_phone,
        branch=branch_key,
        vehicle_interest=vehicle_of_interest or vehicle_interest,
        payment_method=payment_method,
        lead_id=lead_id,
        assignment=assignment,
        whatsapp_client=whatsapp_client,
        appointment_time=when or None,
        valuation_amount=valuation_amount or None,
        monthly_payment=monthly_payment or None,
    )

    return AppointmentHandoffResult(
        lead_id=lead_id,
        stage_name=STAGE_CITA,
        assignment=assignment,
        rep_notification=notice.as_dict(),
        stage_updated=stage_updated,
        advisor_assigned=advisor_assigned,
        handoff_to_advisor=True,
        error=error,
    )


__all__ = [
    "AGENT_AI",
    "AGENT_HUMAN",
    "AppointmentHandoffResult",
    "AppointmentIntent",
    "DEFAULT_VOICE_OUTBOUND_PATH",
    "ENV_AI_MG_QUOTE",
    "ENV_VOICE_OUTBOUND",
    "ENV_VOICE_OUTBOUND_DRY",
    "ENV_VOICE_OUTBOUND_URL",
    "LeadRoutingDecision",
    "MG_QUOTE_LEAD_TAG",
    "PAYMENT_CASH",
    "PAYMENT_FINANCING",
    "PAYMENT_FINANCING_TRADE_IN",
    "PAYMENT_LABEL_AUTO_A_CAMBIO",
    "PAYMENT_LABEL_PERMUTA",
    "PAYMENT_TRADE_IN",
    "PaymentIntent",
    "QuoteVoiceContext",
    "STAGE_CITA",
    "STAGE_PRIMER_CONTACTO",
    "TradeInDetails",
    "advance_trade_in_qualification",
    "ai_mg_quote_enabled",
    "build_advisor_handoff_summary",
    "build_trade_in_quote_message",
    "build_voice_agent_script",
    "complete_voice_appointment_handoff",
    "detect_appointment_intent",
    "detect_forma_pago_financing",
    "detect_forma_pago_permuta",
    "extract_tags",
    "format_ai_reply",
    "handle_outbound_voice_request",
    "handle_voice_appointment_result",
    "handoff_appointment_to_rep",
    "has_mg_quote_tag",
    "parse_payment_intent",
    "parse_trade_in_details",
    "prompt_missing_trade_in_fields",
    "queue_outbound_voice_call",
    "route_inbound_lead",
    "should_defer_human_assignment",
    "update_lead_stage",
    "voice_outbound_dry_run",
    "voice_outbound_enabled",
]
