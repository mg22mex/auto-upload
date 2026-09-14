"""Vapi outbound dialer — place calls + persist status into sync.db."""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from src.voice_gateway.config import (
    DOCUMENTED_CALLER_E164,
    VapiConfig,
    load_vapi_config,
    sync_db_path,
    voice_outbound_dry_run,
)


class VapiDialerError(RuntimeError):
    """Vapi API or local persistence failure."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_customer_e164(phone: str, *, default_country: str = "52") -> str:
    """Digits → E.164-ish string with leading + (MX 52 default for 10-digit)."""
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        raise VapiDialerError("customer phone is required")
    if len(digits) == 10:
        digits = f"{default_country}{digits}"
    if len(digits) < 11 or len(digits) > 15:
        raise VapiDialerError(f"invalid customer phone length: {digits}")
    return f"+{digits}"


@dataclass(frozen=True)
class OutboundCallRequest:
    customer_phone: str
    lead_id: int | None = None
    vehicle_of_interest: str = ""
    valuation_amount: str = ""
    monthly_payment: str = ""
    branch: str = ""
    client_name: str = ""
    payment_method: str = ""
    agent_script: str = ""

    def variable_values(self) -> dict[str, str]:
        return {
            "vehicle_of_interest": self.vehicle_of_interest or "",
            "valuation_amount": self.valuation_amount or "",
            "monthly_payment": self.monthly_payment or "",
            "lead_id": "" if self.lead_id is None else str(self.lead_id),
            "branch": self.branch or "",
            "client_name": self.client_name or "",
            "payment_method": self.payment_method or "",
            "agent_script": self.agent_script or "",
        }


@dataclass
class OutboundCallResult:
    ok: bool
    dry_run: bool = False
    call_id: str | None = None
    status: str = ""
    customer_number: str = ""
    phone_number_id: str = ""
    caller_e164: str = DOCUMENTED_CALLER_E164
    provider_response: dict[str, Any] | None = None
    error: str | None = None
    db_row_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "provider": "vapi",
            "call_id": self.call_id,
            "status": self.status,
            "customer_number": self.customer_number,
            "phone_number_id": self.phone_number_id,
            "caller_e164": self.caller_e164,
            "provider_response": self.provider_response,
            "error": self.error,
            "db_row_id": self.db_row_id,
        }


class VoiceCallStore:
    """SQLite persistence for outbound voice calls (lives in sync.db)."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS voice_outbound_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        vapi_call_id TEXT,
        lead_id INTEGER,
        phone TEXT NOT NULL,
        vehicle_of_interest TEXT NOT NULL DEFAULT '',
        valuation_amount TEXT NOT NULL DEFAULT '',
        monthly_payment TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'queued',
        appointment_time TEXT NOT NULL DEFAULT '',
        request_human INTEGER NOT NULL DEFAULT 0,
        raw_json TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_voice_outbound_vapi
        ON voice_outbound_calls(vapi_call_id);
    CREATE INDEX IF NOT EXISTS idx_voice_outbound_lead
        ON voice_outbound_calls(lead_id);
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path or sync_db_path())
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(self._SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def insert_outbound(
        self,
        *,
        phone: str,
        lead_id: int | None,
        vehicle_of_interest: str,
        valuation_amount: str,
        monthly_payment: str,
        status: str,
        vapi_call_id: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> int:
        now = _utc_now()
        cur = self._conn.execute(
            """
            INSERT INTO voice_outbound_calls (
                vapi_call_id, lead_id, phone, vehicle_of_interest,
                valuation_amount, monthly_payment, status, raw_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                vapi_call_id or "",
                lead_id,
                phone,
                vehicle_of_interest or "",
                valuation_amount or "",
                monthly_payment or "",
                status,
                json.dumps(raw or {}, ensure_ascii=False),
                now,
                now,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def update_by_call_id(
        self,
        vapi_call_id: str,
        *,
        status: str | None = None,
        appointment_time: str | None = None,
        request_human: bool | None = None,
        raw: dict[str, Any] | None = None,
    ) -> bool:
        row = self._conn.execute(
            "SELECT id, raw_json FROM voice_outbound_calls WHERE vapi_call_id = ? ORDER BY id DESC LIMIT 1",
            (vapi_call_id,),
        ).fetchone()
        if row is None:
            return False
        patches: list[str] = ["updated_at = ?"]
        values: list[Any] = [_utc_now()]
        if status is not None:
            patches.append("status = ?")
            values.append(status)
        if appointment_time is not None:
            patches.append("appointment_time = ?")
            values.append(appointment_time)
        if request_human is not None:
            patches.append("request_human = ?")
            values.append(1 if request_human else 0)
        if raw is not None:
            try:
                prev = json.loads(row["raw_json"] or "{}")
            except json.JSONDecodeError:
                prev = {}
            if not isinstance(prev, dict):
                prev = {}
            prev["status_webhook"] = raw
            patches.append("raw_json = ?")
            values.append(json.dumps(prev, ensure_ascii=False))
        values.append(int(row["id"]))
        self._conn.execute(
            f"UPDATE voice_outbound_calls SET {', '.join(patches)} WHERE id = ?",
            values,
        )
        self._conn.commit()
        return True

    def get_by_call_id(self, vapi_call_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM voice_outbound_calls WHERE vapi_call_id = ? ORDER BY id DESC LIMIT 1",
            (vapi_call_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_latest_for_lead(self, lead_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM voice_outbound_calls WHERE lead_id = ? ORDER BY id DESC LIMIT 1",
            (int(lead_id),),
        ).fetchone()
        return dict(row) if row is not None else None


class VapiDialer:
    """HTTP client for Vapi ``POST /call/phone``."""

    def __init__(
        self,
        config: VapiConfig | None = None,
        *,
        session: requests.Session | None = None,
        timeout_sec: float = 30.0,
        store: VoiceCallStore | None = None,
    ) -> None:
        self.config = config or load_vapi_config()
        self._session = session or requests.Session()
        self.timeout_sec = timeout_sec
        self.store = store

    def place_call(
        self,
        request: OutboundCallRequest,
        *,
        dry_run: bool | None = None,
    ) -> OutboundCallResult:
        customer = normalize_customer_e164(request.customer_phone)
        use_dry = voice_outbound_dry_run() if dry_run is None else bool(dry_run)

        if use_dry:
            row_id = None
            if self.store is not None:
                row_id = self.store.insert_outbound(
                    phone=customer,
                    lead_id=request.lead_id,
                    vehicle_of_interest=request.vehicle_of_interest,
                    valuation_amount=request.valuation_amount,
                    monthly_payment=request.monthly_payment,
                    status="dry_run",
                    raw={"request": request.variable_values()},
                )
            return OutboundCallResult(
                ok=True,
                dry_run=True,
                status="dry_run",
                customer_number=customer,
                phone_number_id=self.config.phone_number_id,
                caller_e164=self.config.caller_e164,
                provider_response={"dry_run": True},
                db_row_id=row_id,
            )

        if not self.config.configured:
            return OutboundCallResult(
                ok=False,
                dry_run=False,
                status="misconfigured",
                customer_number=customer,
                error="VAPI_API_KEY and VAPI_PHONE_NUMBER_ID are required",
            )

        payload: dict[str, Any] = {
            "phoneNumberId": self.config.phone_number_id,
            "customer": {"number": customer},
            "assistantOverrides": {
                "variableValues": request.variable_values(),
            },
        }
        if self.config.assistant_id:
            payload["assistantId"] = self.config.assistant_id
        if request.agent_script:
            payload["assistantOverrides"]["firstMessage"] = request.agent_script

        try:
            resp = self._session.post(
                self.config.call_phone_url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=self.timeout_sec,
            )
        except requests.RequestException as exc:
            return OutboundCallResult(
                ok=False,
                status="transport_error",
                customer_number=customer,
                phone_number_id=self.config.phone_number_id,
                error=str(exc),
            )

        try:
            body = resp.json() if resp.content else {}
        except ValueError:
            body = {"raw": resp.text}

        if resp.status_code >= 400:
            return OutboundCallResult(
                ok=False,
                status="provider_error",
                customer_number=customer,
                phone_number_id=self.config.phone_number_id,
                provider_response=body if isinstance(body, dict) else {"data": body},
                error=f"Vapi HTTP {resp.status_code}",
            )

        data = body if isinstance(body, dict) else {"data": body}
        call_id = str(
            data.get("id") or data.get("callId") or (data.get("call") or {}).get("id") or ""
        ).strip() or None
        status = str(data.get("status") or "queued").strip() or "queued"

        row_id = None
        if self.store is not None:
            row_id = self.store.insert_outbound(
                phone=customer,
                lead_id=request.lead_id,
                vehicle_of_interest=request.vehicle_of_interest,
                valuation_amount=request.valuation_amount,
                monthly_payment=request.monthly_payment,
                status=status,
                vapi_call_id=call_id,
                raw={"request": payload, "response": data},
            )

        return OutboundCallResult(
            ok=True,
            dry_run=False,
            call_id=call_id,
            status=status,
            customer_number=customer,
            phone_number_id=self.config.phone_number_id,
            caller_e164=self.config.caller_e164,
            provider_response=data,
            db_row_id=row_id,
        )


def place_vapi_outbound_call(
    *,
    customer_phone: str,
    lead_id: int | None = None,
    vehicle_of_interest: str = "",
    valuation_amount: str = "",
    monthly_payment: str = "",
    branch: str = "",
    client_name: str = "",
    payment_method: str = "",
    agent_script: str = "",
    dry_run: bool | None = None,
    store: VoiceCallStore | None = None,
    dialer: VapiDialer | None = None,
) -> dict[str, Any]:
    """Convenience wrapper used by the FastAPI outbound-call route."""
    req = OutboundCallRequest(
        customer_phone=customer_phone,
        lead_id=lead_id,
        vehicle_of_interest=vehicle_of_interest,
        valuation_amount=valuation_amount,
        monthly_payment=monthly_payment,
        branch=branch,
        client_name=client_name,
        payment_method=payment_method,
        agent_script=agent_script,
    )
    client = dialer or VapiDialer(store=store if store is not None else VoiceCallStore())
    return client.place_call(req, dry_run=dry_run).as_dict()


def _dig(data: Any, *path: str) -> Any:
    cur = data
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def parse_vapi_call_status(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract appointment / escalation fields from a Vapi end-of-call webhook."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")

    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    call = (
        payload.get("call")
        or message.get("call")
        or payload.get("artifact")
        or {}
    )
    if not isinstance(call, dict):
        call = {}

    call_id = str(
        call.get("id")
        or payload.get("callId")
        or payload.get("id")
        or message.get("callId")
        or ""
    ).strip()

    analysis = (
        call.get("analysis")
        or message.get("analysis")
        or payload.get("analysis")
        or {}
    )
    if not isinstance(analysis, dict):
        analysis = {}
    structured = (
        analysis.get("structuredData")
        or analysis.get("structured_data")
        or message.get("structuredData")
        or payload.get("structuredData")
        or {}
    )
    if not isinstance(structured, dict):
        structured = {}

    customer = call.get("customer") if isinstance(call.get("customer"), dict) else {}
    phone = str(
        customer.get("number")
        or payload.get("phone")
        or payload.get("customer_number")
        or ""
    ).strip()

    vars_map = (
        _dig(call, "assistantOverrides", "variableValues")
        or _dig(call, "assistant", "variableValues")
        or structured
        or {}
    )
    if not isinstance(vars_map, dict):
        vars_map = {}

    lead_raw = (
        structured.get("lead_id")
        or vars_map.get("lead_id")
        or payload.get("lead_id")
    )
    lead_id: int | None
    try:
        lead_id = int(lead_raw) if lead_raw not in (None, "") else None
    except (TypeError, ValueError):
        lead_id = None

    appointment_time = str(
        structured.get("appointment_time")
        or structured.get("preferred_datetime")
        or structured.get("when_text")
        or structured.get("cita")
        or payload.get("appointment_time")
        or ""
    ).strip()

    request_human = bool(
        structured.get("request_human")
        or structured.get("handoff_to_advisor")
        or structured.get("escalate")
        or payload.get("request_human")
    )
    confirmed = bool(
        structured.get("appointment_confirmed")
        or structured.get("confirmed")
        or bool(appointment_time)
    )

    ended_reason = str(
        call.get("endedReason")
        or message.get("endedReason")
        or payload.get("endedReason")
        or ""
    ).strip()
    status = str(
        call.get("status")
        or message.get("status")
        or payload.get("status")
        or ("ended" if ended_reason else "unknown")
    ).strip()

    return {
        "call_id": call_id or None,
        "phone": phone,
        "lead_id": lead_id,
        "appointment_time": appointment_time,
        "appointment_confirmed": confirmed,
        "request_human": request_human,
        "vehicle_of_interest": str(
            structured.get("vehicle_of_interest")
            or vars_map.get("vehicle_of_interest")
            or ""
        ),
        "valuation_amount": str(
            structured.get("valuation_amount")
            or vars_map.get("valuation_amount")
            or ""
        ),
        "monthly_payment": str(
            structured.get("monthly_payment")
            or vars_map.get("monthly_payment")
            or ""
        ),
        "status": status,
        "ended_reason": ended_reason,
        "structured_data": structured,
    }


def handle_vapi_call_status(
    payload: dict[str, Any],
    *,
    store: VoiceCallStore | None = None,
    complete_handoff: bool = True,
    odoo: Any | None = None,
    whatsapp_client: Any | None = None,
) -> dict[str, Any]:
    """Persist Vapi status to sync.db and optionally run appointment handoff."""
    parsed = parse_vapi_call_status(payload)
    owned = store is None
    store = store or VoiceCallStore()

    call_id = parsed.get("call_id")
    if call_id:
        store.update_by_call_id(
            str(call_id),
            status=str(parsed.get("status") or "ended"),
            appointment_time=str(parsed.get("appointment_time") or "") or None,
            request_human=bool(parsed.get("request_human")),
            raw=payload,
        )
        existing = store.get_by_call_id(str(call_id))
        if existing:
            if not parsed.get("phone"):
                parsed["phone"] = existing.get("phone") or ""
            if parsed.get("lead_id") is None and existing.get("lead_id") is not None:
                parsed["lead_id"] = existing.get("lead_id")
            for key in (
                "vehicle_of_interest",
                "valuation_amount",
                "monthly_payment",
            ):
                if not parsed.get(key) and existing.get(key):
                    parsed[key] = existing.get(key)

    result: dict[str, Any] = {
        "status": "recorded",
        "parsed": parsed,
        "handoff_to_advisor": False,
        "sync_db": sync_db_path(),
    }

    should_handoff = bool(
        parsed.get("appointment_confirmed")
        or parsed.get("request_human")
        or parsed.get("appointment_time")
    )
    if complete_handoff and should_handoff and parsed.get("phone"):
        from src.lead_routing import complete_voice_appointment_handoff

        handoff = complete_voice_appointment_handoff(
            lead_id=parsed.get("lead_id"),
            client_phone=str(parsed.get("phone") or ""),
            appointment_time=str(parsed.get("appointment_time") or ""),
            vehicle_of_interest=str(parsed.get("vehicle_of_interest") or ""),
            valuation_amount=str(parsed.get("valuation_amount") or ""),
            monthly_payment=str(parsed.get("monthly_payment") or ""),
            odoo=odoo,
            whatsapp_client=whatsapp_client,
            handoff_to_advisor=True,
            request_human=bool(parsed.get("request_human"))
            and not bool(parsed.get("appointment_confirmed")),
        )
        result["handoff_to_advisor"] = True
        result["appointment_result"] = handoff.as_dict()
        result["status"] = "handoff_complete"

    if owned:
        store.close()
    return result


__all__ = [
    "OutboundCallRequest",
    "OutboundCallResult",
    "VapiDialer",
    "VapiDialerError",
    "VoiceCallStore",
    "handle_vapi_call_status",
    "normalize_customer_e164",
    "parse_vapi_call_status",
    "place_vapi_outbound_call",
]
