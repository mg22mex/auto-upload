"""Voice / STT intent extraction for the FastAPI voice gateway."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any


VOICE_CHANNEL = "Voice / Phone"

TRANSFER_PROMPT_ES = (
    "Disculpe, no logré entender bien el audio. "
    "En unos momentos lo transfiero con un asesor de Autosell MX "
    "para continuar con su cotización."
)

GENERIC_CAPTURE_PROMPT_ES = (
    "Gracias por su llamada. Registramos sus datos y un asesor "
    "de Autosell MX le contactará en breve para completar la cotización."
)

_TERM_RE = re.compile(
    r"(?:plazo|meses|a)\s*(?:de\s*)?(\d{1,2})\s*(?:meses|mes)?",
    re.IGNORECASE,
)
_BUDGET_RE = re.compile(
    r"(?:presupuesto|mensual(?:idad)?|puedo\s+pagar|hasta)\s*"
    r"(?:de\s*|unos?\s*)?(?:\$|mxn)?\s*([\d,\.]+)",
    re.IGNORECASE,
)
_ENGANCHE_RE = re.compile(
    r"(?:enganche|down(?:\s*payment)?|anticipo)\s*"
    r"(?:de\s*)?(?:\$|mxn)?\s*([\d,\.]+)",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"\b(20[0-2]\d|19\d{2})\b")


@dataclass
class VoiceIntent:
    """Normalized caller intent for quote + CRM."""

    ok: bool
    mode: str  # quote | transfer | generic_capture
    caller_name: str = ""
    caller_phone: str = ""
    vehicle_name: str = ""
    vehicle_price: Decimal | None = None
    term_months: int = 36
    down_payment: Decimal | None = None
    budget_monthly: Decimal | None = None
    sku: str = ""
    branch_id: int | None = None
    trade_in: dict[str, Any] | None = None
    stt_confidence: float | None = None
    audio_degraded: bool = False
    transcript: str = ""
    tts_fallback: str = ""
    errors: list[str] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)

    def to_lead_data(self) -> dict[str, Any]:
        lead: dict[str, Any] = {
            "name": self.caller_name,
            "phone": self.caller_phone,
            "vehicle_name": self.vehicle_name or "Consulta telefónica",
            "term_months": self.term_months,
            "channel": VOICE_CHANNEL,
            "generate_pdf": self.mode == "quote",
        }
        if self.vehicle_price is not None:
            lead["vehicle_price"] = self.vehicle_price
        if self.down_payment is not None:
            lead["down_payment"] = self.down_payment
        if self.branch_id is not None:
            lead["branch_id"] = self.branch_id
        if self.sku:
            lead["sku"] = self.sku
            lead["autosell_id"] = self.sku
        if self.trade_in:
            lead["trade_in"] = self.trade_in
        if self.budget_monthly is not None:
            lead["budget_monthly"] = self.budget_monthly
        lead.update(self.extras)
        return lead


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        cleaned = str(value).replace(",", "").replace(" ", "").strip()
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


def is_audio_degraded(payload: dict[str, Any]) -> bool:
    """True when STT/audio quality is too poor to trust structured fields."""
    status = str(
        payload.get("audio_status")
        or payload.get("stt_status")
        or payload.get("audio_quality")
        or ""
    ).strip().lower()
    if status in {
        "failed",
        "error",
        "degraded",
        "poor",
        "low",
        "unintelligible",
        "timeout",
    }:
        return True
    if payload.get("stt_failed") is True or payload.get("audio_failed") is True:
        return True
    conf = payload.get("stt_confidence")
    if conf is not None:
        try:
            if float(conf) < float(payload.get("stt_min_confidence") or 0.45):
                return True
        except (TypeError, ValueError):
            pass
    return False


def extract_intent_from_transcript(transcript: str) -> dict[str, Any]:
    """Best-effort Spanish STT → vehicle / budget / term / enganche hints."""
    text = (transcript or "").strip()
    out: dict[str, Any] = {"transcript": text}
    if not text:
        return out

    term_m = _TERM_RE.search(text)
    if term_m:
        months = int(term_m.group(1))
        if 6 <= months <= 72:
            out["term_months"] = months

    eng_m = _ENGANCHE_RE.search(text)
    if eng_m:
        out["down_payment"] = _to_decimal(eng_m.group(1))

    bud_m = _BUDGET_RE.search(text)
    if bud_m:
        out["budget_monthly"] = _to_decimal(bud_m.group(1))

    year_m = _YEAR_RE.search(text)
    # Heuristic: take a chunk that looks like "Marca Modelo Año"
    vehicle_hint = None
    for pat in (
        r"(?:interesad[oa]\s+en|busco|quiero|cotizar|sobre)\s+(.+?)(?:\.|,|$)",
        r"(mazda|nissan|toyota|honda|vw|volkswagen|chevrolet|ford|kia|hyundai|"
        r"jeep|bmw|mercedes|audi|seat|renault|mitsubishi)\s+[\w\-]+(?:\s+\d{4})?",
    ):
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            vehicle_hint = m.group(0 if m.lastindex is None else 1).strip()
            break
    if vehicle_hint:
        # Strip leading verb phrases if captured by first pattern group
        vehicle_hint = re.sub(
            r"^(?:interesad[oa]\s+en|busco|quiero|cotizar|sobre)\s+",
            "",
            vehicle_hint,
            flags=re.IGNORECASE,
        ).strip(" .")
        if year_m and year_m.group(1) not in vehicle_hint:
            vehicle_hint = f"{vehicle_hint} {year_m.group(1)}".strip()
        out["vehicle_name"] = vehicle_hint[:120]

    price_ctx = re.search(
        r"(?:precio|cuesta|vale)\s*(?:de\s*)?(?:\$|mxn)?\s*([\d,\.]+)",
        text,
        re.IGNORECASE,
    )
    if price_ctx:
        out["vehicle_price"] = _to_decimal(price_ctx.group(1))

    return out


def parse_voice_intent(payload: dict[str, Any]) -> VoiceIntent:
    """Parse Voice AI / STT JSON into a VoiceIntent for the sales pipeline."""
    if not isinstance(payload, dict):
        return VoiceIntent(
            ok=False,
            mode="transfer",
            tts_fallback=TRANSFER_PROMPT_ES,
            errors=["payload must be a JSON object"],
        )

    transcript = str(
        payload.get("transcript")
        or payload.get("stt_text")
        or payload.get("text")
        or payload.get("utterance")
        or ""
    ).strip()
    hints = extract_intent_from_transcript(transcript) if transcript else {}
    degraded = is_audio_degraded(payload)

    phone = str(
        payload.get("caller_phone") or payload.get("phone") or ""
    ).strip()
    name = str(
        payload.get("caller_name") or payload.get("name") or ""
    ).strip()

    interest = payload.get("vehicle_interest")
    vehicle_name = ""
    vehicle_price = _to_decimal(payload.get("vehicle_price"))
    sku = str(payload.get("sku") or payload.get("autosell_id") or "").strip()

    if isinstance(interest, dict):
        vehicle_name = str(
            interest.get("name")
            or interest.get("vehicle_name")
            or interest.get("title")
            or ""
        ).strip()
        if vehicle_price is None:
            vehicle_price = _to_decimal(
                interest.get("price") or interest.get("vehicle_price")
            )
        if not sku:
            sku = str(
                interest.get("sku") or interest.get("autosell_id") or ""
            ).strip()
    else:
        vehicle_name = str(
            interest or payload.get("vehicle_name") or ""
        ).strip()

    if not vehicle_name and hints.get("vehicle_name"):
        vehicle_name = str(hints["vehicle_name"])
    if vehicle_price is None and hints.get("vehicle_price") is not None:
        vehicle_price = hints["vehicle_price"]

    term_raw = payload.get("term") or payload.get("term_months") or hints.get(
        "term_months"
    )
    try:
        term_months = int(term_raw) if term_raw is not None else 36
    except (TypeError, ValueError):
        term_months = 36

    down = _to_decimal(payload.get("down_payment"))
    if down is None and hints.get("down_payment") is not None:
        down = hints["down_payment"]

    budget = _to_decimal(payload.get("budget_monthly") or payload.get("budget"))
    if budget is None and hints.get("budget_monthly") is not None:
        budget = hints["budget_monthly"]

    branch_raw = payload.get("branch_id")
    branch_id: int | None
    try:
        branch_id = int(branch_raw) if branch_raw not in (None, "") else None
    except (TypeError, ValueError):
        branch_id = None

    trade_raw = payload.get("trade_in_info") or payload.get("trade_in")
    trade_in: dict[str, Any] | None = None
    if isinstance(trade_raw, dict) and trade_raw:
        try:
            trade_in = {
                "year": int(trade_raw["year"]),
                "make": str(trade_raw["make"]),
                "model": str(trade_raw["model"]),
                "version": str(trade_raw.get("version") or ""),
                "mileage_km": int(trade_raw.get("mileage_km") or 0),
                "vin": str(trade_raw.get("vin") or ""),
                "outstanding_lien": trade_raw.get("outstanding_lien") or 0,
                "condition_adjustment": trade_raw.get("condition_adjustment") or 0,
            }
            if trade_raw.get("manual_guide_value") is not None:
                trade_in["manual_guide_value"] = trade_raw["manual_guide_value"]
        except (KeyError, TypeError, ValueError):
            trade_in = None

    test_drive_raw = (
        payload.get("test_drive")
        or payload.get("test_drive_info")
        or payload.get("appointment")
    )
    test_drive: dict[str, Any] | None = None
    if isinstance(test_drive_raw, dict) and (
        test_drive_raw.get("start")
        or test_drive_raw.get("start_datetime")
        or test_drive_raw.get("datetime")
    ):
        start = (
            test_drive_raw.get("start")
            or test_drive_raw.get("start_datetime")
            or test_drive_raw.get("datetime")
        )
        test_drive = {
            "start": start,
            "stop": test_drive_raw.get("stop") or test_drive_raw.get("end"),
            "vehicle_model": str(
                test_drive_raw.get("vehicle_model")
                or test_drive_raw.get("vehicle")
                or vehicle_name
                or ""
            ).strip(),
            "duration_hours": test_drive_raw.get("duration_hours") or 1.0,
        }

    conf_raw = payload.get("stt_confidence")
    try:
        stt_confidence = float(conf_raw) if conf_raw is not None else None
    except (TypeError, ValueError):
        stt_confidence = None

    extras: dict[str, Any] = {}
    for key in (
        "annual_auto_insurance",
        "include_certificate_renewal",
        "enforce_min_down",
        "include_additional_coverages",
        "year",
        "make",
        "model",
        "vin",
        "mileage_km",
        "transmission",
        "features",
    ):
        if key in payload and payload[key] is not None:
            extras[key] = payload[key]
    if test_drive is not None:
        extras["test_drive"] = test_drive
    if isinstance(interest, dict):
        for key in (
            "year",
            "make",
            "model",
            "vin",
            "mileage_km",
            "transmission",
            "features",
        ):
            if key in interest and key not in extras:
                extras[key] = interest[key]

    errors: list[str] = []
    if not phone:
        errors.append("caller_phone is required")
    if not name:
        errors.append("caller_name is required")

    # Degraded audio with incomplete quote fields → human transfer / soft capture.
    if degraded and (not vehicle_name or vehicle_price is None):
        mode = "generic_capture" if phone and name else "transfer"
        return VoiceIntent(
            ok=bool(phone and name),
            mode=mode,
            caller_name=name or "Cliente telefónico",
            caller_phone=phone,
            vehicle_name=vehicle_name or "Consulta telefónica (audio incompleto)",
            vehicle_price=vehicle_price,
            term_months=term_months,
            down_payment=down,
            budget_monthly=budget,
            sku=sku,
            branch_id=branch_id,
            trade_in=trade_in,
            stt_confidence=stt_confidence,
            audio_degraded=True,
            transcript=transcript,
            tts_fallback=(
                GENERIC_CAPTURE_PROMPT_ES if mode == "generic_capture" else TRANSFER_PROMPT_ES
            ),
            errors=errors
            or ["degraded audio / failed STT — fallback capture"],
            extras={
                **extras,
                "generate_pdf": False,
                "dispatch_whatsapp": False,
                "soft_capture": True,
            },
        )

    if not vehicle_name:
        errors.append("vehicle_interest is required")

    can_quote = bool(phone and name and vehicle_name)
    if not can_quote:
        return VoiceIntent(
            ok=False,
            mode="transfer",
            caller_name=name,
            caller_phone=phone,
            vehicle_name=vehicle_name,
            vehicle_price=vehicle_price,
            term_months=term_months,
            down_payment=down,
            budget_monthly=budget,
            sku=sku,
            branch_id=branch_id,
            trade_in=trade_in,
            stt_confidence=stt_confidence,
            audio_degraded=degraded,
            transcript=transcript,
            tts_fallback=TRANSFER_PROMPT_ES,
            errors=errors or ["insufficient caller data"],
            extras=extras,
        )

    return VoiceIntent(
        ok=True,
        mode="quote",
        caller_name=name,
        caller_phone=phone,
        vehicle_name=vehicle_name,
        vehicle_price=vehicle_price,
        term_months=term_months,
        down_payment=down,
        budget_monthly=budget,
        sku=sku,
        branch_id=branch_id,
        trade_in=trade_in,
        stt_confidence=stt_confidence,
        audio_degraded=degraded,
        transcript=transcript,
        extras=extras,
        errors=errors,
    )


def format_tts_quote(
    *,
    name: str,
    vehicle_name: str,
    monthly: Any,
    down_payment: Any = None,
    term_months: int | None = None,
) -> str:
    """Low-latency Spanish TTS script for a successful quote."""
    try:
        monthly_txt = f"${Decimal(str(monthly)):,.2f}"
    except Exception:
        monthly_txt = str(monthly)
    parts = [
        f"Hola {name}.",
        f"Para el {vehicle_name},",
    ]
    if down_payment is not None:
        try:
            down_txt = f"${Decimal(str(down_payment)):,.2f}"
        except Exception:
            down_txt = str(down_payment)
        parts.append(f"con enganche de {down_txt},")
    if term_months:
        parts.append(f"a {term_months} meses,")
    parts.append(f"la mensualidad estimada es de {monthly_txt} pesos.")
    parts.append(
        "Le enviamos la ficha PDF a su expediente y un asesor le dará seguimiento."
    )
    return " ".join(parts)
