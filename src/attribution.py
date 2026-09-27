"""Lead attribution & sales-commission ledger (SQLite).

Tracks Beatriz / WhatsApp / Voice CRM leads through close and computes a
billable commission summary. Default DB: ``data/commissions.db``.

Sale statuses: ``WON`` / ``IN_PROGRESS`` / ``LOST``.
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config import (
    PLACEHOLDER_BRANCH,
    PRIMARY_BRANCH,
    load_branch_reps,
    normalize_rep_phone,
)

ENV_COMMISSIONS_DB = "COMMISSIONS_DB_PATH"
DEFAULT_COMMISSIONS_DB = "data/commissions.db"
ENV_COMMISSION_PCT = "COMMISSION_DEFAULT_PERCENTAGE"
ENV_WON_STAGES = "COMMISSION_WON_STAGES"
ENV_LOST_STAGES = "COMMISSION_LOST_STAGES"

SALE_WON = "WON"
SALE_IN_PROGRESS = "IN_PROGRESS"
SALE_LOST = "LOST"

DEFAULT_WON_STAGES = (
    "won",
    "ganado",
    "ganada",
    "closed won",
    "cerrado ganado",
    "vendido",
    "sold",
    "won / sold",
)
DEFAULT_LOST_STAGES = (
    "lost",
    "perdido",
    "perdida",
    "cancelled",
    "cancelado",
    "cancelada",
    "dead",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS commissions (
    lead_id INTEGER NOT NULL PRIMARY KEY,
    customer_phone TEXT NOT NULL DEFAULT '',
    customer_name TEXT NOT NULL DEFAULT '',
    vehicle_vin TEXT NOT NULL DEFAULT '',
    vehicle_name TEXT NOT NULL DEFAULT '',
    assigned_rep TEXT NOT NULL DEFAULT '',
    assigned_rep_phone TEXT NOT NULL DEFAULT '',
    odoo_user_id INTEGER,
    branch TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT '',
    first_contact_timestamp TEXT NOT NULL DEFAULT '',
    closed_at TEXT NOT NULL DEFAULT '',
    sale_status TEXT NOT NULL DEFAULT 'IN_PROGRESS',
    deal_amount REAL NOT NULL DEFAULT 0,
    commission_percentage REAL NOT NULL DEFAULT 0,
    commission_amount REAL NOT NULL DEFAULT 0,
    source_tag TEXT NOT NULL DEFAULT 'MG Quote Lead',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_commissions_status ON commissions(sale_status);
CREATE INDEX IF NOT EXISTS idx_commissions_closed ON commissions(closed_at);
CREATE INDEX IF NOT EXISTS idx_commissions_rep ON commissions(assigned_rep);
"""

_lock = threading.Lock()


def commissions_db_path() -> Path:
    raw = (os.getenv(ENV_COMMISSIONS_DB) or DEFAULT_COMMISSIONS_DB).strip()
    return Path(raw or DEFAULT_COMMISSIONS_DB)


def default_commission_percentage() -> float:
    raw = (os.getenv(ENV_COMMISSION_PCT) or "1.0").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 1.0


def _normalize_stage(stage: str | None) -> str:
    text = (stage or "").strip().lower()
    for a, b in (
        ("á", "a"),
        ("é", "e"),
        ("í", "i"),
        ("ó", "o"),
        ("ú", "u"),
    ):
        text = text.replace(a, b)
    return " ".join(text.replace("_", " ").replace("-", " ").split())


def _stage_set(env_name: str, defaults: tuple[str, ...]) -> frozenset[str]:
    raw = (os.getenv(env_name) or "").strip()
    if not raw:
        return frozenset(_normalize_stage(s) for s in defaults)
    return frozenset(
        _normalize_stage(part) for part in raw.split(",") if part.strip()
    )


def won_stages() -> frozenset[str]:
    return _stage_set(ENV_WON_STAGES, DEFAULT_WON_STAGES)


