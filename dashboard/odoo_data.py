"""Live Odoo CRM queries for the executive Streamlit dashboard."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MAPPING_PATH = ROOT / "data" / "odoo_mapping.json"
JUNTA_PATH = ROOT / "data" / "junta_semanal.json"

LEAD_FIELDS = [
    "id",
    "name",
    "contact_name",
    "partner_name",
    "phone",
    "email_from",
    "stage_id",
    "user_id",
    "team_id",
    "expected_revenue",
    "probability",
    "create_date",
    "write_date",
    "date_deadline",
    "date_closed",
    "activity_summary",
    "activity_date_deadline",
    "activity_type_id",
    "lost_reason_id",
    "medium_id",
    "source_id",
    "active",
    "type",
    "tag_ids",
]


def _m2o_id(value: Any) -> int | None:
    if isinstance(value, (list, tuple)) and value:
        try:
            return int(value[0])
        except (TypeError, ValueError):
            return None
    if isinstance(value, int):
        return value
    return None


def _m2o_name(value: Any) -> str:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return str(value[1] or "").strip()
    if value in (False, None):
        return ""
    return str(value).strip()


def load_branch_map() -> dict[int, str]:
    """team_id → Periférico | San Felipe."""
    if not MAPPING_PATH.is_file():
        return {1: "Periférico", 5: "San Felipe"}
    try:
        data = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {1: "Periférico", 5: "San Felipe"}
    out: dict[int, str] = {}
    teams = data.get("teams") or {}
    pe = (teams.get("periferico") or {}).get("team_id")
    sf = (teams.get("san_felipe") or {}).get("team_id")
    if pe:
        out[int(pe)] = "Periférico"
    if sf:
        out[int(sf)] = "San Felipe"
    return out or {1: "Periférico", 5: "San Felipe"}


def get_odoo_client():
    """Odoo XML-RPC client — credentials from ``st.secrets`` or env / ``.env``."""
    from dashboard.secrets_util import get_odoo_client as _client_from_secrets

    return _client_from_secrets()


def fetch_stages(client: Any | None = None) -> list[dict[str, Any]]:
    crm = client or get_odoo_client()
    rows = crm.execute_kw(
        "crm.stage",
        "search_read",
        [[]],
        {"fields": ["id", "name", "sequence", "is_won"], "order": "sequence"},
    )
    return list(rows or [])


def fetch_leads(
    client: Any | None = None,
    *,
    days: int = 90,
    limit: int = 2500,
    include_lost: bool = True,
) -> list[dict[str, Any]]:
    """Fetch opportunities created/updated within ``days`` (plus all active)."""
    crm = client or get_odoo_client()
    since = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    domain: list[Any] = [
        "|",
        ("create_date", ">=", since),
        ("write_date", ">=", since),
    ]
    kwargs: dict[str, Any] = {
        "fields": LEAD_FIELDS,
        "limit": int(limit),
        "order": "write_date desc",
    }
    if include_lost:
        kwargs["context"] = {"active_test": False}
    rows = crm.execute_kw("crm.lead", "search_read", [domain], kwargs)
    return list(rows or [])


def fetch_calendar_appointments(
    client: Any | None = None,
    *,
    days_back: int = 30,
    days_forward: int = 60,
) -> list[dict[str, Any]]:
    crm = client or get_odoo_client()
    start = (date.today() - timedelta(days=days_back)).isoformat()
    stop = (date.today() + timedelta(days=days_forward)).isoformat()
    try:
        rows = crm.execute_kw(
            "calendar.event",
            "search_read",
            [[("start", ">=", start), ("start", "<=", stop)]],
            {
                "fields": [
                    "id",
                    "name",
                    "start",
                    "stop",
                    "user_id",
                    "partner_ids",
                    "opportunity_id",
                ],
                "limit": 2000,
                "order": "start asc",
            },
        )
        return list(rows or [])
    except Exception:
        return []


def fetch_sale_orders(
    client: Any | None = None,
    *,
    days: int = 90,
) -> list[dict[str, Any]]:
    crm = client or get_odoo_client()
    since = (date.today() - timedelta(days=days)).isoformat()
    try:
        rows = crm.execute_kw(
            "sale.order",
            "search_read",
            [[("date_order", ">=", since)]],
            {
                "fields": [
                    "id",
                    "name",
                    "state",
                    "amount_total",
                    "partner_id",
                    "user_id",
                    "team_id",
                    "date_order",
                    "opportunity_id",
                ],
                "limit": 1000,
                "order": "date_order desc",
            },
        )
        return list(rows or [])
    except Exception:
        return []


def normalize_leads(
    rows: list[dict[str, Any]],
    *,
    branch_map: dict[int, str] | None = None,
) -> list[dict[str, Any]]:
    branches = branch_map or load_branch_map()
    out: list[dict[str, Any]] = []
    for row in rows:
        team_id = _m2o_id(row.get("team_id"))
        stage = _m2o_name(row.get("stage_id"))
        active = bool(row.get("active", True))
        lost_reason = _m2o_name(row.get("lost_reason_id"))
        if not active and not lost_reason:
            lost_reason = "Perdido (sin motivo)"
        out.append(
            {
                "id": int(row["id"]),
                "name": str(row.get("name") or "").strip(),
                "contact": str(
                    row.get("contact_name")
                    or row.get("partner_name")
                    or ""
                ).strip()
                or "—",
                "phone": str(row.get("phone") or "").strip() or "—",
                "email": str(row.get("email_from") or "").strip() or "—",
                "stage": stage or "Sin etapa",
                "stage_id": _m2o_id(row.get("stage_id")),
                "vendedor": _m2o_name(row.get("user_id")) or "Sin asignar",
                "user_id": _m2o_id(row.get("user_id")),
                "team": _m2o_name(row.get("team_id")) or "—",
                "sucursal": branches.get(team_id or -1)
                or (_m2o_name(row.get("team_id")) or "Sin sucursal"),
                "expected_revenue": float(row.get("expected_revenue") or 0),
                "probability": float(row.get("probability") or 0),
                "create_date": str(row.get("create_date") or ""),
                "write_date": str(row.get("write_date") or ""),
                "date_deadline": str(row.get("date_deadline") or "") or "—",
                "date_closed": str(row.get("date_closed") or "") or "—",
                "proxima_accion": str(row.get("activity_summary") or "").strip()
                or "—",
                "activity_deadline": str(row.get("activity_date_deadline") or "")
                or "—",
                "lost_reason": lost_reason or "—",
                "medium": _m2o_name(row.get("medium_id")) or "—",
                "source": _m2o_name(row.get("source_id")) or "—",
                "active": active,
                "estatus": (
                    "Perdido"
                    if not active
                    else ("Ganado" if stage.lower() in {"won", "ganado"} else stage)
                ),
            }
        )
    return out


def compute_kpis(
    leads: list[dict[str, Any]],
    appointments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    active = [L for L in leads if L.get("active")]
    lost = [L for L in leads if not L.get("active")]
    won = [
        L
        for L in active
        if str(L.get("stage") or "").lower() in {"won", "ganado"}
    ]
    cita_stages = ("cita", "prueba", "agend")
    citas = [
        L
        for L in active
        if any(tok in str(L.get("stage") or "").lower() for tok in cita_stages)
    ]
    # Prefer calendar count when available
    appt_count = len(appointments or []) if appointments is not None else len(citas)
    if appointments:
        appt_count = max(appt_count, len(citas))

    pipeline_value = sum(float(L.get("expected_revenue") or 0) for L in active)
    closed_value = sum(float(L.get("expected_revenue") or 0) for L in won)
    decided = len(won) + len(lost)
    close_rate = (100.0 * len(won) / decided) if decided else 0.0

    by_stage: dict[str, int] = {}
    for L in active:
        key = str(L.get("stage") or "Sin etapa")
        by_stage[key] = by_stage.get(key, 0) + 1

    by_rep: dict[str, dict[str, float]] = {}
    by_branch: dict[str, dict[str, float]] = {}
    for L in leads:
        rep = str(L.get("vendedor") or "Sin asignar")
        branch = str(L.get("sucursal") or "Sin sucursal")
        for bucket, key in ((by_rep, rep), (by_branch, branch)):
            stats = bucket.setdefault(
                key, {"prospectos": 0, "won": 0, "lost": 0, "revenue": 0.0}
            )
            stats["prospectos"] += 1
            if not L.get("active"):
                stats["lost"] += 1
            elif str(L.get("stage") or "").lower() in {"won", "ganado"}:
                stats["won"] += 1
                stats["revenue"] += float(L.get("expected_revenue") or 0)

    def _eff(stats: dict[str, float]) -> float:
        d = float(stats["won"] + stats["lost"])
        return (100.0 * stats["won"] / d) if d else 0.0

    rep_eff = [
        {
            "vendedor": k,
            "prospectos": int(v["prospectos"]),
            "ganados": int(v["won"]),
            "perdidos": int(v["lost"]),
            "efectividad_%": round(_eff(v), 1),
            "venta": float(v["revenue"]),
        }
        for k, v in sorted(by_rep.items(), key=lambda x: -x[1]["won"])
    ]
    branch_eff = [
        {
            "sucursal": k,
            "prospectos": int(v["prospectos"]),
            "ganados": int(v["won"]),
            "perdidos": int(v["lost"]),
            "efectividad_%": round(_eff(v), 1),
            "venta": float(v["revenue"]),
        }
        for k, v in sorted(by_branch.items(), key=lambda x: -x[1]["prospectos"])
    ]

    lost_reasons: dict[str, int] = {}
    for L in lost:
        reason = str(L.get("lost_reason") or "Sin motivo")
        lost_reasons[reason] = lost_reasons.get(reason, 0) + 1

    credit = [
        L
        for L in active
        if "credit" in str(L.get("stage") or "").lower()
        or "financ" in str(L.get("stage") or "").lower()
        or "credito" in str(L.get("stage") or "").lower()
        or "crédito" in str(L.get("stage") or "").lower()
    ]

    return {
        "total_prospectos": len(active),
        "citas_agendadas": appt_count,
        "eficiencia_cierre": round(close_rate, 1),
        "venta_total_estimada": pipeline_value,
        "venta_cerrada": closed_value,
        "ganados": len(won),
        "perdidos": len(lost),
        "financiamientos": len(credit),
        "by_stage": by_stage,
        "rep_eff": rep_eff,
        "branch_eff": branch_eff,
        "lost_reasons": lost_reasons,
        "credit_leads": credit,
    }


def load_junta_notes() -> list[dict[str, Any]]:
    if not JUNTA_PATH.is_file():
        return []
    try:
        data = json.loads(JUNTA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return list(data.get("notes") or [])


def save_junta_note(
    *,
    week_label: str,
    compromisos: str,
    acuerdos: str,
    author: str = "",
) -> dict[str, Any]:
    notes = load_junta_notes()
    entry = {
        "id": int(datetime.now(timezone.utc).timestamp()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "week": week_label,
        "compromisos": compromisos.strip(),
        "acuerdos": acuerdos.strip(),
        "author": (author or "").strip() or "Gerencia",
    }
    notes.insert(0, entry)
    JUNTA_PATH.parent.mkdir(parents=True, exist_ok=True)
    JUNTA_PATH.write_text(
        json.dumps({"notes": notes[:100]}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return entry


def build_acta_pdf(
    *,
    week_label: str,
    kpis: dict[str, Any],
    compromisos: str,
    acuerdos: str,
) -> bytes:
    from io import BytesIO

    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    width, height = letter
    y = height - inch
    c.setFont("Helvetica-Bold", 16)
    c.drawString(inch, y, "Autosell — Acta Junta Semanal de Ventas")
    y -= 22
    c.setFont("Helvetica", 11)
    c.drawString(inch, y, f"Semana: {week_label}")
    y -= 28
    c.setFont("Helvetica-Bold", 12)
    c.drawString(inch, y, "KPIs")
    y -= 18
    c.setFont("Helvetica", 10)
    lines = [
        f"Prospectos activos: {kpis.get('total_prospectos', 0)}",
        f"Citas agendadas: {kpis.get('citas_agendadas', 0)}",
        f"Eficiencia de cierre: {kpis.get('eficiencia_cierre', 0)}%",
        f"Venta estimada pipeline: ${kpis.get('venta_total_estimada', 0):,.0f}",
        f"Ganados / Perdidos: {kpis.get('ganados', 0)} / {kpis.get('perdidos', 0)}",
    ]
    for line in lines:
        c.drawString(inch, y, line)
        y -= 14
    y -= 10
    c.setFont("Helvetica-Bold", 12)
    c.drawString(inch, y, "Compromisos")
    y -= 16
    c.setFont("Helvetica", 10)
    for para in (compromisos or "—").splitlines() or ["—"]:
        for chunk in _wrap(para, 95):
            if y < inch:
                c.showPage()
                y = height - inch
                c.setFont("Helvetica", 10)
            c.drawString(inch, y, chunk)
            y -= 12
    y -= 10
    c.setFont("Helvetica-Bold", 12)
    c.drawString(inch, y, "Acuerdos")
    y -= 16
    c.setFont("Helvetica", 10)
    for para in (acuerdos or "—").splitlines() or ["—"]:
        for chunk in _wrap(para, 95):
            if y < inch:
                c.showPage()
                y = height - inch
                c.setFont("Helvetica", 10)
            c.drawString(inch, y, chunk)
            y -= 12
    c.showPage()
    c.save()
    return buf.getvalue()


def _wrap(text: str, width: int) -> list[str]:
    words = (text or "").split()
    if not words:
        return ["—"]
    lines: list[str] = []
    cur = words[0]
    for w in words[1:]:
        if len(cur) + 1 + len(w) <= width:
            cur = f"{cur} {w}"
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines


__all__ = [
    "build_acta_pdf",
    "compute_kpis",
    "fetch_calendar_appointments",
    "fetch_leads",
    "fetch_sale_orders",
    "fetch_stages",
    "get_odoo_client",
    "load_branch_map",
    "load_junta_notes",
    "normalize_leads",
    "save_junta_note",
]
