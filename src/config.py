"""Runtime config for branch sales reps (round-robin lead distribution).

Rosters come from JSON env vars so a rep change never needs a deploy:

    REPS_PERIFERICO='[{"odoo_id": 2, "phone": "+526141234567", "name": "Ana"}]'
    REPS_SAN_FELIPE='[{"odoo_id": 5, "phone": "+526149876543"}]'
    DEFAULT_REP_PHONE='+526141234567'

Kept dependency-free (no Odoo / WhatsApp imports) so every layer can read it.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

PRIMARY_BRANCH = "periferico"
PLACEHOLDER_BRANCH = "san_felipe"

ENV_REPS_PERIFERICO = "REPS_PERIFERICO"
ENV_REPS_SAN_FELIPE = "REPS_SAN_FELIPE"
ENV_DEFAULT_REP_PHONE = "DEFAULT_REP_PHONE"

BRANCH_REPS_ENV: dict[str, str] = {
    PRIMARY_BRANCH: ENV_REPS_PERIFERICO,
    PLACEHOLDER_BRANCH: ENV_REPS_SAN_FELIPE,
}

# Catalog title markers (see src/inventory/snapshot.BRANCH_TAGS).
BRANCH_TAG_MAP: dict[str, str] = {
    "*": PRIMARY_BRANCH,
    "+": PLACEHOLDER_BRANCH,
    "-": PRIMARY_BRANCH,  # consignment stock is handled by the Periférico desk
}

BRANCH_LABELS: dict[str, str] = {
    PRIMARY_BRANCH: "Periférico",
    PLACEHOLDER_BRANCH: "San Felipe",
}


@dataclass(frozen=True)
class SalesRep:
    """One assignable sales rep. ``odoo_id`` maps to ``res.users``."""

    phone: str
    odoo_id: int | None = None
    name: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"odoo_id": self.odoo_id, "phone": self.phone, "name": self.name}


def normalize_rep_phone(phone: str | None) -> str:
    """E.164-ish MX phone: digits only, ``52`` country prefix, ``+`` restored."""
    digits = re.sub(r"\D", "", str(phone or ""))
    if not digits:
        return ""
    if len(digits) == 10:
        digits = f"52{digits}"
    return f"+{digits}"


def _coerce_odoo_id(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def parse_reps(raw: str | None) -> list[SalesRep]:
    """Parse a JSON rep roster. Malformed entries are dropped, never raised."""
    text = (raw or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        print("WARN reps roster: invalid JSON, ignoring", flush=True)
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []

    reps: list[SalesRep] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        phone = normalize_rep_phone(item.get("phone") or item.get("whatsapp"))
        odoo_id = _coerce_odoo_id(item.get("odoo_id") or item.get("user_id"))
        if not phone and odoo_id is None:
            continue
        reps.append(
            SalesRep(
                phone=phone,
                odoo_id=odoo_id,
                name=str(item.get("name") or "").strip(),
            )
        )
    return reps


def load_branch_reps() -> dict[str, list[SalesRep]]:
    """Current roster per branch, read fresh so env edits take effect."""
    return {
        branch: parse_reps(os.getenv(env_name))
        for branch, env_name in BRANCH_REPS_ENV.items()
    }


def default_rep_phone() -> str:
    """Fallback WhatsApp number when a branch has no configured rep."""
    explicit = normalize_rep_phone(os.getenv(ENV_DEFAULT_REP_PHONE))
    if explicit:
        return explicit
    # Fall back to the Periférico head (first rep of the primary roster).
    primary = parse_reps(os.getenv(ENV_REPS_PERIFERICO))
    return primary[0].phone if primary else ""


def branch_for_tag(tag: str | None) -> str:
    """Map a catalog branch marker (``*`` / ``+`` / ``-``) to a CRM branch key."""
    key = (tag or "").strip()
    if not key:
        return PRIMARY_BRANCH
    return BRANCH_TAG_MAP.get(key[-1], PRIMARY_BRANCH)


def branch_label(branch: str | None) -> str:
    return BRANCH_LABELS.get(str(branch or PRIMARY_BRANCH), BRANCH_LABELS[PRIMARY_BRANCH])


__all__ = [
    "BRANCH_LABELS",
    "BRANCH_REPS_ENV",
    "BRANCH_TAG_MAP",
    "ENV_DEFAULT_REP_PHONE",
    "ENV_REPS_PERIFERICO",
    "ENV_REPS_SAN_FELIPE",
    "PLACEHOLDER_BRANCH",
    "PRIMARY_BRANCH",
    "SalesRep",
    "branch_for_tag",
    "branch_label",
    "default_rep_phone",
    "load_branch_reps",
    "normalize_rep_phone",
    "parse_reps",
]