def lost_stages() -> frozenset[str]:
    return _stage_set(ENV_LOST_STAGES, DEFAULT_LOST_STAGES)


def classify_sale_status(stage: str | None) -> str:
    norm = _normalize_stage(stage)
    if not norm:
        return SALE_IN_PROGRESS
    if norm in won_stages():
        return SALE_WON
    if norm in lost_stages():
        return SALE_LOST
    return SALE_IN_PROGRESS


def is_won_stage(stage: str | None) -> bool:
    return classify_sale_status(stage) == SALE_WON


def is_lost_stage(stage: str | None) -> bool:
    return classify_sale_status(stage) == SALE_LOST


def _connect(path: Path | None = None) -> sqlite3.Connection:
    db = path or commissions_db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _digits_phone(phone: str | None) -> str:
    return re.sub(r"\D", "", str(phone or ""))


def resolve_rep_label(
    *,
    odoo_user_id: int | None = None,
    rep_name: str | None = None,
    branch: str | None = None,
) -> tuple[str, str]:
    """Return ``(display_name, normalized_phone)`` from ``REPS_*`` roster."""
    table = load_branch_reps()
    raw = (branch or "").strip().lower().replace(" ", "_")
    if "felipe" in raw:
        key = PLACEHOLDER_BRANCH
    elif raw in {"", "primary", "default", "periferico", "periférico"}:
        key = PRIMARY_BRANCH
    else:
        key = raw or PRIMARY_BRANCH

    ordered: list[Any] = []
    if table.get(key):
        ordered.extend(table[key])
    for br, reps in table.items():
        if br == key:
            continue
        ordered.extend(reps or [])

    uid = None
    try:
        uid = int(odoo_user_id) if odoo_user_id not in (None, "", False) else None
    except (TypeError, ValueError):
        uid = None
    name_key = (rep_name or "").strip().casefold()

    if uid is not None:
        for rep in ordered:
            if rep.odoo_id is not None and int(rep.odoo_id) == uid:
                label = (rep.name or "").strip() or f"user:{uid}"
                return label, normalize_rep_phone(rep.phone)
    if name_key:
        for rep in ordered:
            if (rep.name or "").strip().casefold() == name_key:
                return (rep.name or "").strip(), normalize_rep_phone(rep.phone)
    if rep_name:
        phone = ""
        try:
            from src.assigner import resolve_roster_phone

            phone = resolve_roster_phone(
                branch=key, odoo_id=uid, rep_name=rep_name
            )
        except Exception:
            phone = ""
        return str(rep_name).strip(), phone
    return "", ""


@dataclass
class CommissionRecord:
    lead_id: int
    customer_phone: str = ""
    customer_name: str = ""
    vehicle_vin: str = ""
    vehicle_name: str = ""
    assigned_rep: str = ""
    assigned_rep_phone: str = ""
    odoo_user_id: int | None = None
    branch: str = ""
    channel: str = ""
    first_contact_timestamp: str = ""
    closed_at: str = ""
    sale_status: str = SALE_IN_PROGRESS
    deal_amount: float = 0.0
    commission_percentage: float = 0.0
    commission_amount: float = 0.0
    source_tag: str = "MG Quote Lead"
    updated_at: str = ""

    def to_row(self) -> dict[str, Any]:
        return asdict(self)


