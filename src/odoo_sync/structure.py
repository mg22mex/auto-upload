"""Odoo CRM structure bootstrap — teams, users, local ID mapping.

Marco = Appointment Setter (not in closer Round Robin).
Closers = ``REPS_PERIFERICO`` / ``REPS_SAN_FELIPE`` (Desk / setter excluded).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config import (
    BRANCH_LABELS,
    PLACEHOLDER_BRANCH,
    PRIMARY_BRANCH,
    SalesRep,
    load_branch_reps,
    normalize_rep_phone,
)
from src.odoo_sync.base import OdooCRMError
from src.odoo_sync.client import OdooCRMClient
from src.odoo_sync.crm import (
    ENV_TEAM_PERIFERICO,
    ENV_TEAM_SAN_FELIPE,
    normalize_crm_branch,
)

DEFAULT_MAPPING_PATH = Path("data/odoo_mapping.json")

TEAM_NAME_BY_BRANCH: dict[str, tuple[str, ...]] = {
    PRIMARY_BRANCH: ("Ventas Periférico", "Ventas Periferico"),
    PLACEHOLDER_BRANCH: ("Ventas San Felipe",),
}

ENV_SETTER_NAME = "ODOO_SETTER_NAME"
ENV_SETTER_LOGIN = "ODOO_SETTER_LOGIN"
ENV_SETTER_USER_ID = "ODOO_SETTER_USER_ID"
DEFAULT_SETTER_NAME = "Marco"
DEFAULT_SETTER_LOGIN = "marco.setter@autosell.mx"

# Sales / User: All Documents
SALES_USER_GROUP_XMLID_HINT = "Sales / User: All Documents"
SALES_USER_GROUP_ID_FALLBACK = 22


def _short_err(exc: BaseException, limit: int = 180) -> str:
    msg = f"{type(exc).__name__}: {exc}"
    text = str(exc)
    if "Fault" in msg or "Traceback" in text:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        for ln in reversed(lines):
            if ln.startswith(("ValueError:", "AccessError:", "UserError:")) or "QWeb" in ln:
                msg = ln
                break
        else:
            msg = lines[-1] if lines else msg
    if len(msg) > limit:
        return msg[: limit - 3] + "..."
    return msg


@dataclass
class RepMapEntry:
    name: str
    phone: str = ""
    odoo_id: int | None = None
    branch: str = ""
    role: str = "closer"  # closer | desk | setter
    login: str = ""
    created: bool = False
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StructureResult:
    teams: dict[str, int] = field(default_factory=dict)
    reps: dict[str, list[RepMapEntry]] = field(default_factory=dict)
    setter: RepMapEntry | None = None
    mapping_path: str = ""
    warnings: list[str] = field(default_factory=list)

    def as_mapping(self) -> dict[str, Any]:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "teams": {
                "periferico": {
                    "team_id": self.teams.get(PRIMARY_BRANCH),
                    "name": TEAM_NAME_BY_BRANCH[PRIMARY_BRANCH][0],
                },
                "san_felipe": {
                    "team_id": self.teams.get(PLACEHOLDER_BRANCH),
                    "name": TEAM_NAME_BY_BRANCH[PLACEHOLDER_BRANCH][0],
                },
            },
            "setter_user_id": self.setter.odoo_id if self.setter else None,
            "setter": self.setter.as_dict() if self.setter else None,
            "reps": {
                branch: [r.as_dict() for r in entries]
                for branch, entries in self.reps.items()
            },
            "closer_user_ids": {
                branch: [
                    int(r.odoo_id)
                    for r in entries
                    if r.role == "closer" and r.odoo_id
                ]
                for branch, entries in self.reps.items()
            },
            "warnings": list(self.warnings),
        }


def _is_desk(rep: SalesRep) -> bool:
    return "desk" in (rep.name or "").casefold()


def _slug_login(name: str, branch: str) -> str:
    base = re.sub(r"[^a-z0-9]+", ".", (name or "rep").casefold()).strip(".")
    branch_bit = "sf" if normalize_crm_branch(branch) == PLACEHOLDER_BRANCH else "pe"
    return f"{base or 'rep'}.{branch_bit}@autosell.mx"


def find_team_id(client: OdooCRMClient, branch: str) -> int | None:
    names = TEAM_NAME_BY_BRANCH.get(normalize_crm_branch(branch)) or ()
    for name in names:
        rows = client.execute_kw(
            "crm.team",
            "search_read",
            [[["name", "=", name]]],
            {"fields": ["id", "name"], "limit": 1},
        )
        if rows:
            return int(rows[0]["id"])
    # Fuzzy: name ilike label
    label = BRANCH_LABELS.get(normalize_crm_branch(branch), branch)
    rows = client.execute_kw(
        "crm.team",
        "search_read",
        [[["name", "ilike", label]]],
        {"fields": ["id", "name"], "limit": 5},
    )
    for row in rows:
        n = str(row.get("name") or "")
        if "venta" in n.casefold():
            return int(row["id"])
    return None


def ensure_team(
    client: OdooCRMClient,
    branch: str,
    *,
    create: bool = True,
) -> tuple[int | None, bool]:
    """Return (team_id, created)."""
    existing = find_team_id(client, branch)
    if existing:
        return existing, False
    if not create:
        return None, False
    name = TEAM_NAME_BY_BRANCH[normalize_crm_branch(branch)][0]
    team_id = int(
        client.execute_kw(
            "crm.team",
            "create",
            [{"name": name}],
        )
    )
    return team_id, True


def _sales_group_id(client: OdooCRMClient) -> int:
    rows = client.execute_kw(
        "res.groups",
        "search_read",
        [[["full_name", "=", SALES_USER_GROUP_XMLID_HINT]]],
        {"fields": ["id"], "limit": 1},
    )
    if rows:
        return int(rows[0]["id"])
    rows = client.execute_kw(
        "res.groups",
        "search_read",
        [[["full_name", "ilike", "Sales / User: All Documents"]]],
        {"fields": ["id"], "limit": 1},
    )
    if rows:
        return int(rows[0]["id"])
    return SALES_USER_GROUP_ID_FALLBACK


def find_user(
    client: OdooCRMClient,
    *,
    odoo_id: int | None = None,
    name: str = "",
    login: str = "",
    phone: str = "",
) -> dict[str, Any] | None:
    if odoo_id:
        rows = client.execute_kw(
            "res.users",
            "search_read",
            [[["id", "=", int(odoo_id)], ["active", "=", True]]],
            {"fields": ["id", "name", "login", "partner_id"], "limit": 1},
        )
        if rows:
            return rows[0]
    if login:
        rows = client.execute_kw(
            "res.users",
            "search_read",
            [[["login", "=", login], ["active", "=", True]]],
            {"fields": ["id", "name", "login", "partner_id"], "limit": 1},
        )
        if rows:
            return rows[0]
    if name:
        rows = client.execute_kw(
            "res.users",
            "search_read",
            [[["name", "ilike", name], ["active", "=", True], ["share", "=", False]]],
            {"fields": ["id", "name", "login", "partner_id"], "limit": 5},
        )
        needle = name.casefold().strip()
        for row in rows:
            if needle in str(row.get("name") or "").casefold():
                return row
        if rows:
            return rows[0]
    phone_digits = re.sub(r"\D", "", phone or "")
    if phone_digits:
        partners = client.execute_kw(
            "res.partner",
            "search_read",
            [[["phone", "ilike", phone_digits[-10:]]]],
            {"fields": ["id", "name", "phone"], "limit": 10},
        )
        partner_ids = [int(p["id"]) for p in partners]
        if partner_ids:
            rows = client.execute_kw(
                "res.users",
                "search_read",
                [[["partner_id", "in", partner_ids], ["active", "=", True]]],
                {"fields": ["id", "name", "login", "partner_id"], "limit": 1},
            )
            if rows:
                return rows[0]
    return None


def create_internal_user(
    client: OdooCRMClient,
    *,
    name: str,
    login: str,
    phone: str = "",
    team_id: int | None = None,
) -> dict[str, Any]:
    """Create res.partner + res.users with Sales User rights (best-effort)."""
    partner_vals: dict[str, Any] = {
        "name": name.strip() or login,
        "email": login,
        "type": "contact",
    }
    if phone:
        partner_vals["phone"] = normalize_rep_phone(phone) or phone
    partner_id = int(client.execute_kw("res.partner", "create", [partner_vals]))
    group_id = _sales_group_id(client)
    user_vals: dict[str, Any] = {
        "name": name.strip() or login,
        "login": login,
        "partner_id": partner_id,
        # Odoo 19+: ``group_ids`` (legacy ``groups_id`` removed).
        "group_ids": [(4, int(group_id))],
        "notification_type": "inbox",
    }
    user_id = int(
        client.execute_kw(
            "res.users",
            "create",
            [user_vals],
            {
                "context": {
                    "no_reset_password": True,
                    "mail_create_nolog": True,
                    "mail_create_nosubscribe": True,
                    "mail_notrack": True,
                    "tracking_disable": True,
                }
            },
        )
    )
    if team_id:
        try:
            team = client.execute_kw(
                "crm.team",
                "read",
                [[int(team_id)]],
                {"fields": ["member_ids"]},
            )
            members = list((team[0].get("member_ids") if team else []) or [])
            if user_id not in members:
                members.append(user_id)
                client.execute_kw(
                    "crm.team",
                    "write",
                    [[int(team_id)], {"member_ids": [(6, 0, members)]}],
                )
        except Exception as exc:
            print(f"WARN link user={user_id} to team={team_id}: {exc}", flush=True)
    rows = client.execute_kw(
        "res.users",
        "read",
        [[user_id]],
        {"fields": ["id", "name", "login", "partner_id"]},
    )
    return rows[0] if rows else {"id": user_id, "name": name, "login": login}


def link_user_to_team(client: OdooCRMClient, user_id: int, team_id: int) -> None:
    team = client.execute_kw(
        "crm.team",
        "read",
        [[int(team_id)]],
        {"fields": ["member_ids"]},
    )
    members = list((team[0].get("member_ids") if team else []) or [])
    uid = int(user_id)
    if uid in members:
        return
    members.append(uid)
    client.execute_kw(
        "crm.team",
        "write",
        [[int(team_id)], {"member_ids": [(6, 0, members)]}],
    )


def ensure_setter(
    client: OdooCRMClient,
    *,
    create: bool = True,
) -> RepMapEntry:
    explicit = (os.getenv(ENV_SETTER_USER_ID) or "").strip()
    name = (os.getenv(ENV_SETTER_NAME) or DEFAULT_SETTER_NAME).strip()
    login = (os.getenv(ENV_SETTER_LOGIN) or DEFAULT_SETTER_LOGIN).strip()
    entry = RepMapEntry(name=name, login=login, role="setter")
    odoo_id = None
    if explicit.isdigit():
        odoo_id = int(explicit)
    user = find_user(client, odoo_id=odoo_id, name=name, login=login)
    if user:
        entry.odoo_id = int(user["id"])
        entry.login = str(user.get("login") or login)
        entry.name = str(user.get("name") or name)
        return entry
    if not create:
        entry.error = "setter user not found"
        return entry
    try:
        created = create_internal_user(client, name=name, login=login)
        entry.odoo_id = int(created["id"])
        entry.login = str(created.get("login") or login)
        entry.created = True
    except Exception as exc:
        # Do NOT fall back to API uid when that user is also a sales closer
        # (would pull them out of Round Robin as "setter").
        entry.error = _short_err(exc)
    return entry


def ensure_rep(
    client: OdooCRMClient,
    rep: SalesRep,
    *,
    branch: str,
    team_id: int | None,
    create: bool = True,
) -> RepMapEntry:
    role = "desk" if _is_desk(rep) else "closer"
    entry = RepMapEntry(
        name=rep.name or (normalize_rep_phone(rep.phone) or "Rep"),
        phone=normalize_rep_phone(rep.phone) or rep.phone,
        odoo_id=rep.odoo_id,
        branch=normalize_crm_branch(branch),
        role=role,
    )
    if role == "desk":
        # Desk is WhatsApp routing only — never a CRM closer user.
        return entry

    user = find_user(
        client,
        odoo_id=rep.odoo_id,
        name=rep.name,
        phone=rep.phone,
    )
    if user:
        entry.odoo_id = int(user["id"])
        entry.login = str(user.get("login") or "")
        entry.name = str(user.get("name") or entry.name)
        if team_id and entry.odoo_id:
            try:
                link_user_to_team(client, entry.odoo_id, int(team_id))
            except Exception as exc:
                entry.error = f"team link failed: {exc}"
        return entry

    if not create:
        entry.error = "user not found"
        return entry

    login = _slug_login(entry.name, branch)
    try:
        created = create_internal_user(
            client,
            name=entry.name,
            login=login,
            phone=entry.phone,
            team_id=team_id,
        )
        entry.odoo_id = int(created["id"])
        entry.login = str(created.get("login") or login)
        entry.created = True
    except Exception as exc:
        entry.error = _short_err(exc)
    return entry


def load_mapping(path: Path | str | None = None) -> dict[str, Any]:
    p = Path(path or DEFAULT_MAPPING_PATH)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_mapping(mapping: dict[str, Any], path: Path | str | None = None) -> Path:
    p = Path(path or DEFAULT_MAPPING_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(mapping, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


def setup_odoo_structure(
    *,
    client: OdooCRMClient | None = None,
    create_missing: bool = True,
    mapping_path: Path | str | None = None,
) -> StructureResult:
    """Validate/create teams + users; write ``data/odoo_mapping.json``."""
    crm = client or OdooCRMClient()
    if crm.uid is None:
        crm.authenticate()

    result = StructureResult()
    for branch in (PRIMARY_BRANCH, PLACEHOLDER_BRANCH):
        try:
            team_id, created = ensure_team(crm, branch, create=create_missing)
        except Exception as exc:
            result.warnings.append(f"team {branch}: {exc}")
            team_id, created = None, False
        if team_id:
            result.teams[branch] = int(team_id)
            if created:
                result.warnings.append(f"created team {branch} id={team_id}")
        else:
            result.warnings.append(f"missing team for {branch}")

    result.setter = ensure_setter(crm, create=create_missing)
    if result.setter and result.setter.error:
        result.warnings.append(f"setter: {result.setter.error}")

    roster = load_branch_reps()
    setter_id = result.setter.odoo_id if result.setter else None
    for branch, reps in roster.items():
        team_id = result.teams.get(branch)
        entries: list[RepMapEntry] = []
        for rep in reps:
            entry = ensure_rep(
                crm,
                rep,
                branch=branch,
                team_id=team_id,
                create=create_missing and not _is_desk(rep),
            )
            # Never treat setter as a closer even if listed in REPS_*.
            if (
                setter_id
                and entry.odoo_id
                and int(entry.odoo_id) == int(setter_id)
                and entry.role == "closer"
            ):
                entry.role = "setter"
                result.warnings.append(
                    f"excluded setter user_id={setter_id} from closer pool ({branch})"
                )
            if entry.error:
                result.warnings.append(
                    f"{branch}/{entry.name}: {entry.error}"
                )
            entries.append(entry)
        result.reps[branch] = entries

    path = save_mapping(result.as_mapping(), mapping_path)
    result.mapping_path = str(path)

    # Soft-sync env hints (print only — never rewrite .env automatically).
    for branch, env_name in (
        (PRIMARY_BRANCH, ENV_TEAM_PERIFERICO),
        (PLACEHOLDER_BRANCH, ENV_TEAM_SAN_FELIPE),
    ):
        tid = result.teams.get(branch)
        if tid:
            print(f"HINT set {env_name}={tid}", flush=True)

    return result


def closer_odoo_ids_from_mapping(
    branch: str,
    *,
    mapping: dict[str, Any] | None = None,
) -> list[int]:
    data = mapping if mapping is not None else load_mapping()
    key = normalize_crm_branch(branch)
    ids = (data.get("closer_user_ids") or {}).get(key) or []
    out: list[int] = []
    for raw in ids:
        try:
            n = int(raw)
        except (TypeError, ValueError):
            continue
        if n > 0:
            out.append(n)
    return out


def setter_user_id_from_mapping(
    *,
    mapping: dict[str, Any] | None = None,
) -> int | None:
    data = mapping if mapping is not None else load_mapping()
    raw = data.get("setter_user_id")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


@dataclass
class EmployeeSyncEntry:
    user_id: int
    name: str
    work_email: str = ""
    employee_id: int | None = None
    created: bool = False
    skipped: bool = False
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EmployeeSyncResult:
    employees: list[EmployeeSyncEntry] = field(default_factory=list)
    created: int = 0
    existing: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "existing": self.existing,
            "errors": self.errors,
            "employees": [e.as_dict() for e in self.employees],
        }


def _mapped_closer_users(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten closer reps (and optional setter) that have an odoo user id."""
    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    for branch_entries in (mapping.get("reps") or {}).values():
        if not isinstance(branch_entries, list):
            continue
        for entry in branch_entries:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("role") or "") != "closer":
                continue
            try:
                uid = int(entry.get("odoo_id"))
            except (TypeError, ValueError):
                continue
            if uid <= 0 or uid in seen:
                continue
            seen.add(uid)
            out.append(entry)
    return out


