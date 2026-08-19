"""Inbound VoIP/SIP call webhook — parse, branch route, TwiML/JSON response."""
from __future__ import annotations

import html
import os
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from src.odoo_sync.crm import (
    PLACEHOLDER_BRANCH,
    PRIMARY_BRANCH,
    load_branch_teams,
    normalize_crm_branch,
    normalize_phone_digits,
    resolve_team_id,
)

ENV_DID_PERIFERICO = "VOICE_DID_PERIFERICO"
ENV_DID_SAN_FELIPE = "VOICE_DID_SAN_FELIPE"
ENV_FORWARD_PERIFERICO = "VOICE_FORWARD_PERIFERICO"
ENV_FORWARD_SAN_FELIPE = "VOICE_FORWARD_SAN_FELIPE"

BRANCH_LABELS = {
    PRIMARY_BRANCH: "Periférico",
    PLACEHOLDER_BRANCH: "San Felipe",
}

_INBOUND_CALLER_KEYS = (
    "From",
    "from",
    "caller_number",
    "caller",
    "caller_phone",
    "Caller",
)
_INBOUND_CALLED_KEYS = (
    "To",
    "to",
    "called_number",
    "called",
    "destination",
    "Called",
)
_CALL_SID_KEYS = ("CallSid", "call_sid", "call_id", "uuid", "CallUUID")
_CALL_STATUS_KEYS = ("CallStatus", "call_status", "status", "disposition")
_DURATION_KEYS = ("CallDuration", "duration_sec", "duration", "Duration")
_BRANCH_KEYS = ("branch", "branch_id", "crm_branch")
_TRUNK_KEYS = ("trunk_id", "trunk", "TrunkSid", "sip_trunk")


@dataclass(frozen=True)
class InboundCallEvent:
    """Normalized inbound call fields from JSON or Twilio-style form posts."""

    caller_phone: str
    called_number: str = ""
    caller_name: str = "Llamada entrante"
    call_sid: str = ""
    call_status: str = ""
    duration_sec: int | None = None
    branch_hint: str = ""
    trunk_id: str = ""
    raw: dict[str, Any] | None = None