def upsert_commission(
    record: CommissionRecord,
    *,
    path: Path | None = None,
) -> CommissionRecord:
    """Insert or update one ledger row (keyed by ``lead_id``)."""
    if record.commission_percentage <= 0:
        record.commission_percentage = default_commission_percentage()
    if record.sale_status == SALE_WON and record.deal_amount > 0:
        record.commission_amount = round(
            record.deal_amount * (record.commission_percentage / 100.0), 2
        )
    elif record.sale_status != SALE_WON:
        record.commission_amount = 0.0
    record.updated_at = _utc_now()
    if not record.first_contact_timestamp:
        record.first_contact_timestamp = record.updated_at

    with _lock:
        conn = _connect(path)
        try:
            conn.execute(
                """
                INSERT INTO commissions (
                    lead_id, customer_phone, customer_name, vehicle_vin,
                    vehicle_name, assigned_rep, assigned_rep_phone, odoo_user_id,
                    branch, channel, first_contact_timestamp, closed_at,
                    sale_status, deal_amount, commission_percentage,
                    commission_amount, source_tag, updated_at
                ) VALUES (
                    :lead_id, :customer_phone, :customer_name, :vehicle_vin,
                    :vehicle_name, :assigned_rep, :assigned_rep_phone, :odoo_user_id,
                    :branch, :channel, :first_contact_timestamp, :closed_at,
                    :sale_status, :deal_amount, :commission_percentage,
                    :commission_amount, :source_tag, :updated_at
                )
                ON CONFLICT(lead_id) DO UPDATE SET
                    customer_phone=excluded.customer_phone,
                    customer_name=excluded.customer_name,
                    vehicle_vin=CASE
                        WHEN excluded.vehicle_vin != '' THEN excluded.vehicle_vin
                        ELSE commissions.vehicle_vin END,
                    vehicle_name=CASE
                        WHEN excluded.vehicle_name != '' THEN excluded.vehicle_name
                        ELSE commissions.vehicle_name END,
                    assigned_rep=CASE
                        WHEN excluded.assigned_rep != '' THEN excluded.assigned_rep
                        ELSE commissions.assigned_rep END,
                    assigned_rep_phone=CASE
                        WHEN excluded.assigned_rep_phone != ''
                        THEN excluded.assigned_rep_phone
                        ELSE commissions.assigned_rep_phone END,
                    odoo_user_id=COALESCE(
                        excluded.odoo_user_id, commissions.odoo_user_id
                    ),
                    branch=CASE
                        WHEN excluded.branch != '' THEN excluded.branch
                        ELSE commissions.branch END,
                    channel=CASE
                        WHEN excluded.channel != '' THEN excluded.channel
                        ELSE commissions.channel END,
                    first_contact_timestamp=CASE
                        WHEN commissions.first_contact_timestamp != ''
                        THEN commissions.first_contact_timestamp
                        ELSE excluded.first_contact_timestamp END,
                    closed_at=CASE
                        WHEN excluded.sale_status = 'WON'
                             AND excluded.closed_at != ''
                        THEN excluded.closed_at
                        WHEN excluded.sale_status = 'WON'
                             AND commissions.closed_at = ''
                        THEN excluded.updated_at
                        WHEN excluded.sale_status != 'WON'
                        THEN ''
                        ELSE commissions.closed_at END,
                    sale_status=excluded.sale_status,
                    deal_amount=CASE
                        WHEN excluded.deal_amount > 0 THEN excluded.deal_amount
                        ELSE commissions.deal_amount END,
                    commission_percentage=excluded.commission_percentage,
                    commission_amount=excluded.commission_amount,
                    source_tag=excluded.source_tag,
                    updated_at=excluded.updated_at
                """,
                record.to_row(),
            )
            conn.commit()
        finally:
            conn.close()
    return record


def get_commission(lead_id: int, *, path: Path | None = None) -> CommissionRecord | None:
    with _lock:
        conn = _connect(path)
        try:
            row = conn.execute(
                "SELECT * FROM commissions WHERE lead_id = ?",
                (int(lead_id),),
            ).fetchone()
        finally:
            conn.close()
    if row is None:
        return None
    return CommissionRecord(**{k: row[k] for k in row.keys()})