def sync_hr_employees(
    *,
    client: OdooCRMClient | None = None,
    mapping: dict[str, Any] | None = None,
    mapping_path: Path | str | None = None,
    create_missing: bool = True,
    include_setter: bool = False,
) -> EmployeeSyncResult:
    """Ensure ``hr.employee`` exists for each closer in ``odoo_mapping.json``.

    Links ``name``, ``work_email`` (from ``res.users.login`` / email), and ``user_id``.
    """
    crm = client or OdooCRMClient()
    if crm.uid is None:
        crm.authenticate()

    data = mapping if mapping is not None else load_mapping(mapping_path)
    if not data:
        return EmployeeSyncResult()

    targets = _mapped_closer_users(data)
    if include_setter:
        setter = data.get("setter") if isinstance(data.get("setter"), dict) else None
        try:
            sid = int((setter or {}).get("odoo_id") or data.get("setter_user_id"))
        except (TypeError, ValueError):
            sid = 0
        if sid > 0 and sid not in {int(t.get("odoo_id") or 0) for t in targets}:
            targets.append(
                setter
                or {
                    "odoo_id": sid,
                    "name": "Marco",
                    "login": "",
                    "role": "setter",
                }
            )

    result = EmployeeSyncResult()
    for entry in targets:
        uid = int(entry["odoo_id"])
        name = str(entry.get("name") or "").strip() or f"User {uid}"
        login = str(entry.get("login") or "").strip()
        row = EmployeeSyncEntry(user_id=uid, name=name, work_email=login)

        try:
            existing = crm.execute_kw(
                "hr.employee",
                "search_read",
                [[["user_id", "=", uid]]],
                {"fields": ["id", "name", "work_email", "user_id"], "limit": 1},
            )
        except Exception as exc:
            row.error = _short_err(exc)
            result.errors += 1
            result.employees.append(row)
            continue

        if existing:
            row.employee_id = int(existing[0]["id"])
            row.work_email = str(
                existing[0].get("work_email") or login or ""
            )
            row.name = str(existing[0].get("name") or name)
            row.skipped = True
            result.existing += 1
            result.employees.append(row)
            continue

        if not create_missing:
            row.error = "missing hr.employee"
            result.errors += 1
            result.employees.append(row)
            continue

        # Prefer live user name / login for email.
        try:
            users = crm.execute_kw(
                "res.users",
                "read",
                [[uid]],
                {"fields": ["id", "name", "login", "email"]},
            )
            if users:
                name = str(users[0].get("name") or name).strip() or name
                login = (
                    str(users[0].get("email") or users[0].get("login") or login)
                    .strip()
                )
                row.name = name
                row.work_email = login
        except Exception:
            pass

        vals: dict[str, Any] = {
            "name": name,
            "user_id": uid,
        }
        if login and "@" in login:
            vals["work_email"] = login
        try:
            emp_id = int(crm.execute_kw("hr.employee", "create", [vals]))
            row.employee_id = emp_id
            row.created = True
            result.created += 1
        except Exception as exc:
            row.error = _short_err(exc)
            result.errors += 1
        result.employees.append(row)

    # Persist employee ids back into mapping when path known.
    path = Path(mapping_path) if mapping_path else (
        Path(DEFAULT_MAPPING_PATH) if (mapping is None) else None
    )
    if path is not None and data:
        by_user = {
            int(e.user_id): int(e.employee_id)
            for e in result.employees
            if e.employee_id
        }
        data["employees"] = {
            str(uid): emp_id for uid, emp_id in sorted(by_user.items())
        }
        for branch_entries in (data.get("reps") or {}).values():
            if not isinstance(branch_entries, list):
                continue
            for rep in branch_entries:
                if not isinstance(rep, dict):
                    continue
                try:
                    uid = int(rep.get("odoo_id"))
                except (TypeError, ValueError):
                    continue
                if uid in by_user:
                    rep["employee_id"] = by_user[uid]
        setter = data.get("setter")
        if isinstance(setter, dict):
            try:
                sid = int(setter.get("odoo_id"))
            except (TypeError, ValueError):
                sid = 0
            if sid in by_user:
                setter["employee_id"] = by_user[sid]
        try:
            save_mapping(data, path)
        except OSError:
            pass

    return result


__all__ = [
    "DEFAULT_MAPPING_PATH",
    "EmployeeSyncResult",
    "StructureResult",
    "closer_odoo_ids_from_mapping",
    "ensure_setter",
    "ensure_team",
    "load_mapping",
    "save_mapping",
    "setter_user_id_from_mapping",
    "setup_odoo_structure",
    "sync_hr_employees",
]
