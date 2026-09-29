"""Booked-appointment sync → res.partner + crm.lead + calendar.event.

Marco (setter) creates the booking; Round-Robin closer owns the opportunity.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from src.config import BRANCH_LABELS, normalize_rep_phone
from src.odoo_sync.client import OdooCRMClient
from src.odoo_sync.crm import (
    CRMLeadManager,
    assign_lead_owner,
    normalize_crm_branch,
    resolve_team_id,
)
from src.odoo_sync.structure import (
    closer_odoo_ids_from_mapping,
    load_mapping,
    setter_user_id_from_mapping,
)

MX_TZ = ZoneInfo("America/Chihuahua")


@dataclass
class AppointmentSyncResult:
    partner_id: int | None = None
    lead_id: int | None = None
    event_id: int | None = None
    team_id: int | None = None
    closer_user_id: int | None = None
    setter_user_id: int | None = None
    branch: str = ""
    when_iso: str = ""
    dry_run: bool = False
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _digits(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def ensure_customer_partner(
    client: OdooCRMClient,
    *,
    name: str,
    phone: str,
    dry_run: bool = False,
) -> int | None:
    """Search ``res.partner`` by phone; create when missing."""
    phone_norm = normalize_rep_phone(phone) or (phone or "").strip()
    digits = _digits(phone_norm)
    tail = digits[-10:] if len(digits) >= 10 else digits
    if not dry_run and tail:
        rows = client.execute_kw(
            "res.partner",
            "search_read",
            [[["phone", "ilike", tail]]],
            {"fields": ["id", "name", "phone"], "limit": 1},
        )
        if rows:
            return int(rows[0]["id"])
    if dry_run:
        return -1
    vals: dict[str, Any] = {
        "name": (name or "Cliente").strip() or "Cliente",
        "type": "contact",
        "customer_rank": 1,
    }
    if phone_norm:
        vals["phone"] = phone_norm
    return int(client.execute_kw("res.partner", "create", [vals]))


def _parse_when_to_start(when_text: str, *, now: datetime | None = None) -> datetime:
    """Best-effort local MX datetime from Beatriz when-text."""
    base = now or datetime.now(MX_TZ)
    if base.tzinfo is None:
        base = base.replace(tzinfo=MX_TZ)
    else:
        base = base.astimezone(MX_TZ)
    text = (when_text or "").strip().casefold()
    day = base
    if "pasado mañana" in text or "pasado manana" in text:
        day = base + timedelta(days=2)
    elif "mañana" in text or "manana" in text:
        day = base + timedelta(days=1)
    elif "hoy" in text:
        day = base

    if "media hora" in text:
        return (base + timedelta(minutes=30)).replace(second=0, microsecond=0)

    mer_pm = bool(re.search(r"\bpm\b|p\.?\s*m\.?", text))
    mer_am = bool(re.search(r"\bam\b|a\.?\s*m\.?", text))
    m = re.search(
        r"(?:a\s+las?\s+)?(\d{1,2})(?:[:.](\d{2}))?",
        text,
    )
    if not m:
        # default: next round hour
        return (base + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)

    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    if mer_pm and hour < 12:
        hour += 12
    if mer_am and hour == 12:
        hour = 0
    # Compact 530 already normalized to 5:30 elsewhere; bare 17 stays 17.
    if hour > 23:
        hour = hour % 24
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _partner_id_for_user(client: OdooCRMClient, user_id: int | None) -> int | None:
    if not user_id:
        return None
    rows = client.execute_kw(
        "res.users",
        "read",
        [[int(user_id)]],
        {"fields": ["partner_id"]},
    )
    if not rows:
        return None
    raw = rows[0].get("partner_id")
    if isinstance(raw, (list, tuple)) and raw:
        return int(raw[0])
    if raw:
        return int(raw)
    return None


def _pick_closer_user_id(branch: str) -> tuple[int | None, dict[str, Any]]:
    """Round-robin closer; prefer mapped closer ids (excludes Desk / Marco)."""
    mapping = load_mapping()
    allowed = set(closer_odoo_ids_from_mapping(branch, mapping=mapping))
    pick = assign_lead_owner(branch)
    meta = pick.as_dict() if hasattr(pick, "as_dict") else {
        "odoo_id": pick.odoo_id,
        "phone": pick.phone,
        "rep_name": pick.rep_name,
        "branch": pick.branch,
    }
    if pick.odoo_id and (not allowed or int(pick.odoo_id) in allowed):
        return int(pick.odoo_id), meta
    # Skip desk / setter: advance until a mapped closer appears (max roster size).
    for _ in range(max(len(allowed), 4)):
        pick = assign_lead_owner(branch)
        meta = pick.as_dict()
        if pick.odoo_id and int(pick.odoo_id) in allowed:
            return int(pick.odoo_id), meta
    if allowed:
        return next(iter(sorted(allowed))), {"branch": branch, "fell_back": True}
    return (int(pick.odoo_id) if pick.odoo_id else None), meta


def sync_booked_appointment(
    *,
    customer_name: str,
    phone: str,
    vehicle: str,
    branch: str,
    when_text: str = "",
    vehicle_price: float | None = None,
    client: OdooCRMClient | None = None,
    manager: CRMLeadManager | None = None,
    dry_run: bool | None = None,
    lead_id: int | None = None,
) -> AppointmentSyncResult:
    """Create/update partner + lead + calendar for a confirmed cita."""
    crm = client or OdooCRMClient()
    use_dry = crm.dry_run if dry_run is None else bool(dry_run)
    if not use_dry and crm.uid is None:
        crm.authenticate()

    branch_key = normalize_crm_branch(branch)
    result = AppointmentSyncResult(branch=branch_key, dry_run=use_dry)
    mapping = load_mapping()
    result.setter_user_id = setter_user_id_from_mapping(mapping=mapping)

    try:
        result.partner_id = ensure_customer_partner(
            crm,
            name=customer_name,
            phone=phone,
            dry_run=use_dry,
        )
    except Exception as exc:
        result.warnings.append(f"partner: {exc}")

    _, team_id, _ = resolve_team_id(branch_key)
    mapped_team = ((mapping.get("teams") or {}).get(branch_key) or {}).get("team_id")
    if mapped_team and not team_id:
        try:
            team_id = int(mapped_team)
        except (TypeError, ValueError):
            team_id = team_id
    result.team_id = team_id

    closer_id: int | None = None
    closer_meta: dict[str, Any] = {}
    if lead_id and not use_dry:
        try:
            rows = crm.execute_kw(
                "crm.lead",
                "read",
                [[int(lead_id)]],
                {"fields": ["user_id"]},
            )
            raw = rows[0].get("user_id") if rows else None
            if isinstance(raw, (list, tuple)) and raw:
                closer_id = int(raw[0])
                closer_meta = {"odoo_id": closer_id, "reused_lead_owner": True}
            elif raw:
                closer_id = int(raw)
                closer_meta = {"odoo_id": closer_id, "reused_lead_owner": True}
        except Exception as exc:
            result.warnings.append(f"read lead owner: {exc}")
    if closer_id is None:
        closer_id, closer_meta = _pick_closer_user_id(branch_key)
    result.closer_user_id = closer_id
    if closer_meta.get("fell_back"):
        result.warnings.append("closer fell back to mapped roster")

    start_dt = _parse_when_to_start(when_text)
    result.when_iso = start_dt.isoformat()
    dest = BRANCH_LABELS.get(branch_key, branch_key)
    price_bit = (
        f"Precio lista: ${float(vehicle_price):,.0f}\n"
        if vehicle_price not in (None, "")
        else ""
    )
    description = (
        f"Vehículo: {vehicle}\n"
        f"{price_bit}"
        f"Sucursal: {dest}\n"
        f"Cita: {when_text or start_dt.strftime('%Y-%m-%d %H:%M')}\n"
        f"Set by Marco (Appointment Setter)\n"
        f"Closer RR: {closer_meta.get('rep_name') or closer_id}\n"
    )

    title = f"Cita: {(customer_name or 'Cliente').strip()} - {(vehicle or 'Consulta').strip()}"[
        :128
    ]

    if lead_id:
        result.lead_id = int(lead_id)
        if not use_dry:
            try:
                vals: dict[str, Any] = {
                    "name": title,
                    "description": description,
                }
                if result.partner_id:
                    vals["partner_id"] = int(result.partner_id)
                if team_id:
                    vals["team_id"] = int(team_id)
                if closer_id:
                    vals["user_id"] = int(closer_id)
                crm.execute_kw("crm.lead", "write", [[int(lead_id)], vals])
            except Exception as exc:
                result.warnings.append(f"lead update: {exc}")
    elif use_dry:
        result.lead_id = -1
    else:
        mgr = manager or CRMLeadManager(client=crm)
        payload: dict[str, Any] = {
            "name": customer_name,
            "client_name": customer_name,
            "phone": phone,
            "vehicle_info": vehicle,
            "vehicle_name": vehicle,
            "description": description,
            "notes": description,
            "channel": "WhatsApp",
            "medium_name": "WhatsApp",
            "source_name": "WA Directo",
            "appointment_date": when_text or start_dt.strftime("%Y-%m-%d %H:%M"),
            "opportunity_name": title,
            "stage_name": "Beatriz Cita",
            "assign_round_robin": False,  # closer already chosen above
            "preserve_salesperson": False,
        }
        if result.partner_id and result.partner_id > 0:
            payload["partner_id"] = int(result.partner_id)
        try:
            created = mgr.create_or_update_lead(payload, branch=branch_key)
            result.lead_id = int(created.get("lead_id") or 0) or None
            if closer_id and result.lead_id:
                try:
                    crm.assign_lead_advisor(int(result.lead_id), int(closer_id))
                except Exception as exc:
                    result.warnings.append(f"assign closer: {exc}")
            if team_id and result.lead_id:
                try:
                    crm.execute_kw(
                        "crm.lead",
                        "write",
                        [[int(result.lead_id)], {"team_id": int(team_id)}],
                    )
                except Exception as exc:
                    result.warnings.append(f"team write: {exc}")
            if result.partner_id and result.lead_id and result.partner_id > 0:
                try:
                    crm.execute_kw(
                        "crm.lead",
                        "write",
                        [
                            [int(result.lead_id)],
                            {"partner_id": int(result.partner_id)},
                        ],
                    )
                except Exception as exc:
                    result.warnings.append(f"partner link: {exc}")
        except Exception as exc:
            result.error = f"lead: {exc}"
            return result

    # Calendar attendees: customer + closer + Marco (setter).
    partner_ids: list[int] = []
    if result.partner_id and result.partner_id > 0:
        partner_ids.append(int(result.partner_id))
    for uid in (closer_id, result.setter_user_id):
        pid = None if use_dry else _partner_id_for_user(crm, uid)
        if pid and pid not in partner_ids:
            partner_ids.append(pid)

    if use_dry:
        result.event_id = -1
        return result

    if not result.lead_id:
        result.error = result.error or "missing lead_id"
        return result

    try:
        td = crm.create_test_drive_event(
            lead_id=int(result.lead_id),
            vehicle_model=vehicle,
            customer_name=customer_name,
            start=start_dt,
            user_id=closer_id,
            partner_id=result.partner_id if result.partner_id and result.partner_id > 0 else None,
            phone=phone,
            duration_hours=1.0,
            advance_stage=True,
            schedule_confirmation_activity=True,
            branch_id=int(team_id or 1),
            dry_run=False,
        )
        result.event_id = td.event_id
        if td.error:
            result.warnings.append(td.error)
        # Ensure setter is on the event attendees when create_test_drive_event
        # only attached customer + closer.
        if td.event_id and result.setter_user_id:
            setter_partner = _partner_id_for_user(crm, result.setter_user_id)
            if setter_partner and setter_partner not in partner_ids:
                partner_ids.append(setter_partner)
            if partner_ids:
                try:
                    crm.execute_kw(
                        "calendar.event",
                        "write",
                        [[int(td.event_id)], {"partner_ids": [(6, 0, partner_ids)]}],
                    )
                except Exception as exc:
                    result.warnings.append(f"calendar attendees: {exc}")
    except Exception as exc:
        result.warnings.append(f"calendar: {exc}")

    return result


__all__ = [
    "AppointmentSyncResult",
    "ensure_customer_partner",
    "sync_booked_appointment",
]