def list_commissions(
    *,
    sale_status: str | None = None,
    month: str | None = None,
    path: Path | None = None,
) -> list[CommissionRecord]:
    """List ledger rows. ``month`` is ``YYYY-MM`` matched on ``closed_at`` / ``updated_at``."""
    clauses: list[str] = []
    params: list[Any] = []
    if sale_status:
        clauses.append("sale_status = ?")
        params.append(sale_status)
    if month:
        clauses.append(
            "(COALESCE(NULLIF(closed_at,''), updated_at) LIKE ?)"
        )
        params.append(f"{month}%")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        f"SELECT * FROM commissions {where} "
        "ORDER BY COALESCE(NULLIF(closed_at,''), updated_at) DESC, lead_id DESC"
    )
    with _lock:
        conn = _connect(path)
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
    return [CommissionRecord(**{k: r[k] for k in r.keys()}) for r in rows]


@dataclass
class MonthlyCommissionSummary:
    month: str
    won_count: int = 0
    in_progress_count: int = 0
    lost_count: int = 0
    total_deal_amount: float = 0.0
    total_commission: float = 0.0
    by_rep: dict[str, dict[str, float]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def monthly_summary(
    month: str | None = None,
    *,
    path: Path | None = None,
) -> MonthlyCommissionSummary:
    """Billable commission rollup for one calendar month (UTC ``YYYY-MM``)."""
    if not month:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
    rows = list_commissions(path=path)
    summary = MonthlyCommissionSummary(month=month)
    for row in rows:
        stamp = (row.closed_at or row.first_contact_timestamp or row.updated_at)[:7]
        if row.sale_status == SALE_WON:
            stamp = (row.closed_at or row.updated_at)[:7]
        if stamp != month and not (
            row.sale_status == SALE_IN_PROGRESS and stamp == month
        ):
            # Count open pipeline for current month by first contact; won/lost by close.
            if row.sale_status == SALE_WON:
                continue
            if row.sale_status == SALE_LOST:
                lost_stamp = (row.updated_at or "")[:7]
                if lost_stamp != month:
                    continue
            elif (row.first_contact_timestamp or row.updated_at)[:7] != month:
                continue

        if row.sale_status == SALE_WON:
            if (row.closed_at or row.updated_at)[:7] != month:
                continue
            summary.won_count += 1
            summary.total_deal_amount += float(row.deal_amount or 0)
            summary.total_commission += float(row.commission_amount or 0)
            rep = row.assigned_rep or "(sin asesor)"
            bucket = summary.by_rep.setdefault(
                rep, {"won": 0, "deal_amount": 0.0, "commission": 0.0}
            )
            bucket["won"] += 1
            bucket["deal_amount"] += float(row.deal_amount or 0)
            bucket["commission"] += float(row.commission_amount or 0)
        elif row.sale_status == SALE_LOST:
            summary.lost_count += 1
        else:
            summary.in_progress_count += 1
    return summary


def record_from_lead_payload(
    lead_id: int,
    lead_data: dict[str, Any],
    *,
    stage: str | None = None,
) -> CommissionRecord:
    """Map free-form CRM / trigger payload → ledger row."""
    stage_label = stage or str(
        lead_data.get("stage")
        or lead_data.get("stage_name")
        or lead_data.get("new_stage")
        or ""
    )
    status = classify_sale_status(stage_label)

    phone = str(
        lead_data.get("phone")
        or lead_data.get("mobile")
        or lead_data.get("customer_phone")
        or lead_data.get("caller_phone")
        or ""
    ).strip()
    name = str(
        lead_data.get("contact_name")
        or lead_data.get("client_name")
        or lead_data.get("partner_name")
        or lead_data.get("name")
        or ""
    ).strip()
    vehicle = lead_data.get("vehicle") if isinstance(lead_data.get("vehicle"), dict) else {}
    vin = str(
        lead_data.get("vin")
        or lead_data.get("vin_sn")
        or vehicle.get("vin")
        or vehicle.get("vin_sn")
        or ""
    ).strip()
    vehicle_name = str(
        lead_data.get("vehicle_name")
        or lead_data.get("interested_vehicle")
        or lead_data.get("vehicle_interest")
        or vehicle.get("name")
        or vehicle.get("vehicle_name")
        or ""
    ).strip()

    user_raw = lead_data.get("user_id") or lead_data.get("odoo_user_id")
    odoo_uid: int | None = None
    if isinstance(user_raw, (list, tuple)) and user_raw:
        try:
            odoo_uid = int(user_raw[0])
        except (TypeError, ValueError):
            odoo_uid = None
    elif user_raw not in (None, "", False):
        try:
            odoo_uid = int(user_raw)
        except (TypeError, ValueError):
            odoo_uid = None

    rep_hint = str(
        lead_data.get("assigned_rep")
        or lead_data.get("rep_name")
        or lead_data.get("salesperson")
        or (user_raw[1] if isinstance(user_raw, (list, tuple)) and len(user_raw) > 1 else "")
        or ""
    ).strip()
    branch = str(
        lead_data.get("branch")
        or lead_data.get("physical_location")
        or ""
    ).strip()
    rep_label, rep_phone = resolve_rep_label(
        odoo_user_id=odoo_uid, rep_name=rep_hint or None, branch=branch or None
    )
    if not rep_label:
        rep_label = rep_hint or (f"user:{odoo_uid}" if odoo_uid else "")

    amount_raw = (
        lead_data.get("expected_revenue")
        or lead_data.get("deal_amount")
        or lead_data.get("vehicle_price")
        or lead_data.get("price")
        or lead_data.get("list_price")
        or 0
    )
    try:
        deal_amount = float(amount_raw or 0)
    except (TypeError, ValueError):
        deal_amount = 0.0

    first_ts = str(
        lead_data.get("create_date")
        or lead_data.get("first_contact_timestamp")
        or ""
    ).strip()
    closed_at = ""
    if status == SALE_WON:
        closed_at = str(
            lead_data.get("date_closed")
            or lead_data.get("closed_at")
            or lead_data.get("write_date")
            or _utc_now()
        ).strip()

    pct = default_commission_percentage()
    commission_amount = (
        round(deal_amount * (pct / 100.0), 2) if status == SALE_WON else 0.0
    )

    return CommissionRecord(
        lead_id=int(lead_id),
        customer_phone=_digits_phone(phone) or phone,
        customer_name=name,
        vehicle_vin=vin,
        vehicle_name=vehicle_name,
        assigned_rep=rep_label,
        assigned_rep_phone=rep_phone,
        odoo_user_id=odoo_uid,
        branch=branch,
        channel=str(lead_data.get("channel") or "").strip(),
        first_contact_timestamp=first_ts,
        closed_at=closed_at,
        sale_status=status,
        deal_amount=deal_amount,
        commission_percentage=pct,
        commission_amount=commission_amount,
        source_tag=str(lead_data.get("source_tag") or "MG Quote Lead"),
    )


def sync_lead_attribution(
    lead_id: int,
    stage: str,
    lead_data: dict[str, Any] | None = None,
    *,
    path: Path | None = None,
) -> CommissionRecord:
    """Upsert ledger from a stage-change event (won / lost / in progress)."""
    record = record_from_lead_payload(lead_id, dict(lead_data or {}), stage=stage)
    return upsert_commission(record, path=path)


def _odoo_many2one_name(value: Any) -> str:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return str(value[1] or "")
    return ""


def _odoo_many2one_id(value: Any) -> int | None:
    if isinstance(value, (list, tuple)) and value:
        try:
            return int(value[0])
        except (TypeError, ValueError):
            return None
    if value in (None, False, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def fetch_ai_leads_from_odoo(
    client: Any,
    *,
    limit: int = 200,
    tag_name: str = "MG Quote Lead",
) -> list[dict[str, Any]]:
    """``search_read`` CRM leads tagged as AI / Beatriz / WA sourced."""
    client.authenticate()
    tag_ids = client.execute_kw(
        "crm.tag",
        "search",
        [[("name", "=", tag_name)]],
        {"limit": 5},
    )
    domain: list[Any] = []
    if tag_ids:
        domain.append(("tag_ids", "in", list(tag_ids)))
    fields = [
        "id",
        "name",
        "contact_name",
        "phone",
        "mobile",
        "stage_id",
        "user_id",
        "team_id",
        "expected_revenue",
        "create_date",
        "write_date",
        "date_closed",
        "description",
        "tag_ids",
        "medium_id",
        "source_id",
    ]
    rows = client.execute_kw(
        "crm.lead",
        "search_read",
        [domain],
        {"fields": fields, "limit": int(limit), "order": "write_date desc"},
    )
    return list(rows or [])


def reconcile_commissions_from_odoo(
    client: Any | None = None,
    *,
    path: Path | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Cross-reference Odoo AI-tagged leads → local commission ledger.

    Closed / won stages produce billable rows; others stay ``IN_PROGRESS`` / ``LOST``.
    """
    if client is None:
        from src.odoo_sync.client import OdooCRMClient

        client = OdooCRMClient()

    leads = fetch_ai_leads_from_odoo(client, limit=limit)
    synced = 0
    won = 0
    lost = 0
    errors: list[str] = []

    for lead in leads:
        try:
            lead_id = int(lead["id"])
            stage_name = _odoo_many2one_name(lead.get("stage_id"))
            payload = {
                "name": lead.get("name") or "",
                "contact_name": lead.get("contact_name") or "",
                "phone": lead.get("phone") or lead.get("mobile") or "",
                "user_id": lead.get("user_id"),
                "expected_revenue": lead.get("expected_revenue") or 0,
                "create_date": lead.get("create_date") or "",
                "write_date": lead.get("write_date") or "",
                "date_closed": lead.get("date_closed") or "",
                "channel": _odoo_many2one_name(lead.get("medium_id"))
                or _odoo_many2one_name(lead.get("source_id")),
                "branch": _odoo_many2one_name(lead.get("team_id")),
                "vehicle_name": str(lead.get("name") or ""),
                "source_tag": "MG Quote Lead",
            }
            # Prefer description snippets for vehicle when title is generic.
            desc = str(lead.get("description") or "")
            if "Vehículo" in desc or "vehicle" in desc.lower():
                for line in desc.splitlines():
                    if "vehículo" in line.lower() or "vehicle" in line.lower():
                        payload["vehicle_name"] = line.split(":", 1)[-1].strip() or payload[
                            "vehicle_name"
                        ]
                        break
            record = sync_lead_attribution(
                lead_id, stage_name, payload, path=path
            )
            synced += 1
            if record.sale_status == SALE_WON:
                won += 1
            elif record.sale_status == SALE_LOST:
                lost += 1
        except Exception as exc:
            errors.append(f"lead={lead.get('id')}: {exc}")

    month = datetime.now(timezone.utc).strftime("%Y-%m")
    summary = monthly_summary(month, path=path)
    return {
        "ok": not errors,
        "fetched": len(leads),
        "synced": synced,
        "won": won,
        "lost": lost,
        "errors": errors,
        "month": month,
        "summary": summary.to_dict(),
    }


__all__ = [
    "DEFAULT_COMMISSIONS_DB",
    "ENV_COMMISSIONS_DB",
    "SALE_IN_PROGRESS",
    "SALE_LOST",
    "SALE_WON",
    "CommissionRecord",
    "MonthlyCommissionSummary",
    "classify_sale_status",
    "commissions_db_path",
    "default_commission_percentage",
    "fetch_ai_leads_from_odoo",
    "get_commission",
    "is_lost_stage",
    "is_won_stage",
    "list_commissions",
    "monthly_summary",
    "reconcile_commissions_from_odoo",
    "record_from_lead_payload",
    "resolve_rep_label",
    "sync_lead_attribution",
    "upsert_commission",
    "won_stages",
]
