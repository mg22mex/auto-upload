"""Evolution instance → CRM branch / Odoo team routing."""
from __future__ import annotations

import os
from typing import Any

from src.odoo_sync.crm import (
    PLACEHOLDER_BRANCH,
    PRIMARY_BRANCH,
    load_branch_teams,
    normalize_crm_branch,
    resolve_team_id,
)
from src.whatsapp_worker.client import _first_env

ENV_INSTANCE_PERIFERICO = "WHATSAPP_INSTANCE_PERIFERICO"
ENV_INSTANCE_SAN_FELIPE = "WHATSAPP_INSTANCE_SAN_FELIPE"

BRANCH_LABELS = {
    PRIMARY_BRANCH: "Periférico",
    PLACEHOLDER_BRANCH: "San Felipe",
}


def instance_name_periferico() -> str:
    return _first_env(ENV_INSTANCE_PERIFERICO, default="autosell_periferico")


def instance_name_san_felipe() -> str:
    return _first_env(ENV_INSTANCE_SAN_FELIPE, default="autosell_san_felipe")


def _normalize_instance(name: str) -> str:
    return (name or "").strip().lower()


def branch_key_for_instance(instance: str) -> str:
    """Map Evolution ``instance`` name to ``periferico`` or ``san_felipe``."""
    inst = _normalize_instance(instance)
    if not inst:
        return PRIMARY_BRANCH
    if inst == _normalize_instance(instance_name_san_felipe()):
        return PLACEHOLDER_BRANCH
    if inst == _normalize_instance(instance_name_periferico()):
        return PRIMARY_BRANCH
    if "san_felipe" in inst or "sanfelipe" in inst or inst.endswith("_sf"):
        return PLACEHOLDER_BRANCH
    if "periferico" in inst or inst.endswith("_peri"):
        return PRIMARY_BRANCH
    return PRIMARY_BRANCH


def resolve_instance_for_branch(branch: str | None = None) -> str:
    """Outbound Evolution instance for a CRM branch key."""
    key = normalize_crm_branch(branch)
    if key == PLACEHOLDER_BRANCH:
        return instance_name_san_felipe()
    return instance_name_periferico()


def branch_context_for_instance(instance: str) -> dict[str, Any]:
    """Build branch / location / team context from an Evolution instance name."""
    branch_key = branch_key_for_instance(instance)
    teams = load_branch_teams()
    effective_branch, team_id, fell_back = resolve_team_id(branch_key, teams=teams)
    ctx: dict[str, Any] = {
        # Keep WhatsApp line identity even if Odoo team falls back to Periférico.
        "branch": branch_key,
        "physical_location": BRANCH_LABELS.get(branch_key, "Periférico"),
        "whatsapp_instance": instance or resolve_instance_for_branch(branch_key),
        "branch_fell_back": fell_back,
        "crm_branch_effective": effective_branch,
    }
    if team_id is not None:
        ctx["branch_id"] = team_id
    return ctx


def apply_whatsapp_branch_context(lead_data: dict[str, Any], instance: str) -> dict[str, Any]:
    """Merge instance-derived branch fields into pipeline lead_data (in place).

    Instance routing always wins over voice/default branch_id for WhatsApp lines.
    """
    ctx = branch_context_for_instance(instance)
    lead_data["branch"] = ctx["branch"]
    lead_data["physical_location"] = ctx["physical_location"]
    lead_data["whatsapp_instance"] = ctx["whatsapp_instance"]
    if "branch_id" in ctx:
        lead_data["branch_id"] = ctx["branch_id"]
    return lead_data
