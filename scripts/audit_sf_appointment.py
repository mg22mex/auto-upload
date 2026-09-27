#!/usr/bin/env python3
"""Audit latest San Felipe appointment for Marco Gastelum (5216141754852).

Prints the SAN FELIPE APPOINTMENT AUDIT REPORT from Odoo + journal/Evolution
evidence. Safe to run on Oracle (needs .env + journalctl + optional docker).

Usage::

  PYTHONPATH=. .venv/bin/python scripts/audit_sf_appointment.py
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from src.config import parse_reps  # noqa: E402
from src.odoo_sync.client import OdooCRMClient  # noqa: E402


PHONE_NEEDLE = "6141754852"
DEFAULT_LEAD_ID = 1937


def _plain(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", text).strip()


def _journal_hits(since: str = "12 hours ago") -> list[str]:
    try:
        out = subprocess.check_output(
            [
                "journalctl",
                "-u",
                "autosell-webhook",
                "--since",
                since,
                "--no-pager",
                "-n",
                "2000",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return []
    needles = (
        PHONE_NEEDLE,
        "san_felipe",
        "nuevo lead",
        "assign advisor",
        "dispatch_appointment",
        "1937",
        "mustang",
        "11am",
        "res.users(20",
    )
    hits: list[str] = []
    for line in out.splitlines():
        low = line.casefold()
        if any(n in low for n in needles):
            hits.append(line[-320:])
    return hits


def _evolution_nuevo_lead_payload() -> tuple[str, str]:
    """Best-effort: last Nuevo Lead card + remoteJid from Evolution docker logs."""
    try:
        cid = subprocess.check_output(
            ["docker", "ps", "-qf", "name=evolution-api"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip().splitlines()[0]
    except Exception:
        return "", ""
    try:
        logs = subprocess.check_output(
            [
                "docker",
                "logs",
                cid,
                "--since",
                "24h",
            ],
            text=True,
            stderr=subprocess.STDOUT,
        )
    except Exception:
        return "", ""
    # Find the block that mentions lead 1937 / 11am / San Felipe.
    idx = logs.rfind("Nuevo Lead Asignado")
    if idx < 0:
        return "", ""
    chunk = logs[max(0, idx - 800) : idx + 900]
    phone_m = re.search(r"remoteJid: '(\d+)@s\.whatsapp\.net'", chunk)
    # Reconstruct multiline JS string concatenation if present.
    text_m = re.search(
        r"text: '((?:\\'|[^'])*)'",
        chunk.replace("' +\n", "").replace("' +", ""),
        re.DOTALL,
    )
    # Fallback: join quoted fragments after Nuevo Lead
    if not text_m:
        parts = re.findall(r"'([^']*Nuevo Lead[^']*)'|'(👤[^']*)'|'(🚘[^']*)'|'(💳[^']*)'|'(📍[^']*)'|'(🗓️[^']*)'|'(🔗[^']*)'", chunk)
        flat = "".join(p for tup in parts for p in tup if p)
        payload = flat.replace("\\n", "\n") if flat else ""
    else:
        payload = text_m.group(1).replace("\\n", "\n")
    # Prefer the known SF appointment card if present in logs.
    if "id=1937" in chunk or "11am" in chunk:
        # Extract exact concatenated text lines
        lines = re.findall(r"'(🎯[^']*|👤[^']*|🚘[^']*|💳[^']*|📍[^']*|🗓️[^']*|🔗[^']*)'", chunk)
        if lines:
            payload = "\n".join(lines)
    rep_phone = phone_m.group(1) if phone_m else ""
    return rep_phone, payload


def main() -> int:
    reps = parse_reps(os.getenv("REPS_SAN_FELIPE") or "[]")
    client = OdooCRMClient()
    client.authenticate()

    lead_ids = set(
        int(x)
        for x in client.execute_kw(
            "crm.lead",
            "search",
            [[["phone", "ilike", PHONE_NEEDLE]]],
            {"limit": 20, "order": "write_date desc"},
        )
    )
    lead_ids.add(DEFAULT_LEAD_ID)
    fields = [
        "id",
        "name",
        "contact_name",
        "phone",
        "user_id",
        "team_id",
        "stage_id",
        "description",
        "write_date",
    ]
    leads = []
    for lid in lead_ids:
        rows = client.execute_kw("crm.lead", "read", [[lid], fields])
        if rows:
            leads.append(rows[0])

    def score(lead: dict) -> tuple:
        desc = str(lead.get("description") or "").casefold()
        team = lead.get("team_id")
        team_name = team[1] if isinstance(team, (list, tuple)) and len(team) > 1 else ""
        pts = 0
        if "felipe" in str(team_name).casefold():
            pts += 10
        if "11am" in desc or "11:00" in desc or "lunes" in desc:
            pts += 8
        if "mustang" in desc:
            pts += 5
        if "cita" in desc:
            pts += 3
        return (pts, str(lead.get("write_date") or ""))

    best = sorted(leads, key=score, reverse=True)[0] if leads else None
    if not best:
        print("No leads found", file=sys.stderr)
        return 1

    user = best.get("user_id")
    user_name = (
        user[1].strip()
        if isinstance(user, (list, tuple)) and len(user) > 1
        else "UNASSIGNED"
    )
    user_id = user[0] if isinstance(user, (list, tuple)) and user else None
    stage = best.get("stage_id")
    stage_name = (
        stage[1]
        if isinstance(stage, (list, tuple)) and len(stage) > 1
        else str(stage)
    )
    team = best.get("team_id")
    team_name = (
        team[1]
        if isinstance(team, (list, tuple)) and len(team) > 1
        else str(team)
    )
    desc = _plain(str(best.get("description") or ""))
    appt_m = re.search(
        r"Cita solicitada:\s*([^|.\n]+)|Appointment:\s*([^|.\n]+)|11am|lunes[^\n]*",
        desc,
        re.I,
    )
    appt = (appt_m.group(0) if appt_m else "").strip() or "see notes"

    hits = _journal_hits()
    assign_fail = any("res.users(20" in h or "assign advisor failed" in h for h in hits)
    # Infer RR pick: first SF roster entry (Desk) when cursor starts at 0.
    rr_pick = reps[0] if reps else None
    pointer_after = 1 if reps else None
    next_rep = reps[1] if len(reps) > 1 else None

    evo_phone, evo_payload = _evolution_nuevo_lead_payload()
    notif = "YES" if evo_payload and ("1937" in evo_payload or "11am" in evo_payload) else (
        "YES" if evo_payload else "NO / UNKNOWN"
    )
    if assign_fail and rr_pick:
        intended = f"{rr_pick.name} (odoo_id={rr_pick.odoo_id}) — Odoo write FAILED (user missing)"
    elif user_name:
        intended = user_name
    else:
        intended = "UNASSIGNED"

    # Local qualification context
    qdb = Path(os.getenv("WA_QUALIFICATION_DB_PATH") or ROOT / "data" / "wa_qualification.db")
    qual_note = ""
    if qdb.exists():
        conn = sqlite3.connect(qdb)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM wa_qualification WHERE phone LIKE ? AND branch=? "
            "ORDER BY updated_at DESC LIMIT 1",
            (f"%{PHONE_NEEDLE}%", "san_felipe"),
        ).fetchone()
        if row:
            d = dict(row)
            qual_note = (
                f"qual SF: appt={d.get('appointment_time')} "
                f"trade_in={d.get('trade_in_vehicle')} "
                f"interest={d.get('vehicle_interest')}"
            )

    payload_preview = evo_payload.replace("\n", " | ") if evo_payload else "(not found in Evolution logs)"
    rep_phone = evo_phone or (rr_pick.phone if rr_pick else "n/a")

    print("==================================================")
    print("SAN FELIPE APPOINTMENT AUDIT REPORT")
    print("==================================================")
    print(f"Odoo Lead ID:      {best.get('id')}")
    print(f"Lead Title:        {best.get('name')}")
    print(f"Lead Stage:        {stage_name}")
    print(f"Team:              {team_name}")
    print(f"Assigned Sales Rep:{intended}")
    if user_id is not None:
        print(f"CRM user_id now:   {user_id} ({user_name})")
    print(f"Rep Phone Number:  {rep_phone}")
    print(f"Appointment:       {appt}")
    print(f"Pointer Index:     {pointer_after} (inferred next after Desk@0; webhook in-proc)")
    if next_rep:
        print(f"Next Rep in Queue: {next_rep.name} (odoo_id={next_rep.odoo_id})")
    print(f"Notification Sent: {notif}")
    print(f"Payload Preview:   {payload_preview[:500]}")
    print("==================================================")
    if qual_note:
        print(f"NOTE: {qual_note}")
    print(
        "NOTE: CRM vehicle on this cita is Corolla sticky — Mustang 2024 trade-in "
        "lived on autosell_periferico session, not written into SF handoff card."
    )
    if assign_fail:
        print(
            "NOTE: REPS_SAN_FELIPE[0] odoo_id=20 (San Felipe Desk) is NOT a real "
            "res.users row — Odoo assign failed; WA still went to Desk phone."
        )
    print("\n--- Roster REPS_SAN_FELIPE ---")
    for i, r in enumerate(reps):
        mark = " <- intended pick (index 0)" if i == 0 else ""
        if pointer_after is not None and i == pointer_after:
            mark += " <- next"
        print(f"  [{i}] {r.name} odoo_id={r.odoo_id} phone={r.phone}{mark}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
