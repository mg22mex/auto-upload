"""Fleet vehicle lookup and lead VIN mapping."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class _OdooSession(Protocol):
    dry_run: bool

    def execute_kw(
        self,
        model: str,
        method: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> Any: ...

    def _use_dry_run(self, dry_run: bool | None) -> bool: ...


def _m2o_label(value: Any) -> str:
    if isinstance(value, (list, tuple)) and len(value) > 1:
        return str(value[1] or "")
    if isinstance(value, str):
        return value
    return ""


def _extract_location_label(row: dict[str, Any]) -> str:
    """Best-effort human location string from a fleet.vehicle row."""
    for key in (
        "x_studio_ubicacion",
        "x_ubicacion",
        "x_branch",
        "x_sucursal",
        "x_studio_sucursal",
        "location",
        "location_id",
        "company_id",
        "name",
    ):
        raw = row.get(key)
        if raw in (None, False, ""):
            continue
        label = _m2o_label(raw) if not isinstance(raw, str) else raw
        label = str(label or "").strip()
        if label:
            return label
    return ""


@dataclass(frozen=True)
class FleetVehicle:
    id: int
    name: str
    vin_sn: str = ""
    license_plate: str = ""
    model_id: int | None = None
    model_name: str = ""
    driver_id: int | None = None
    location_label: str = ""
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class FleetLinkResult:
    ok: bool
    lead_id: int
    vehicle_id: int | None = None
    vin: str = ""
    linked_via: str = ""  # x_vin | chatter | description | dry_run
    dry_run: bool = False
    error: str | None = None


class FleetMixin:
    """Query ``fleet.vehicle`` and attach VIN metadata to ``crm.lead``."""

    FLEET_FIELDS = [
        "id",
        "name",
        "vin_sn",
        "license_plate",
        "model_id",
        "driver_id",
        # Location / branch (Studio custom fields vary by DB)
        "location",
        "location_id",
        "x_studio_ubicacion",
        "x_ubicacion",
        "x_branch",
        "x_sucursal",
        "x_studio_sucursal",
        "company_id",
    ]

    def find_fleet_vehicle_by_vin(self: _OdooSession, vin: str) -> FleetVehicle | None:
        """Lookup ``fleet.vehicle`` by ``vin_sn`` (exact then ilike)."""
        code = (vin or "").strip()
        if not code:
            return None
        try:
            rows = self._fleet_search_read([("vin_sn", "=", code)])  # type: ignore[attr-defined]
            if not rows:
                rows = self._fleet_search_read([("vin_sn", "ilike", code)])  # type: ignore[attr-defined]
            if not rows:
                return None
            return self._fleet_row_to_vehicle(rows[0])  # type: ignore[attr-defined]
        except Exception as exc:
            print(f"WARN find_fleet_vehicle_by_vin: {exc}")
            return None

    def find_fleet_vehicle_by_plate(
        self: _OdooSession,
        plate: str,
    ) -> FleetVehicle | None:
        """Lookup ``fleet.vehicle`` by license plate / stock plate."""
        code = (plate or "").strip()
        if not code:
            return None
        try:
            domain_opts = [
                [("license_plate", "=", code)],
                [("license_plate", "ilike", code)],
            ]
            rows: list[dict[str, Any]] = []
            for domain in domain_opts:
                rows = self._fleet_search_read(domain)  # type: ignore[attr-defined]
                if rows:
                    break
            if not rows:
                return None
            return self._fleet_row_to_vehicle(rows[0])  # type: ignore[attr-defined]
        except Exception as exc:
            print(f"WARN find_fleet_vehicle_by_plate: {exc}")
            return None

    def _fleet_search_read(
        self: _OdooSession,
        domain: list[Any],
    ) -> list[dict[str, Any]]:
        """search_read with progressive field drop if extras don't exist on DB."""
        fields: list[str] = list(self.FLEET_FIELDS)  # type: ignore[attr-defined]
        while fields:
            try:
                return self.execute_kw(
                    "fleet.vehicle",
                    "search_read",
                    [domain],
                    {"fields": fields, "limit": 1},
                )
            except Exception as exc:
                msg = str(exc).lower()
                if "invalid field" in msg or "unknown field" in msg or "does not exist" in msg:
                    # Drop optional location fields from the end until core works
                    if len(fields) <= 6:
                        raise
                    fields = fields[:-1]
                    continue
                raise
        return []

    def find_fleet_vehicle(
        self: _OdooSession,
        *,
        vin: str | None = None,
        plate: str | None = None,
    ) -> FleetVehicle | None:
        """Resolve fleet vehicle by VIN first, then plate/stock."""
        if vin:
            found = self.find_fleet_vehicle_by_vin(vin)  # type: ignore[attr-defined]
            if found:
                return found
        if plate:
            return self.find_fleet_vehicle_by_plate(plate)  # type: ignore[attr-defined]
        return None

    def _fleet_row_to_vehicle(self, row: dict[str, Any]) -> FleetVehicle:
        model_raw = row.get("model_id")
        model_id = None
        model_name = ""
        if isinstance(model_raw, (list, tuple)) and model_raw:
            model_id = int(model_raw[0])
            if len(model_raw) > 1:
                model_name = str(model_raw[1])
        driver_raw = row.get("driver_id")
        driver_id = (
            int(driver_raw[0])
            if isinstance(driver_raw, (list, tuple)) and driver_raw
            else (int(driver_raw) if driver_raw else None)
        )
        location_label = _extract_location_label(row)
        return FleetVehicle(
            id=int(row["id"]),
            name=str(row.get("name") or ""),
            vin_sn=str(row.get("vin_sn") or ""),
            license_plate=str(row.get("license_plate") or ""),
            model_id=model_id,
            model_name=model_name,
            driver_id=driver_id,
            location_label=location_label,
            raw=dict(row),
        )

    def link_fleet_vehicle_to_lead(
        self: _OdooSession,
        lead_id: int,
        *,
        vin: str | None = None,
        plate: str | None = None,
        vehicle_id: int | None = None,
        dry_run: bool | None = None,
    ) -> FleetLinkResult:
        """Attach VIN / fleet metadata to ``crm.lead`` (``x_vin`` or chatter)."""
        use_dry = self._use_dry_run(dry_run)
        vin_hint = (vin or "").strip()
        if use_dry:
            print(
                f"DRY-RUN link_fleet_vehicle_to_lead lead={lead_id} "
                f"vin={vin_hint!r} vehicle_id={vehicle_id}"
            )
            return FleetLinkResult(
                ok=True,
                lead_id=int(lead_id),
                vehicle_id=int(vehicle_id) if vehicle_id is not None else None,
                vin=vin_hint,
                linked_via="dry_run",
                dry_run=True,
            )

        vehicle: FleetVehicle | None = None
        if vehicle_id is not None:
            try:
                rows = self._fleet_search_read(  # type: ignore[attr-defined]
                    [("id", "=", int(vehicle_id))]
                )
                if rows:
                    vehicle = self._fleet_row_to_vehicle(rows[0])  # type: ignore[attr-defined]
            except Exception as exc:
                return FleetLinkResult(
                    ok=False,
                    lead_id=int(lead_id),
                    error=str(exc),
                    dry_run=False,
                )
        else:
            vehicle = self.find_fleet_vehicle(vin=vin, plate=plate)  # type: ignore[attr-defined]

        vin_value = (vin or (vehicle.vin_sn if vehicle else "") or "").strip()
        if not vin_value and vehicle is None:
            return FleetLinkResult(
                ok=False,
                lead_id=int(lead_id),
                error="vin, plate, or vehicle_id required",
            )

        try:
            # Prefer custom x_vin field
            try:
                self.execute_kw(
                    "crm.lead",
                    "write",
                    [[int(lead_id)], {"x_vin": vin_value}],
                )
                linked_via = "x_vin"
            except Exception:
                # Append to description
                linked_via = "description"
                rows = self.execute_kw(
                    "crm.lead",
                    "read",
                    [[int(lead_id)]],
                    {"fields": ["description"]},
                )
                prev = ""
                if rows:
                    prev = str(rows[0].get("description") or "")
                meta_lines = [
                    "",
                    "--- Fleet / VIN ---",
                    f"VIN: {vin_value or 'n/a'}",
                ]
                if vehicle:
                    meta_lines.append(f"Fleet vehicle: {vehicle.name} (id={vehicle.id})")
                    if vehicle.license_plate:
                        meta_lines.append(f"Plate: {vehicle.license_plate}")
                    if vehicle.model_name:
                        meta_lines.append(f"Model: {vehicle.model_name}")
                note = "\n".join(meta_lines)
                if f"VIN: {vin_value}" not in prev:
                    self.execute_kw(
                        "crm.lead",
                        "write",
                        [[int(lead_id)], {"description": (prev + note).strip()}],
                    )

                # Also try chatter when available on this client
                post = getattr(self, "post_quote_to_chatter", None)
                if callable(post):
                    try:
                        post(int(lead_id), note.strip())
                        linked_via = "chatter+description"
                    except Exception:
                        pass

            print(
                f"Linked fleet VIN={vin_value!r} to lead id={lead_id} "
                f"via {linked_via}"
            )
            return FleetLinkResult(
                ok=True,
                lead_id=int(lead_id),
                vehicle_id=vehicle.id if vehicle else None,
                vin=vin_value,
                linked_via=linked_via,
            )
        except Exception as exc:
            msg = str(exc)
            print(f"WARN link_fleet_vehicle_to_lead lead={lead_id}: {msg}")
            return FleetLinkResult(
                ok=False, lead_id=int(lead_id), vin=vin_value, error=msg
            )
