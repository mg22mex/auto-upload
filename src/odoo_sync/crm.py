"""CRM lead manager — payload-driven create/update with branch + fleet hooks.

Primary entrypoint for inbound channels (voice, Meta, manual tools) that need
a dict in/out API rather than the positional ``OdooCRMClient.create_or_update_lead``.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

from src.odoo_sync.base import OdooCRMError
from src.odoo_sync.client import OdooCRMClient

PRIMARY_BRANCH = "periferico"
PLACEHOLDER_BRANCH = "san_felipe"

ENV_TEAM_PERIFERICO = "ODOO_TEAM_PERIFERICO"
ENV_TEAM_SAN_FELIPE = "ODOO_TEAM_SAN_FELIPE"
ENV_SOURCE_ID = "ODOO_CRM_SOURCE_ID"
ENV_MEDIUM_ID = "ODOO_CRM_MEDIUM_ID"

BRANCH_TEAM_ENV: dict[str, str] = {
    PRIMARY_BRANCH: ENV_TEAM_PERIFERICO,
    PLACEHOLDER_BRANCH: ENV_TEAM_SAN_FELIPE,
}


def normalize_crm_branch(branch: str | None) -> str:
    key = (branch or PRIMARY_BRANCH).strip().lower().replace(" ", "_")
    if key in {
        "periferico",
        "periférico",
        "primary",
        "default",
        "periferico_sur",
    }:
        return PRIMARY_BRANCH
    if key in {"san_felipe", "san-felipe", "sanfelipe", "san_felipe_sur"}:
        return PLACEHOLDER_BRANCH
    return key or PRIMARY_BRANCH


def parse_team_id(raw: str | None) -> int | None:
    """Parse env team id; empty / None / 0 → None (unset)."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text.lower() in {"none", "null"}:
        return None
    try:
        value = int(text)
    except ValueError:
        return None
    return value if value > 0 else None


def load_branch_teams() -> dict[str, int | None]:
    return {
        PRIMARY_BRANCH: parse_team_id(os.getenv(ENV_TEAM_PERIFERICO)),
        PLACEHOLDER_BRANCH: parse_team_id(os.getenv(ENV_TEAM_SAN_FELIPE)),
    }


def resolve_team_id(
    branch: str = PRIMARY_BRANCH,
    *,
    teams: dict[str, int | None] | None = None,
) -> tuple[str, int | None, bool]:
    """Resolve (effective_branch, team_id, fell_back).

    Missing / 0 San Felipe team → soft fall back to Periférico.
    Missing Periférico → ``team_id=None`` (create without team; never raises).
    """
    requested = normalize_crm_branch(branch)
    table = teams if teams is not None else load_branch_teams()
    primary = table.get(PRIMARY_BRANCH)
    chosen = table.get(requested)

    if requested != PRIMARY_BRANCH and chosen is None:
        if primary is not None:
            print(
                f"WARN CRM branch {requested!r}: no team id; "
                f"falling back to primary {PRIMARY_BRANCH!r} "
                f"({ENV_TEAM_PERIFERICO}={primary})"
            )
            return PRIMARY_BRANCH, primary, True
        print(
            f"WARN CRM branch {requested!r}: no team ids configured; "
            f"creating/updating lead without team_id"
        )
        return PRIMARY_BRANCH, None, True

    if requested == PRIMARY_BRANCH and chosen is None:
        return PRIMARY_BRANCH, None, False

    return requested, chosen, False


def normalize_phone_digits(phone: str | None) -> str:
    return re.sub(r"\D", "", str(phone or ""))


def _optional_int(value: Any) -> int | None:
    if value is None or value is False or value == "":
        return None
    try:
        num = int(value)
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


