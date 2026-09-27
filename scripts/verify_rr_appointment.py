#!/usr/bin/env python3
"""Inspect latest Marco Gastelum / 614* appointment lead + round-robin state."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1] if (Path(__file__).parent.name == "scripts") else Path.cwd()
load_dotenv(ROOT / ".env")

from src.config import parse_reps  # noqa: E402
from src.odoo_sync.client import OdooCRMClient  # noqa: E402
from src.odoo_sync.crm import _ASSIGNER  # noqa: E402


def main() -> None:
    qdb = Path(os.getenv("WA_QUALIFICATION_DB_PATH") or ROOT / "data" / "wa_qualification.db")
    conn = sqlite3.connect(qdb)
    conn.row_factory = sqlite3.Row
    local_rows: list[dict] = []
    for row in conn.execute("SELECT * FROM wa_qualification ORDER BY updated_at DESC"):
        d = dict(row)
        ph = str(d.get("phone") or "")
        name = str(d.get("contact_name") or "")
        if (
            "6141754852" in ph
            or "6143231198" in ph
            or "Marco" in name
            or "Gastelum" in name
        ):
            local_rows.append(d)

    reps = parse_reps(os.getenv("REPS_PERIFERICO") or "[]")
    cursor = dict(_ASSIGNER._cursor)
    peri_idx = int(cursor.get("periferico", 0) or 0)
    n = len(reps) or 1
    last_assigned_i = (peri_idx - 1) % n if peri_idx else None
    next_i = peri_idx % n if reps else None

    client = OdooCRMClient()
    client.authenticate()

    lead_ids: list[int] = []
    for digits in ("6141754852", "6143231198"):
        found = client.execute_kw(
            "crm.lead",
            "search",
            [[["phone", "ilike", digits[-10:]]]],
            {"limit": 8, "order": "write_date desc"},
        )
        lead_ids.extend(int(x) for x in found)

    for lid in (1937, 2160):
        if lid not in lead_ids:
            lead_ids.append(lid)

    base_fields = [
        "id",
        "name",
        "contact_name",
        "partner_name",
        "phone",
        "user_id",
        "team_id",
        "stage_id",
        "description",
        "write_date",
        "create_date",
    ]
    leads: list[dict] = []
    for lid in lead_ids:
        try:
            rows = client.execute_kw(
                "crm.lead",
                "read",
                [[lid], base_fields + ["x_vehicle_name"]],
            )
        except Exception:
            rows = client.execute_kw("crm.lead", "read", [[lid], base_fields])
        if rows:
            leads.append(rows[0])

    def score(lead: dict) -> tuple:
        desc = str(lead.get("description") or "").casefold()
        stage = lead.get("stage_id")
        stage_name = (
            stage[1]
            if isinstance(stage, (list, tuple)) and len(stage) > 1
            else ""
        )
        points = 0
        if "cita" in stage_name.casefold():
            points += 10
        if "trade-in" in desc or "valuaci" in desc or "corolla" in desc:
            points += 5
        if "4 pm" in desc or "mañana" in desc or "manana" in desc:
            points += 3
        if "6141754852" in str(lead.get("phone") or ""):
            points += 8
        return (points, str(lead.get("write_date") or ""))

    leads_sorted = sorted(leads, key=score, reverse=True)
    best = leads_sorted[0] if leads_sorted else None

    team_id = None
    rr_odoo = None
    if best and best.get("team_id"):
        team_id = (
            best["team_id"][0]
            if isinstance(best["team_id"], (list, tuple))
            else best["team_id"]
        )
        try:
            key = f"autosell.round_robin.branch_{int(team_id)}"
            params = client.execute_kw(
                "ir.config_parameter",
                "search_read",
                [[["key", "=", key]]],
                {"fields": ["value"], "limit": 1},
            )
            if params:
                rr_odoo = int(params[0]["value"])
        except Exception as exc:
            rr_odoo = f"err:{exc}"

    notif = "UNKNOWN"
    log_hits: list[str] = []
    try:
        out = subprocess.check_output(
            [
                "journalctl",
                "-u",
                "autosell-webhook",
                "--since",
                "6 hours ago",
                "--no-pager",
                "-n",
                "800",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        needles = (
            "6141754852",
            "nuevo lead asignado",
            "dispatch_appointment_rep",
            "rep_notification",
            "1937",
            "beatriz cita",
            "gastelum",
        )
        for line in out.splitlines():
            low = line.casefold()
            if any(tok in low for tok in needles):
                log_hits.append(line[-260:])
        joined = "\n".join(log_hits).casefold()
        if (
            "dispatch_appointment_rep_alert sent=true" in joined
            or "nuevo lead asignado" in joined
            or "sent=true" in joined
            and "rep" in joined
        ):
            notif = "YES"
        elif log_hits:
            notif = "PARTIAL (see log hits)"
        else:
            notif = "NO (no matching webhook logs in last 6h)"
    except Exception as exc:
        notif = f"NO (journal unavailable: {exc})"

    local = None
    for r in local_rows:
        if "6141754852" in str(r.get("phone")):
            local = r
            break
    if local is None and local_rows:
        local = local_rows[0]

    user = best.get("user_id") if best else None
    user_name = (
        user[1]
        if isinstance(user, (list, tuple)) and len(user) > 1
        else (str(user) if user else "UNASSIGNED")
    )
    user_id = user[0] if isinstance(user, (list, tuple)) and user else user
    stage = best.get("stage_id") if best else None
    stage_name = (
        stage[1]
        if isinstance(stage, (list, tuple)) and len(stage) > 1
        else str(stage)
    )
    team = best.get("team_id") if best else None
    team_name = (
        team[1]
        if isinstance(team, (list, tuple)) and len(team) > 1
        else str(team)
    )

    assigned_rep = user_name
    matched = None
    if user_id is not None:
        for r in reps:
            if int(r.odoo_id or 0) == int(user_id):
                matched = r
                break
    if matched and matched.name:
        assigned_rep = matched.name

    if reps and next_i is not None:
        nxt = reps[next_i]
        next_rep = nxt.name or f"odoo_id={nxt.odoo_id} phone={nxt.phone}"
    else:
        next_rep = "n/a"

    appt = (local or {}).get("appointment_time") or ""
    desc = str(best.get("description") or "") if best else ""
    lead_name = (
        (local or {}).get("contact_name")
        or (best or {}).get("contact_name")
        or (best or {}).get("name")
        or "n/a"
    )

    print("==================================================")
    print("ROUND ROBIN VERIFICATION RESULT")
    print("==================================================")
    print(f"Lead Name: {lead_name}")
    print("Sucursal: Periférico")
    print(f"Assigned Asesor: {assigned_rep}")
    print(f"Odoo Lead ID: {(best or {}).get('id')}")
    print(f"Stage: {stage_name}")
    print(f"Team: {team_name} (id={team_id})")
    print(f"Phone (CRM): {(best or {}).get('phone')}")
    print(f"Appointment: {appt or 'see notes'}")
    print(f"RR cursor periferico (next index): {peri_idx}")
    print(f"Next Rep in Queue: {next_rep}")
    if rr_odoo is not None:
        print(f"Odoo ir.config_parameter RR (team {team_id}): {rr_odoo}")
    print(f"Notification Sent: {notif}")
    print("==================================================")
    print("\n--- Local qualification match(es) ---")
    for r in local_rows[:6]:
        print(
            {
                k: r.get(k)
                for k in (
                    "phone",
                    "lead_id",
                    "contact_name",
                    "state",
                    "appointment_time",
                    "trade_in_vehicle",
                    "down_payment",
                    "updated_at",
                )
            }
        )
    print("\n--- CRM description (truncated) ---")
    print(desc[:1500])
    print("\n--- Roster ---")
    for i, r in enumerate(reps):
        mark = ""
        if last_assigned_i is not None and i == last_assigned_i:
            mark = " <- last assigned (inferred from cursor)"
        if next_i is not None and i == next_i:
            mark += " <- next"
        print(
            f"  [{i}] {r.name or ''} odoo_id={r.odoo_id} phone={r.phone}{mark}"
        )
    if log_hits:
        print("\n--- Recent log hits ---")
        for line in log_hits[-15:]:
            print(line)


if __name__ == "__main__":
    main()