def _first_str(raw: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = raw.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _optional_duration(raw: dict[str, Any]) -> int | None:
    text = _first_str(raw, *_DURATION_KEYS)
    if not text:
        return None
    try:
        sec = int(float(text))
    except (TypeError, ValueError):
        return None
    return sec if sec >= 0 else None


def parse_inbound_call_payload(raw: dict[str, Any]) -> InboundCallEvent:
    """Parse provider payload (JSON or form dict) into ``InboundCallEvent``.

    Raises ``ValueError`` when caller phone is missing.
    """
    if not isinstance(raw, dict):
        raise ValueError("payload must be a dict")

    caller = _first_str(raw, *_INBOUND_CALLER_KEYS)
    if not caller:
        raise ValueError("caller_number or From is required")

    called = _first_str(raw, *_INBOUND_CALLED_KEYS)
    name = _first_str(raw, "caller_name", "CallerName", "name") or "Llamada entrante"
    branch_hint = _first_str(raw, *_BRANCH_KEYS)
    trunk = _first_str(raw, *_TRUNK_KEYS)

    return InboundCallEvent(
        caller_phone=caller,
        called_number=called,
        caller_name=name,
        call_sid=_first_str(raw, *_CALL_SID_KEYS),
        call_status=_first_str(raw, *_CALL_STATUS_KEYS),
        duration_sec=_optional_duration(raw),
        branch_hint=branch_hint,
        trunk_id=trunk,
        raw=raw,
    )


async def parse_inbound_call_request(request: Request) -> dict[str, Any]:
    """Read JSON, form-urlencoded, or query params from the inbound request."""
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        body = await request.json()
        if isinstance(body, dict):
            return body
        return {"_raw": body}
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await request.form()
        return {str(k): str(v) for k, v in form.items()}
    try:
        body = await request.json()
        if isinstance(body, dict):
            return body
    except Exception:
        pass
    if request.query_params:
        return dict(request.query_params)
    return {}


def _branch_from_trunk(trunk_id: str) -> str | None:
    text = (trunk_id or "").strip().lower()
    if not text:
        return None
    compact = text.replace(" ", "_").replace("-", "_")
    if "san_felipe" in compact or "sanfelipe" in compact or compact.endswith("_sf"):
        return PLACEHOLDER_BRANCH
    if "periferico" in compact or compact.endswith("_peri"):
        return PRIMARY_BRANCH
    return None


def _branch_from_did(called_number: str) -> str | None:
    digits = normalize_phone_digits(called_number)
    if not digits:
        return None

    periferico_did = normalize_phone_digits(os.getenv(ENV_DID_PERIFERICO) or "")
    san_felipe_did = normalize_phone_digits(os.getenv(ENV_DID_SAN_FELIPE) or "")
    if periferico_did and digits.endswith(periferico_did):
        return PRIMARY_BRANCH
    if san_felipe_did and digits.endswith(san_felipe_did):
        return PLACEHOLDER_BRANCH

    lowered = called_number.lower()
    if "san felipe" in lowered or "san_felipe" in lowered or "sanfelipe" in lowered:
        return PLACEHOLDER_BRANCH
    if "periferico" in lowered or "periférico" in lowered:
        return PRIMARY_BRANCH
    return None


def forward_number_for_branch(branch_key: str) -> str | None:
    """Rep phone / queue for ``<Dial>`` TwiML forwarding."""
    key = normalize_crm_branch(branch_key)
    if key == PLACEHOLDER_BRANCH:
        return (os.getenv(ENV_FORWARD_SAN_FELIPE) or "").strip() or None
    return (os.getenv(ENV_FORWARD_PERIFERICO) or "").strip() or None


def branch_context_for_inbound_call(event: InboundCallEvent) -> dict[str, Any]:
    """Map called number / trunk / branch hint → CRM branch + Odoo team."""
    branch_key: str | None = None

    if event.branch_hint:
        branch_key = normalize_crm_branch(event.branch_hint)
    if branch_key is None and event.trunk_id:
        branch_key = _branch_from_trunk(event.trunk_id)
    if branch_key is None and event.called_number:
        branch_key = _branch_from_did(event.called_number)
    if branch_key is None:
        branch_key = PRIMARY_BRANCH

    teams = load_branch_teams()
    effective_branch, team_id, fell_back = resolve_team_id(branch_key, teams=teams)
    ctx: dict[str, Any] = {
        "branch": branch_key,
        "crm_branch_effective": effective_branch,
        "physical_location": BRANCH_LABELS.get(branch_key, "Periférico"),
        "branch_fell_back": fell_back,
        "forward_to": forward_number_for_branch(branch_key),
    }
    if team_id is not None:
        ctx["branch_id"] = team_id
    return ctx


def wants_twiml_response(request: Request, raw: dict[str, Any]) -> bool:
    """Twilio and SIP gateways usually expect XML; JSON when explicitly requested."""
    fmt = (request.query_params.get("format") or raw.get("format") or "").strip().lower()
    if fmt in {"json"}:
        return False
    if fmt in {"twiml", "xml"}:
        return True
    accept = (request.headers.get("accept") or "").lower()
    if "application/json" in accept and "xml" not in accept:
        return False
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        return True
    if "xml" in accept or "text/xml" in accept:
        return True
    return fmt == "" and "application/json" not in accept


def build_twiml_dial(*, forward_to: str | None, branch_label: str) -> str:
    """TwiML ``Response`` with optional ``Dial`` to branch rep."""
    greeting = (
        f"Gracias por llamar a Autosell {branch_label}. "
        "Lo transferimos con un asesor."
    )
    safe_greeting = html.escape(greeting, quote=True)
    if forward_to:
        safe_number = html.escape(forward_to.strip(), quote=True)
        dial = f'  <Dial timeout="30">{safe_number}</Dial>\n'
    else:
        dial = ""
        safe_greeting = html.escape(
            greeting + " Un asesor le contactará en breve.",
            quote=True,
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        f'  <Say language="es-MX">{safe_greeting}</Say>\n'
        f"{dial}"
        "</Response>\n"
    )


def build_inbound_call_response(
    request: Request,
    *,
    ctx: dict[str, Any],
    crm_result: dict[str, Any],
    raw: dict[str, Any] | None = None,
) -> Response:
    """Return TwiML (default for form/SIP) or JSON summary."""
    branch_label = str(ctx.get("physical_location") or "Periférico")
    forward_to = ctx.get("forward_to")

    if wants_twiml_response(request, raw or {}):
        body = build_twiml_dial(forward_to=forward_to, branch_label=branch_label)
        return Response(content=body, media_type="application/xml")

    payload: dict[str, Any] = {
        "status": "ok",
        "confirmation": "inbound call logged",
        "lead_id": crm_result.get("lead_id"),
        "activity_id": crm_result.get("activity_id"),
        "branch": ctx.get("branch"),
        "branch_id": ctx.get("branch_id") or crm_result.get("team_id"),
        "forward_to": forward_to,
        "call_status": crm_result.get("call_status"),
        "error": None,
    }
    return JSONResponse(status_code=200, content=payload)


__all__ = [
    "InboundCallEvent",
    "branch_context_for_inbound_call",
    "build_inbound_call_response",
    "build_twiml_dial",
    "forward_number_for_branch",
    "parse_inbound_call_payload",
    "parse_inbound_call_request",
    "wants_twiml_response",
]