def infer_physical_location(label: str | None) -> str | None:
    """Map free-text fleet/location labels to ``periferico`` / ``san_felipe``."""
    text = (label or "").strip().lower()
    if not text:
        return None
    text = (
        text.replace("á", "a")
        .replace("é", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ú", "u")
    )
    compact = text.replace(" ", "_").replace("-", "_")
    if "san_felipe" in compact or "sanfelipe" in compact:
        return PLACEHOLDER_BRANCH
    if "periferico" in compact:
        return PRIMARY_BRANCH
    return None


def apply_vehicle_location_team(
    inbound_branch: str,
    physical_location: str | None,
    *,
    teams: dict[str, int | None] | None = None,
) -> tuple[str, int | None, bool, bool]:
    """Resolve team with optional San Felipe physical-location override.

    Returns (effective_branch, team_id, fell_back, location_overrode).
    """
    table = teams if teams is not None else load_branch_teams()
    inbound, team_id, fell_back = resolve_team_id(inbound_branch, teams=table)
    location_overrode = False

    if physical_location == PLACEHOLDER_BRANCH:
        sf_team = table.get(PLACEHOLDER_BRANCH)
        if sf_team is not None:
            return PLACEHOLDER_BRANCH, sf_team, False, True
        # San Felipe location known but team unset — keep inbound; still flip branch label
        print(
            f"WARN CRM physical location is {PLACEHOLDER_BRANCH!r} but "
            f"{ENV_TEAM_SAN_FELIPE} unset; team stays {inbound!r}/{team_id}"
        )
        return PLACEHOLDER_BRANCH, team_id, fell_back, True

    # periferico or unknown → inbound branch parameter logic
    return inbound, team_id, fell_back, location_overrode


@dataclass
class CRMLeadManager:
    """Dict-payload CRM lead upsert with branch teams + fleet VIN."""

    client: OdooCRMClient | None = None
    dry_run: bool | None = None
    _client: OdooCRMClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._client = self.client or OdooCRMClient()
        if self.dry_run is not None:
            self._client.dry_run = bool(self.dry_run)

    @property
    def odoo(self) -> OdooCRMClient:
        return self._client

    def _use_dry_run(self) -> bool:
        return self._client._use_dry_run(self.dry_run)

    def create_or_update_lead(
        self,
        payload: dict[str, Any],
        branch: str = PRIMARY_BRANCH,
    ) -> dict[str, Any]:
        """Create or update a lead from a free-form payload.

        Returns a compact result dict::

            {
              "status": "created" | "updated",
              "lead_id": int,
              "deduplicated": bool,
              "branch": str,
              "team_id": int | None,
              "fell_back": bool,
              "fleet": {...} | None,
              "dry_run": bool,
            }
        """
        if not isinstance(payload, dict):
            raise OdooCRMError("payload must be a dict")

        client_name = str(
            payload.get("client_name")
            or payload.get("name")
            or payload.get("contact_name")
            or ""
        ).strip()
        phone_raw = str(payload.get("phone") or payload.get("mobile") or "").strip()
        phone_digits = normalize_phone_digits(phone_raw)
        if not client_name:
            raise OdooCRMError("payload requires client_name or name")
        if not phone_digits:
            raise OdooCRMError("payload requires phone")

        vehicle_info = str(
            payload.get("vehicle_info")
            or payload.get("vehicle_name")
            or payload.get("vehicle")
            or ""
        ).strip()
        vehicle_label = vehicle_info or "General"
        title = f"Consulta: {vehicle_label} - {client_name}"
        if len(title) > 128:
            title = title[:125] + "..."

        email = str(
            payload.get("email_from")
            or payload.get("email")
            or ""
        ).strip() or False
        notes = str(
            payload.get("description")
            or payload.get("notes")
            or payload.get("quote_summary")
            or ""
        ).strip()
        channel = str(payload.get("channel") or "").strip()

        physical_location, fleet_preview = self._resolve_physical_location(payload)
        effective_branch, team_id, fell_back, location_overrode = (
            apply_vehicle_location_team(branch, physical_location)
        )

        description = self._build_description(
            client_name=client_name,
            vehicle_info=vehicle_label,
            phone=phone_digits,
            email=email or "",
            channel=channel,
            notes=notes,
            payload=payload,
            physical_location=physical_location,
        )

        source_id = _optional_int(
            payload.get("source_id") or os.getenv(ENV_SOURCE_ID)
        )
        medium_id = _optional_int(
            payload.get("medium_id") or os.getenv(ENV_MEDIUM_ID)
        )

        use_dry = self._use_dry_run()
        if use_dry:
            print(
                f"DRY-RUN CRMLeadManager.create_or_update_lead "
                f"title={title!r} phone={phone_digits} "
                f"branch={effective_branch!r} team_id={team_id} "
                f"physical_location={physical_location!r} "
                f"location_overrode={location_overrode} fell_back={fell_back}"
            )
            vin = str(payload.get("vin") or payload.get("vin_sn") or "").strip()
            plate = str(
                payload.get("plate") or payload.get("license_plate") or ""
            ).strip()
            fleet_meta = None
            if vin or plate or payload.get("fleet_vehicle_id") is not None:
                fleet_meta = {
                    "status": "dry_run",
                    "vin": vin,
                    "plate": plate,
                    "linked_via": "dry_run",
                    "physical_location": physical_location,
                    **(fleet_preview or {}),
                }
            return {
                "status": "created",
                "lead_id": -1,
                "deduplicated": False,
                "branch": effective_branch,
                "team_id": team_id,
                "fell_back": fell_back,
                "location_overrode": location_overrode,
                "physical_location": physical_location,
                "title": title,
                "phone": phone_digits,
                "fleet": fleet_meta,
                "dry_run": True,
            }

        existing_id = self._find_active_lead_id(phone_digits, phone_raw)

        if existing_id is not None:
            chatter_body = self._inquiry_chatter_body(
                title=title,
                vehicle_info=vehicle_label,
                client_name=client_name,
                description=description,
            )
            try:
                self._client.post_quote_to_chatter(int(existing_id), chatter_body)
            except Exception as exc:
                print(
                    f"WARN CRMLeadManager chatter on lead {existing_id}: {exc}"
                )
            try:
                update_vals: dict[str, Any] = {
                    "contact_name": client_name,
                    "phone": phone_digits,
                    "description": description,
                }
                if email:
                    update_vals["email_from"] = email
                if team_id is not None:
                    update_vals["team_id"] = int(team_id)
                self._client.execute_kw(
                    "crm.lead",
                    "write",
                    [[int(existing_id)], update_vals],
                )
            except Exception as exc:
                print(f"WARN CRMLeadManager update lead {existing_id}: {exc}")

            fleet_meta = self._maybe_link_fleet(int(existing_id), payload)
            if fleet_meta is not None and physical_location:
                fleet_meta = {**fleet_meta, "physical_location": physical_location}
            return {
                "status": "updated",
                "lead_id": int(existing_id),
                "deduplicated": True,
                "branch": effective_branch,
                "team_id": team_id,
                "fell_back": fell_back,
                "location_overrode": location_overrode,
                "physical_location": physical_location,
                "title": title,
                "phone": phone_digits,
                "fleet": fleet_meta,
                "dry_run": False,
            }

        vals: dict[str, Any] = {
            "name": title,
            "contact_name": client_name,
            "partner_name": client_name,
            "phone": phone_digits,
            "description": description,
            "type": "opportunity",
        }
        if email:
            vals["email_from"] = email
        if team_id is not None:
            vals["team_id"] = int(team_id)
        if source_id is not None:
            vals["source_id"] = int(source_id)
        if medium_id is not None:
            vals["medium_id"] = int(medium_id)

        lead_id = self._create_lead_with_fallbacks(vals)
        fleet_meta = self._maybe_link_fleet(int(lead_id), payload)
        if fleet_meta is not None and physical_location:
            fleet_meta = {**fleet_meta, "physical_location": physical_location}
        print(
            f"CRMLeadManager created lead id={lead_id} branch={effective_branch} "
            f"team_id={team_id} physical_location={physical_location} "
            f"location_overrode={location_overrode}"
        )
        return {
            "status": "created",
            "lead_id": int(lead_id),
            "deduplicated": False,
            "branch": effective_branch,
            "team_id": team_id,
            "fell_back": fell_back,
            "location_overrode": location_overrode,
            "physical_location": physical_location,
            "title": title,
            "phone": phone_digits,
            "fleet": fleet_meta,
            "dry_run": False,
        }

    def _resolve_physical_location(
        self,
        payload: dict[str, Any],
    ) -> tuple[str | None, dict[str, Any] | None]:
        """Return (physical_location branch key | None, fleet preview meta)."""
        vin = str(payload.get("vin") or payload.get("vin_sn") or "").strip()
        plate = str(
            payload.get("plate") or payload.get("license_plate") or ""
        ).strip()
        fleet_vehicle_id = _optional_int(payload.get("fleet_vehicle_id"))
        explicit = str(
            payload.get("physical_location")
            or payload.get("vehicle_location")
            or payload.get("ubicacion")
            or ""
        ).strip()
        if explicit:
            return infer_physical_location(explicit) or normalize_crm_branch(explicit), {
                "source": "payload"
            }

        if not (vin or plate or fleet_vehicle_id is not None):
            return None, None

        if self._use_dry_run():
            # Dry-run without explicit location cannot hit Odoo; leave unknown.
            return None, {"source": "dry_run_no_lookup"}

        vehicle = None
        try:
            if fleet_vehicle_id is not None:
                rows = self._client._fleet_search_read(  # type: ignore[attr-defined]
                    [("id", "=", int(fleet_vehicle_id))]
                )
                if rows:
                    vehicle = self._client._fleet_row_to_vehicle(rows[0])  # type: ignore[attr-defined]
            if vehicle is None:
                vehicle = self._client.find_fleet_vehicle(vin=vin or None, plate=plate or None)
        except Exception as exc:
            print(f"WARN CRMLeadManager fleet location lookup: {exc}")
            vehicle = None

        if vehicle is None:
            return None, {"source": "fleet_not_found"}

        label = vehicle.location_label or vehicle.name or ""
        if vehicle.raw:
            # Concatenate more fields for keyword scan
            extras = []
            for key, val in vehicle.raw.items():
                if isinstance(val, str):
                    extras.append(val)
                elif isinstance(val, (list, tuple)) and len(val) > 1:
                    extras.append(str(val[1]))
            if extras:
                label = " ".join([label, *extras])
        loc = infer_physical_location(label)
        return loc, {
            "source": "fleet",
            "vehicle_id": vehicle.id,
            "location_label": vehicle.location_label or None,
        }

    def _create_lead_with_fallbacks(self, vals: dict[str, Any]) -> int:
        attempt = dict(vals)
        last_exc: BaseException | None = None
        optional_keys = (
            "partner_name",
            "email_from",
            "source_id",
            "medium_id",
            "team_id",
            "type",
        )
        # Progressive drop of optional fields that some DBs lack.
        drop_sets: list[tuple[str, ...]] = [()]
        for i in range(len(optional_keys)):
            drop_sets.append(optional_keys[: i + 1])
        for drop in drop_sets:
            trial = dict(attempt)
            for key in drop:
                trial.pop(key, None)
            try:
                return int(self._client.execute_kw("crm.lead", "create", [trial]))
            except Exception as exc:
                last_exc = exc
                continue
        raise OdooCRMError(
            f"lead create failed after field fallbacks: {last_exc}"
        ) from last_exc

    def _find_active_lead_id(
        self,
        phone_digits: str,
        phone_raw: str,
    ) -> int | None:
        candidates = []
        if phone_digits:
            candidates.append(phone_digits)
            if len(phone_digits) >= 10:
                candidates.append(phone_digits[-10:])
        if phone_raw and phone_raw not in candidates:
            candidates.append(phone_raw.strip())

        seen: set[str] = set()
        for candidate in candidates:
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            domain: list[Any] = [
                ("phone", "=", candidate),
                ("active", "=", True),
            ]
            try:
                found = self._client.execute_kw(
                    "crm.lead",
                    "search",
                    [domain],
                    {"limit": 1, "order": "write_date desc, id desc"},
                )
            except Exception:
                try:
                    found = self._client.execute_kw(
                        "crm.lead",
                        "search",
                        [[("phone", "=", candidate)]],
                        {"limit": 1, "order": "id desc"},
                    )
                except Exception as exc:
                    print(f"WARN CRMLeadManager search phone={candidate!r}: {exc}")
                    found = []
            if found:
                return int(found[0])

            # Partial match on last 10 digits when exact fails
            if len(candidate) >= 10:
                tail = candidate[-10:]
                try:
                    found = self._client.execute_kw(
                        "crm.lead",
                        "search",
                        [[("phone", "ilike", tail), ("active", "=", True)]],
                        {"limit": 1, "order": "write_date desc, id desc"},
                    )
                    if found:
                        return int(found[0])
                except Exception:
                    pass
        return None

    def _maybe_link_fleet(
        self,
        lead_id: int,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        vin = str(payload.get("vin") or payload.get("vin_sn") or "").strip()
        plate = str(
            payload.get("plate") or payload.get("license_plate") or ""
        ).strip()
        fleet_vehicle_id = payload.get("fleet_vehicle_id")
        if not (vin or plate or fleet_vehicle_id is not None):
            return None
        if bool(payload.get("link_fleet", True)) is False:
            return {"status": "skipped", "reason": "link_fleet=false"}

        try:
            result = self._client.link_fleet_vehicle_to_lead(
                lead_id,
                vin=vin or None,
                plate=plate or None,
                vehicle_id=_optional_int(fleet_vehicle_id),
                dry_run=self._use_dry_run(),
            )
            return {
                "status": "ok" if result.ok else "error",
                "vehicle_id": result.vehicle_id,
                "vin": result.vin,
                "linked_via": result.linked_via,
                "dry_run": result.dry_run,
                "error": result.error,
            }
        except Exception as exc:
            print(f"WARN CRMLeadManager fleet link lead={lead_id}: {exc}")
            return {"status": "error", "error": str(exc)}

    @staticmethod
    def _build_description(
        *,
        client_name: str,
        vehicle_info: str,
        phone: str,
        email: str,
        channel: str,
        notes: str,
        payload: dict[str, Any],
        physical_location: str | None = None,
    ) -> str:
        lines = [
            f"Contact: {client_name}",
            f"Phone: {phone}",
            f"Vehicle interest: {vehicle_info}",
        ]
        if email:
            lines.append(f"Email: {email}")
        if channel:
            lines.append(f"Channel: {channel}")
        sku = str(payload.get("sku") or payload.get("autosell_id") or "").strip()
        if sku:
            lines.append(f"SKU: {sku}")
        vin = str(payload.get("vin") or payload.get("vin_sn") or "").strip()
        if vin:
            lines.append(f"VIN: {vin}")
        plate = str(
            payload.get("plate") or payload.get("license_plate") or ""
        ).strip()
        if plate:
            lines.append(f"Plate: {plate}")
        has_unit = bool(
            vin
            or plate
            or payload.get("fleet_vehicle_id") is not None
            or physical_location
        )
        if has_unit:
            loc_note = physical_location or "desconocida"
            lines.append(f"Ubicación Física del Vehículo: {loc_note}")
        if notes:
            lines.extend(["", "--- Notes ---", notes])
        return "\n".join(lines)

    @staticmethod
    def _inquiry_chatter_body(
        *,
        title: str,
        vehicle_info: str,
        client_name: str,
        description: str,
    ) -> str:
        return (
            f"New inquiry (deduplicated)\n"
            f"Title: {title}\n"
            f"Client: {client_name}\n"
            f"Vehicle: {vehicle_info}\n\n"
            f"{description}"
        )


__all__ = [
    "BRANCH_TEAM_ENV",
    "CRMLeadManager",
    "ENV_MEDIUM_ID",
    "ENV_SOURCE_ID",
    "ENV_TEAM_PERIFERICO",
    "ENV_TEAM_SAN_FELIPE",
    "PLACEHOLDER_BRANCH",
    "PRIMARY_BRANCH",
    "apply_vehicle_location_team",
    "infer_physical_location",
    "load_branch_teams",
    "normalize_crm_branch",
    "normalize_phone_digits",
    "parse_team_id",
    "resolve_team_id",
]
