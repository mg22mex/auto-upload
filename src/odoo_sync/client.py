"""Odoo CRM XML-RPC client — leads, round-robin advisors, chatter + extensions."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from src.odoo_sync.base import OdooClient, OdooCRMError
from src.odoo_sync.documents import DocumentsMixin
from src.odoo_sync.fleet import FleetMixin
from src.odoo_sync.whatsapp import WhatsAppMixin

__all__ = [
    "OdooCRMClient",
    "OdooCRMError",
    "QuoteLeadResult",
    "TestDriveEventResult",
]


@dataclass(frozen=True)
class QuoteLeadResult:
    """Result of quote lead upsert + follow-up scheduling."""

    lead_id: int
    activity_id: int | None = None
    tag_ids: tuple[int, ...] = field(default_factory=tuple)
    calendar_event_id: int | None = None


@dataclass(frozen=True)
class TestDriveEventResult:
    """Result of calendar.event booking for a test drive."""

    event_id: int | None
    lead_id: int
    stage_updated: bool = False
    activity_id: int | None = None
    partner_id: int | None = None
    dry_run: bool = False
    error: str | None = None


class OdooCRMClient(WhatsAppMixin, FleetMixin, DocumentsMixin, OdooClient):
    """CRM + WhatsApp + Fleet + Documents on one shared Odoo XML-RPC session."""

    TEST_DRIVE_STAGE = "Cita/Prueba de manejo"
    DEFAULT_ACTIVITY_SUMMARY = "Seguimiento post-cotización / Confirmación de Cita"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

    def search_vehicle_inventory(self, query_string: str) -> list[dict[str, Any]]:
        """Search vehicle product templates and return normalized inventory rows."""
        query = (query_string or "").strip()
        if not query:
            return []

        fields = ["id", "name", "list_price", "qty_available", "categ_id"]
        vehicle_domain: list[Any] = [
            ("name", "ilike", query),
            ("categ_id.name", "ilike", "vehicul"),
        ]
        records = self.execute_kw(
            "product.template",
            "search_read",
            [vehicle_domain],
            {"fields": fields, "limit": 20, "order": "id desc"},
        )
        if not records:
            records = self.execute_kw(
                "product.template",
                "search_read",
                [[("name", "ilike", query)]],
                {"fields": fields, "limit": 20, "order": "id desc"},
            )

        vehicles: list[dict[str, Any]] = []
        for record in records:
            category = record.get("categ_id")
            category_id = (
                int(category[0])
                if isinstance(category, (list, tuple)) and category
                else None
            )
            category_name = (
                str(category[1])
                if isinstance(category, (list, tuple)) and len(category) > 1
                else ""
            )
            vehicles.append(
                {
                    "id": int(record["id"]),
                    "name": str(record.get("name") or ""),
                    "list_price": float(record.get("list_price") or 0),
                    "qty_available": float(record.get("qty_available") or 0),
                    "categ_id": category_id,
                    "category_name": category_name,
                }
            )
        return vehicles

    def find_vehicle_category_id(self, name_ilike: str = "vehicul") -> int | None:
        """Resolve product.category id for vehicles (prefer 'vehiculos')."""
        rows = self.execute_kw(
            "product.category",
            "search_read",
            [[("name", "ilike", name_ilike)]],
            {"fields": ["id", "name"], "limit": 20},
        )
        if not rows:
            return None
        for row in rows:
            if str(row.get("name") or "").strip().lower() == "vehiculos":
                return int(row["id"])
        for row in rows:
            if "vehicul" in str(row.get("name") or "").lower():
                return int(row["id"])
        return int(rows[0]["id"])

    def find_product_template(
        self,
        *,
        default_code: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any] | None:
        """Find product.template by SKU (`default_code` / autosell_id).

        Name match only when no SKU provided (manual lookups).
        """
        fields = ["id", "name", "list_price", "qty_available", "categ_id", "default_code"]
        if default_code:
            # Include archived so re-listed SKUs can be reactivated (active=True).
            try:
                rows = self.execute_kw(
                    "product.template",
                    "search_read",
                    [[("default_code", "=", default_code)]],
                    {
                        "fields": fields,
                        "limit": 1,
                        "context": {"active_test": False},
                    },
                )
            except Exception:
                rows = []
            return rows[0] if rows else None
        if name:
            rows = self.execute_kw(
                "product.template",
                "search_read",
                [[("name", "=ilike", name)]],
                {"fields": fields, "limit": 1, "order": "id desc"},
            )
            return rows[0] if rows else None
        return None

    # Inventory status labels (website gone = completed sale).
    VEHICLE_STATE_FIELDS = (
        "x_studio_state",
        "x_studio_estatus",
        "x_vehicle_state",
        "state",
    )
    VEHICLE_STATE_SOLD = ("sold", "Sold", "Vendido", "vendido")
    VEHICLE_STATE_AVAILABLE = ("available", "Available", "Disponible", "disponible")

    def _write_product_inventory_status(
        self,
        product_id: int,
        *,
        inventory_status: str,
        active: bool,
    ) -> dict[str, Any]:
        """Write active flag + best-effort sold/available state field.

        Tries Studio/custom selection fields with EN/ES labels; falls back to
        ``active`` only when no status field is accepted by the database.
        """
        status = (inventory_status or "").strip().lower()
        labels = (
            self.VEHICLE_STATE_SOLD
            if status == "sold"
            else self.VEHICLE_STATE_AVAILABLE
        )
        last_exc: BaseException | None = None
        for field in self.VEHICLE_STATE_FIELDS:
            for label in labels:
                vals = {"active": bool(active), field: label}
                try:
                    self.execute_kw(
                        "product.template",
                        "write",
                        [[int(product_id)], vals],
                    )
                    return {
                        "active": bool(active),
                        "state_field": field,
                        "state_value": label,
                    }
                except Exception as exc:
                    last_exc = exc
                    continue
        try:
            self.execute_kw(
                "product.template",
                "write",
                [[int(product_id)], {"active": bool(active)}],
            )
            return {"active": bool(active), "state_field": None, "state_value": None}
        except Exception as exc:
            raise OdooCRMError(
                f"product status write failed id={product_id}: {exc or last_exc}"
            ) from exc

    def upsert_vehicle_product(
        self,
        *,
        name: str,
        list_price: float,
        default_code: str,
        categ_id: int | None = None,
        description: str = "",
        qty_available: float = 1.0,
    ) -> dict[str, Any]:
        """Create or update product.template from scraped vehicle.

        Re-lists reset inventory status to available and ``active=True``.
        Returns {"id", "action": "created"|"updated", "name", "list_price", ...}.
        """
        name = (name or "").strip()
        if not name:
            raise OdooCRMError("product name is required")
        if list_price <= 0:
            raise OdooCRMError(f"list_price must be positive for {name!r}")

        if categ_id is None:
            categ_id = self.find_vehicle_category_id()

        existing = self.find_product_template(default_code=default_code, name=name)
        vals: dict[str, Any] = {
            "name": name,
            "list_price": float(list_price),
            "default_code": default_code,
            "description_sale": description or False,
            "type": "consu",
            "is_storable": True,
            "active": True,
            "x_studio_state": "available",
        }
        if categ_id is not None:
            vals["categ_id"] = int(categ_id)

        def _write_or_create(product_id: int | None) -> int:
            attempt = dict(vals)
            last_exc: BaseException | None = None
            for drop in (
                (),
                ("is_storable",),
                ("is_storable", "type"),
                ("is_storable", "type", "description_sale"),
                ("is_storable", "type", "description_sale", "x_studio_state"),
                (
                    "is_storable",
                    "type",
                    "description_sale",
                    "x_studio_state",
                    "active",
                ),
            ):
                for key in drop:
                    attempt.pop(key, None)
                try:
                    if product_id is not None:
                        self.execute_kw(
                            "product.template", "write", [[product_id], attempt]
                        )
                        return product_id
                    return int(
                        self.execute_kw("product.template", "create", [attempt])
                    )
                except Exception as exc:
                    last_exc = exc
                    continue
            raise OdooCRMError(
                f"product upsert failed for {default_code}: {last_exc}"
            ) from last_exc

        if existing:
            product_id = _write_or_create(int(existing["id"]))
            action = "updated"
        else:
            product_id = _write_or_create(None)
            action = "created"

        # Always enforce available + active on website-present SKUs (incl. re-lists).
        status_meta = self._write_product_inventory_status(
            product_id,
            inventory_status="available",
            active=True,
        )

        # Best-effort on-hand qty (Odoo 19 may ignore / compute qty_available).
        try:
            variants = self.execute_kw(
                "product.product",
                "search",
                [[("product_tmpl_id", "=", product_id)]],
                {"limit": 1},
            )
            if variants and qty_available > 0:
                self._ensure_product_qty(int(variants[0]), float(qty_available))
        except Exception:
            pass

        return {
            "id": product_id,
            "action": action,
            "name": name,
            "list_price": float(list_price),
            "default_code": default_code,
            "inventory_status": "available",
            "state_field": status_meta.get("state_field"),
            "state_value": status_meta.get("state_value"),
        }

    def _ensure_product_qty(self, product_id: int, qty: float) -> None:
        """Set on-hand qty via stock.quant when inventory module allows it."""
        quants = self.execute_kw(
            "stock.quant",
            "search_read",
            [[("product_id", "=", product_id), ("location_id.usage", "=", "internal")]],
            {"fields": ["id", "quantity"], "limit": 1},
        )
        if quants:
            self.execute_kw(
                "stock.quant",
                "write",
                [[int(quants[0]["id"])], {"inventory_quantity": qty}],
            )
            try:
                self.execute_kw(
                    "stock.quant",
                    "action_apply_inventory",
                    [[int(quants[0]["id"])]],
                )
            except Exception:
                pass
            return
        # Create quant in first internal location if none exists.
        locations = self.execute_kw(
            "stock.location",
            "search",
            [[("usage", "=", "internal")]],
            {"limit": 1},
        )
        if not locations:
            return
        quant_id = int(
            self.execute_kw(
                "stock.quant",
                "create",
                [
                    {
                        "product_id": product_id,
                        "location_id": int(locations[0]),
                        "inventory_quantity": qty,
                    }
                ],
            )
        )
        try:
            self.execute_kw("stock.quant", "action_apply_inventory", [[quant_id]])
        except Exception:
            pass

    def list_active_vehicle_products(
        self,
        *,
        categ_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Active vehicle templates with Autosell SKU (`default_code`)."""
        domain: list[Any] = [
            ("active", "=", True),
            ("default_code", "!=", False),
        ]
        if categ_id is not None:
            domain.append(("categ_id", "=", int(categ_id)))
        else:
            domain.append(("categ_id.name", "ilike", "vehicul"))
        rows = self.execute_kw(
            "product.template",
            "search_read",
            [domain],
            {"fields": ["id", "name", "default_code", "list_price", "categ_id"]},
        )
        products: list[dict[str, Any]] = []
        for row in rows:
            code = str(row.get("default_code") or "").strip()
            if not code:
                continue
            products.append(
                {
                    "id": int(row["id"]),
                    "name": str(row.get("name") or ""),
                    "default_code": code,
                    "list_price": float(row.get("list_price") or 0),
                }
            )
        return products

    def archive_orphan_vehicles(
        self,
        active_default_codes: set[str] | list[str],
        *,
        categ_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Mark website-missing vehicles as sold, then soft-archive (`active=False`).

        Website removal = completed sale. Preserves history; only templates with a
        non-empty `default_code` in the vehicles category (or `categ_id`).
        """
        keep = {str(code).strip() for code in active_default_codes if str(code).strip()}
        archived: list[dict[str, Any]] = []
        for product in self.list_active_vehicle_products(categ_id=categ_id):
            code = product["default_code"]
            if code in keep:
                continue
            status_meta = self._write_product_inventory_status(
                int(product["id"]),
                inventory_status="sold",
                active=False,
            )
            archived.append(
                {
                    **product,
                    "inventory_status": "sold",
                    "state_field": status_meta.get("state_field"),
                    "state_value": status_meta.get("state_value"),
                }
            )
        return archived

    def create_or_update_lead(
        self,
        name: str,
        phone: str,
        vehicle_name: str,
        branch_id: int,
        *,
        down_payment: Any = None,
        term_months: int | None = None,
        quote_summary: str | None = None,
        stage_name: str = "Quote Generated",
        channel: str | None = None,
        estimated_monthly_payment: Any = None,
        vehicle_price: Any = None,
        schedule_follow_up: bool = True,
        user_id: int | None = None,
    ) -> QuoteLeadResult:
        """Find crm.lead by phone/chat id; create or update.

        Captures vehicle, enganche, plazo, and quote notes. Sets pipeline stage
        to ``stage_name`` when a matching ``crm.stage`` exists. Tags the lead
        and schedules a Phone Call / To-Do follow-up (~24h).
        """
        phone_norm = (phone or "").strip()
        if not phone_norm:
            raise OdooCRMError("phone is required")
        if not name or not name.strip():
            raise OdooCRMError("name is required")

        # Odoo 19 crm.lead: phone only (no mobile on lead in some editions)
        domain: list[Any] = [("phone", "=", phone_norm)]
        try:
            found = self.execute_kw(
                "crm.lead",
                "search",
                [domain],
                {"limit": 1},
            )
        except Exception as exc:
            raise OdooCRMError(f"lead search failed: {exc}") from exc

        note_lines = [
            f"Vehicle interest: {vehicle_name}".strip(),
        ]
        if channel:
            note_lines.append(f"Channel: {channel}")
        if vehicle_price not in (None, ""):
            note_lines.append(f"Vehicle price: {vehicle_price}")
        if down_payment not in (None, ""):
            note_lines.append(f"Requested down payment: {down_payment}")
        if term_months is not None:
            note_lines.append(f"Requested term: {int(term_months)} months")
        if estimated_monthly_payment not in (None, ""):
            note_lines.append(
                f"Estimated monthly payment: {estimated_monthly_payment}"
            )
        note_lines.append("Pipeline stage: Quote Generated")
        if quote_summary:
            note_lines.extend(["", "--- Quote breakdown ---", quote_summary.strip()])

        description = "\n".join(note_lines)
        opportunity_name = f"{name.strip()} — {vehicle_name}".strip(" —")

        vals: dict[str, Any] = {
            "name": opportunity_name[:128],
            "contact_name": name.strip(),
            "phone": phone_norm,
            "description": description,
            "type": "opportunity",
            "team_id": int(branch_id),
        }
        # Optional custom fields when present on the database
        vals["x_vehicle_name"] = vehicle_name
        if term_months is not None:
            vals["x_term_months"] = int(term_months)
        if down_payment not in (None, ""):
            try:
                vals["x_down_payment"] = float(down_payment)
            except (TypeError, ValueError):
                vals["x_down_payment"] = str(down_payment)

        stage_id = self._resolve_crm_stage_id(stage_name)
        if stage_id is not None:
            vals["stage_id"] = stage_id

        tag_ids = self._resolve_quote_lead_tag_ids(channel)
        if tag_ids:
            vals["tag_ids"] = [(6, 0, list(tag_ids))]

        medium_id, source_id = self.resolve_lead_attribution(channel=channel)
        if medium_id is not None:
            vals["medium_id"] = int(medium_id)
        if source_id is not None:
            vals["source_id"] = int(source_id)

        def _write_or_create(existing_id: int | None) -> int:
            attempt_vals = dict(vals)
            last_exc: BaseException | None = None
            for drop in (
                (),
                ("x_down_payment", "x_term_months"),
                ("x_vehicle_name", "x_down_payment", "x_term_months"),
                ("x_vehicle_name", "x_down_payment", "x_term_months", "stage_id"),
                (
                    "x_vehicle_name",
                    "x_down_payment",
                    "x_term_months",
                    "stage_id",
                    "tag_ids",
                ),
                (
                    "x_vehicle_name",
                    "x_down_payment",
                    "x_term_months",
                    "stage_id",
                    "tag_ids",
                    "medium_id",
                    "source_id",
                ),
                (
                    "x_vehicle_name",
                    "x_down_payment",
                    "x_term_months",
                    "stage_id",
                    "tag_ids",
                    "medium_id",
                    "source_id",
                    "team_id",
                ),
                (
                    "x_vehicle_name",
                    "x_down_payment",
                    "x_term_months",
                    "stage_id",
                    "tag_ids",
                    "medium_id",
                    "source_id",
                    "team_id",
                    "type",
                ),
            ):
                for key in drop:
                    attempt_vals.pop(key, None)
                try:
                    if existing_id is not None:
                        self.execute_kw(
                            "crm.lead", "write", [[existing_id], attempt_vals]
                        )
                        return existing_id
                    return int(self.execute_kw("crm.lead", "create", [attempt_vals]))
                except Exception as exc:
                    last_exc = exc
                    continue
            raise OdooCRMError(
                f"lead create/update failed after field fallbacks: {last_exc}"
            ) from last_exc

        lead_id = _write_or_create(int(found[0]) if found else None)

        # Apply tags separately if create/write dropped tag_ids.
        if tag_ids:
            try:
                self.execute_kw(
                    "crm.lead",
                    "write",
                    [[lead_id], {"tag_ids": [(6, 0, list(tag_ids))]}],
                )
            except Exception:
                pass

        activity_id: int | None = None
        if schedule_follow_up:
            activity_id = self.schedule_quote_follow_up(
                lead_id,
                vehicle_name=vehicle_name,
                down_payment=down_payment,
                term_months=term_months,
                estimated_monthly_payment=estimated_monthly_payment,
                channel=channel,
                branch_id=branch_id,
                user_id=user_id,
            )
            if activity_id is not None:
                print(
                    f"Scheduled follow-up activity id={activity_id} "
                    f"for lead id={lead_id}"
                )

        return QuoteLeadResult(
            lead_id=lead_id,
            activity_id=activity_id,
            tag_ids=tuple(tag_ids),
        )

    def _follow_up_deadline(self, *, hours: int = 24) -> date:
        """Deadline = now+hours, rolled to next weekday morning if weekend."""
        when = datetime.now(timezone.utc) + timedelta(hours=hours)
        day = when.date()
        # Sat→Mon, Sun→Mon
        if day.weekday() == 5:
            day = day + timedelta(days=2)
        elif day.weekday() == 6:
            day = day + timedelta(days=1)
        return day

    def _resolve_activity_type_id(
        self,
        *,
        kind: str = "call",
    ) -> int | None:
        """Prefer Phone Call or Meeting; fall back to To-Do."""
        kind_norm = (kind or "call").strip().lower()
        if kind_norm in {"meeting", "cita", "appointment"}:
            xmlids = [("mail", "mail_activity_data_meeting")]
            labels = (
                "Meeting",
                "Reunión",
                "Cita",
                "Call",
                "Phone Call",
                "Llamada",
                "To-Do",
                "To Do",
                "Todo",
            )
        else:
            xmlids = [("mail", "mail_activity_data_call")]
            labels = (
                "Call",
                "Phone Call",
                "Llamada",
                "Meeting",
                "Reunión",
                "To-Do",
                "To Do",
                "Todo",
            )
        for module, xmlid in xmlids:
            try:
                ref = self.execute_kw(
                    "ir.model.data",
                    "check_object_reference",
                    [module, xmlid],
                )
                if isinstance(ref, (list, tuple)) and len(ref) >= 2:
                    return int(ref[1])
            except Exception:
                pass
        for label in labels:
            try:
                found = self.execute_kw(
                    "mail.activity.type",
                    "search",
                    [[("name", "ilike", label)]],
                    {"limit": 1},
                )
                if found:
                    return int(found[0])
            except Exception:
                continue
        return None

    # Channel → (utm.medium name, utm.source name) for CRM reporting.
    LEAD_ATTRIBUTION: dict[str, tuple[str, str]] = {
        "whatsapp": ("WhatsApp", "Facebook Marketplace"),
        "voice / phone": ("Phone", "Inbound Call"),
        "voice_ai": ("Phone", "Inbound Call"),
        "voice": ("Phone", "Inbound Call"),
        "phone": ("Phone", "Inbound Call"),
        "inbound call": ("Phone", "Inbound Call"),
        "website": ("Website", "Autosell Web"),
        "web": ("Website", "Autosell Web"),
        "autosell web": ("Website", "Autosell Web"),
        "web form": ("Website", "Autosell Web"),
    }
    QUOTE_LEAD_TAG = "MG Quote Lead"

    def attribution_names_for_channel(
        self, channel: str | None
    ) -> tuple[str | None, str | None]:
        """Return (medium_name, source_name) for a lead channel label."""
        key = (channel or "").strip().lower()
        if not key:
            return None, None
        if key in self.LEAD_ATTRIBUTION:
            return self.LEAD_ATTRIBUTION[key]
        if "whatsapp" in key:
            return self.LEAD_ATTRIBUTION["whatsapp"]
        if "voice" in key or "phone" in key or "llamada" in key:
            return self.LEAD_ATTRIBUTION["voice / phone"]
        if "web" in key or "autosell" in key:
            return self.LEAD_ATTRIBUTION["website"]
        return None, None

    def resolve_lead_attribution(
        self,
        *,
        channel: str | None = None,
        medium_name: str | None = None,
        source_name: str | None = None,
        medium_id: int | None = None,
        source_id: int | None = None,
    ) -> tuple[int | None, int | None]:
        """Resolve ``(medium_id, source_id)`` from ids, names, or channel map.

        Falls back to ``ODOO_CRM_MEDIUM_ID`` / ``ODOO_CRM_SOURCE_ID`` when set.
        """
        if medium_id is None and medium_name:
            medium_id = self._ensure_utm_record("utm.medium", medium_name)
        if source_id is None and source_name:
            source_id = self._ensure_utm_record("utm.source", source_name)

        if medium_id is None or source_id is None:
            mapped_medium, mapped_source = self.attribution_names_for_channel(channel)
            if medium_id is None and mapped_medium:
                medium_id = self._ensure_utm_record("utm.medium", mapped_medium)
            if source_id is None and mapped_source:
                source_id = self._ensure_utm_record("utm.source", mapped_source)

        if medium_id is None:
            raw = (os.getenv("ODOO_CRM_MEDIUM_ID") or "").strip()
            if raw.isdigit() and int(raw) > 0:
                medium_id = int(raw)
        if source_id is None:
            raw = (os.getenv("ODOO_CRM_SOURCE_ID") or "").strip()
            if raw.isdigit() and int(raw) > 0:
                source_id = int(raw)
        return medium_id, source_id

    def _ensure_utm_record(self, model: str, name: str) -> int | None:
        """Search or create ``utm.medium`` / ``utm.source`` by exact name."""
        label = (name or "").strip()
        if not label or model not in {"utm.medium", "utm.source"}:
            return None
        try:
            rows = self.execute_kw(
                model,
                "search_read",
                [[("name", "=", label)]],
                {"fields": ["id"], "limit": 1},
            )
            if rows:
                return int(rows[0]["id"])
            return int(self.execute_kw(model, "create", [{"name": label}]))
        except Exception as exc:
            print(f"WARN _ensure_utm_record {model}={label!r}: {exc}")
            return None

    def _resolve_quote_lead_tag_ids(self, channel: str | None) -> list[int]:
        """Ensure MG Quote Lead (+ Messenger Bot when applicable) crm.tag ids."""
        names = [self.QUOTE_LEAD_TAG]
        channel_norm = (channel or "").strip().lower()
        if "messenger" in channel_norm or channel_norm.startswith("facebook"):
            names.append("Messenger Bot")

        tag_ids: list[int] = []
        seen_names: set[str] = set()
        seen_ids: set[int] = set()
        for name in names:
            key = name.lower()
            if key in seen_names:
                continue
            seen_names.add(key)
            tag_id = self._ensure_crm_tag(name)
            if tag_id is not None and tag_id not in seen_ids:
                seen_ids.add(tag_id)
                tag_ids.append(tag_id)
        return tag_ids

    def _ensure_crm_tag(self, name: str) -> int | None:
        label = (name or "").strip()
        if not label:
            return None
        for model in ("crm.tag", "crm.lead.tag"):
            try:
                rows = self.execute_kw(
                    model,
                    "search_read",
                    [[("name", "=", label)]],
                    {"fields": ["id"], "limit": 1},
                )
                if rows:
                    return int(rows[0]["id"])
                return int(self.execute_kw(model, "create", [{"name": label}]))
            except Exception:
                continue
        return None

    def _resolve_follow_up_user_id(
        self,
        *,
        branch_id: int,
        user_id: int | None = None,
    ) -> int | None:
        if user_id is not None:
            return int(user_id)
        env_raw = os.getenv(self.ENV_DEFAULT_ACTIVITY_USER, "").strip()
        if env_raw.isdigit():
            return int(env_raw)
        try:
            advisors = self._advisor_ids_for_branch(branch_id)
            if advisors:
                return int(advisors[0])
        except Exception:
            pass
        return int(self.uid) if self.uid is not None else None

    def schedule_activity(
        self,
        lead_id: int,
        *,
        summary: str | None = None,
        activity_kind: str = "call",
        hours: int = 24,
        user_id: int | None = None,
        branch_id: int = 1,
        note: str | None = None,
        dry_run: bool | None = None,
    ) -> int | None:
        """Create ``mail.activity`` on ``crm.lead`` (Call/Meeting, ~+24h).

        Failures are logged and return ``None`` so the pipeline can continue.
        """
        use_dry = self.dry_run if dry_run is None else bool(dry_run)
        summary_text = (summary or self.DEFAULT_ACTIVITY_SUMMARY).strip()[:200]
        if use_dry:
            print(
                f"DRY-RUN schedule_activity lead={lead_id} "
                f"summary={summary_text!r} kind={activity_kind}"
            )
            return -1

        try:
            activity_type_id = self._resolve_activity_type_id(kind=activity_kind)
            if activity_type_id is None:
                print(
                    f"WARN schedule_activity: no activity type for lead id={lead_id}"
                )
                return None
            assignee = self._resolve_follow_up_user_id(
                branch_id=branch_id, user_id=user_id
            )
            if assignee is None:
                print(
                    f"WARN schedule_activity: no assignee for lead id={lead_id}"
                )
                return None

            deadline = self._follow_up_deadline(hours=hours)
            note_body = note or summary_text
            note_html = (
                note_body.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            vals: dict[str, Any] = {
                "res_model": "crm.lead",
                "res_id": int(lead_id),
                "activity_type_id": int(activity_type_id),
                "summary": summary_text,
                "note": f"<p>{note_html}</p>",
                "date_deadline": deadline.isoformat(),
                "user_id": int(assignee),
            }
            try:
                model_ids = self.execute_kw(
                    "ir.model",
                    "search",
                    [[("model", "=", "crm.lead")]],
                    {"limit": 1},
                )
                if model_ids:
                    vals["res_model_id"] = int(model_ids[0])
            except Exception:
                pass

            last_exc: BaseException | None = None
            for drop in ((), ("res_model",), ("res_model_id",), ("note",)):
                attempt = dict(vals)
                for key in drop:
                    attempt.pop(key, None)
                try:
                    activity_id = int(
                        self.execute_kw("mail.activity", "create", [attempt])
                    )
                    print(
                        f"Scheduled activity id={activity_id} for lead id={lead_id}"
                    )
                    return activity_id
                except Exception as exc:
                    last_exc = exc
                    continue
            print(
                f"WARN schedule_activity failed for lead id={lead_id}: {last_exc}"
            )
            return None
        except Exception as exc:
            print(f"WARN schedule_activity error for lead id={lead_id}: {exc}")
            return None

    def schedule_quote_follow_up(
        self,
        lead_id: int,
        *,
        vehicle_name: str,
        down_payment: Any = None,
        term_months: int | None = None,
        estimated_monthly_payment: Any = None,
        channel: str | None = None,
        branch_id: int = 1,
        user_id: int | None = None,
        hours: int = 24,
        dry_run: bool | None = None,
    ) -> int | None:
        """Create mail.activity Phone Call / To-Do due in ~24h on the lead."""
        note_parts = [
            f"Follow up on generated vehicle quote: {vehicle_name}",
            f"Down payment: {down_payment if down_payment not in (None, '') else 'n/a'}",
            f"Loan term: {term_months if term_months is not None else 'n/a'} months",
            f"Monthly payment: {estimated_monthly_payment if estimated_monthly_payment not in (None, '') else 'n/a'}",
            f"Preferred channel: {channel or 'n/a'}",
        ]
        return self.schedule_activity(
            lead_id,
            summary=f"Follow up on generated vehicle quote: {vehicle_name}"[:200],
            activity_kind="call",
            hours=hours,
            user_id=user_id,
            branch_id=branch_id,
            note="<br/>".join(note_parts),
            dry_run=dry_run,
        )

    def _resolve_crm_stage_id(self, stage_name: str) -> int | None:
        """Best-effort match for crm.stage by name (e.g. Quote Generated)."""
        label = (stage_name or "").strip()
        if not label:
            return None
        try:
            rows = self.execute_kw(
                "crm.stage",
                "search_read",
                [[("name", "ilike", label)]],
                {"fields": ["id", "name"], "limit": 10},
            )
        except Exception:
            rows = []
        if not rows:
            lowered = label.lower()
            alts: list[str] = []
            if "prueba" in lowered or "cita" in lowered or "manejo" in lowered:
                alts = [
                    "Prueba de manejo",
                    "Cita",
                    "Test Drive",
                    "Appointment",
                ]
            for alt in alts:
                try:
                    rows = self.execute_kw(
                        "crm.stage",
                        "search_read",
                        [[("name", "ilike", alt)]],
                        {"fields": ["id", "name"], "limit": 5},
                    )
                except Exception:
                    rows = []
                if rows:
                    break
        if not rows:
            return None
        lowered = label.lower()
        for row in rows:
            if str(row.get("name") or "").strip().lower() == lowered:
                return int(row["id"])
        return int(rows[0]["id"])

    def _advisor_ids_for_branch(self, branch_id: int) -> list[int]:
        """Salespeople on crm.team (branch)."""
        teams = self.execute_kw(
            "crm.team",
            "read",
            [[int(branch_id)]],
            {"fields": ["member_ids"]},
        )
        if not teams:
            raise OdooCRMError(f"Unknown branch/team_id={branch_id}")
        member_ids = list(teams[0].get("member_ids") or [])
        if not member_ids:
            raise OdooCRMError(f"No advisors on branch/team_id={branch_id}")
        return [int(x) for x in member_ids]

    def _rr_param_key(self, branch_id: int) -> str:
        return f"autosell.round_robin.branch_{int(branch_id)}"

    def _get_rr_index(self, branch_id: int) -> int:
        key = self._rr_param_key(branch_id)
        try:
            params = self.execute_kw(
                "ir.config_parameter",
                "search_read",
                [[["key", "=", key]]],
                {"fields": ["value"], "limit": 1},
            )
            if params:
                return int(params[0]["value"])
        except Exception:
            pass
        return self._rr_cursor.get(int(branch_id), -1)

    def _set_rr_index(self, branch_id: int, index: int) -> None:
        self._rr_cursor[int(branch_id)] = index
        key = self._rr_param_key(branch_id)
        try:
            existing = self.execute_kw(
                "ir.config_parameter",
                "search",
                [[["key", "=", key]]],
                {"limit": 1},
            )
            if existing:
                self.execute_kw(
                    "ir.config_parameter",
                    "write",
                    [existing, {"value": str(index)}],
                )
            else:
                self.execute_kw(
                    "ir.config_parameter",
                    "create",
                    [{"key": key, "value": str(index)}],
                )
        except Exception:
            # Local cursor still advanced
            pass

    def round_robin_assign_advisor(self, branch_id: int) -> int:
        """Next advisor on branch team; write user_id on… caller assigns to lead.

        Returns assigned user_id.
        """
        advisors = self._advisor_ids_for_branch(branch_id)
        last = self._get_rr_index(branch_id)
        nxt = (last + 1) % len(advisors)
        self._set_rr_index(branch_id, nxt)
        user_id = advisors[nxt]
        return user_id

    def assign_lead_advisor(self, lead_id: int, user_id: int) -> bool:
        """Set crm.lead user_id (salesperson)."""
        return bool(
            self.execute_kw(
                "crm.lead",
                "write",
                [[int(lead_id)], {"user_id": int(user_id)}],
            )
        )

    def _mail_note_subtype_id(self) -> int | None:
        """Resolve mail.mt_note id (Odoo 19 has no subtype_xmlid on create)."""
        try:
            ref = self.execute_kw(
                "ir.model.data",
                "check_object_reference",
                ["mail", "mt_note"],
            )
            if isinstance(ref, (list, tuple)) and len(ref) >= 2:
                return int(ref[1])
        except Exception:
            pass
        try:
            found = self.execute_kw(
                "mail.message.subtype",
                "search",
                [[["name", "=", "Note"], ["res_model", "=", False]]],
                {"limit": 1},
            )
            if found:
                return int(found[0])
        except Exception:
            pass
        return None

    def post_quote_to_chatter(self, lead_id: int, quote_summary_text: str) -> int:
        """Post quote summary as mail.message on lead chatter. Returns message id."""
        body = (quote_summary_text or "").strip()
        if not body:
            raise OdooCRMError("quote_summary_text is required")
        safe = (
            body.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br/>")
        )
        vals: dict[str, Any] = {
            "model": "crm.lead",
            "res_id": int(lead_id),
            "body": f"<p>{safe}</p>",
            "message_type": "comment",
        }
        subtype_id = self._mail_note_subtype_id()
        if subtype_id is not None:
            vals["subtype_id"] = subtype_id

        try:
            return int(self.execute_kw("mail.message", "create", [vals]))
        except Exception as exc:
            # Retry bare comment if subtype rejected
            vals.pop("subtype_id", None)
            try:
                return int(self.execute_kw("mail.message", "create", [vals]))
            except Exception as exc2:
                raise OdooCRMError(
                    f"chatter post failed: {exc2}"
                ) from exc2

    @staticmethod
    def _parse_odoo_datetime(value: Any) -> datetime:
        """Parse ISO / Odoo datetime into aware UTC datetime."""
        if isinstance(value, datetime):
            when = value
        else:
            text = str(value or "").strip()
            if not text:
                raise ValueError("datetime is required")
            text = text.replace("Z", "+00:00")
            when = datetime.fromisoformat(text)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.astimezone(timezone.utc)

    @staticmethod
    def _format_odoo_datetime(when: datetime) -> str:
        return when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def _ensure_partner_for_lead(
        self,
        lead_id: int,
        *,
        customer_name: str | None = None,
        phone: str | None = None,
    ) -> tuple[int | None, int | None]:
        """Return (partner_id, salesperson_user_id) for a lead."""
        rows = self.execute_kw(
            "crm.lead",
            "read",
            [[int(lead_id)]],
            {"fields": ["partner_id", "contact_name", "phone", "user_id", "name"]},
        )
        if not rows:
            raise OdooCRMError(f"crm.lead id={lead_id} not found")
        row = rows[0]
        user_raw = row.get("user_id")
        user_id = (
            int(user_raw[0])
            if isinstance(user_raw, (list, tuple)) and user_raw
            else (int(user_raw) if user_raw else None)
        )
        partner_raw = row.get("partner_id")
        if isinstance(partner_raw, (list, tuple)) and partner_raw:
            return int(partner_raw[0]), user_id
        if partner_raw:
            return int(partner_raw), user_id

        name = (
            (customer_name or "").strip()
            or str(row.get("contact_name") or "").strip()
            or str(row.get("name") or "").strip()
            or "Prospecto"
        )
        phone_norm = (phone or str(row.get("phone") or "")).strip()
        partner_vals: dict[str, Any] = {"name": name, "type": "contact"}
        if phone_norm:
            partner_vals["phone"] = phone_norm
        partner_id = int(self.execute_kw("res.partner", "create", [partner_vals]))
        try:
            self.execute_kw(
                "crm.lead",
                "write",
                [[int(lead_id)], {"partner_id": partner_id}],
            )
        except Exception:
            pass
        return partner_id, user_id

    def create_test_drive_event(
        self,
        *,
        lead_id: int,
        vehicle_model: str,
        customer_name: str,
        start: Any,
        stop: Any | None = None,
        user_id: int | None = None,
        partner_id: int | None = None,
        phone: str | None = None,
        duration_hours: float = 1.0,
        advance_stage: bool = True,
        schedule_confirmation_activity: bool = True,
        branch_id: int = 1,
        dry_run: bool | None = None,
    ) -> TestDriveEventResult:
        """Create ``calendar.event`` for a test drive linked to ``crm.lead``.

        Advances stage to ``Cita/Prueba de manejo`` when possible. Calendar /
        activity failures are caught so the voice pipeline can continue.
        """
        use_dry = self.dry_run if dry_run is None else bool(dry_run)
        vehicle = (vehicle_model or "").strip() or "Vehículo"
        customer = (customer_name or "").strip() or "Cliente"
        event_name = f"Prueba de Manejo - {vehicle} - {customer}"[:200]

        try:
            start_dt = self._parse_odoo_datetime(start)
            if stop is None:
                stop_dt = start_dt + timedelta(hours=float(duration_hours or 1.0))
            else:
                stop_dt = self._parse_odoo_datetime(stop)
            if stop_dt <= start_dt:
                stop_dt = start_dt + timedelta(hours=1)
        except Exception as exc:
            msg = f"invalid test-drive datetime: {exc}"
            print(f"WARN create_test_drive_event lead={lead_id}: {msg}")
            return TestDriveEventResult(
                event_id=None, lead_id=int(lead_id), error=msg, dry_run=use_dry
            )

        if use_dry:
            print(
                f"DRY-RUN create_test_drive_event lead={lead_id} "
                f"name={event_name!r} start={start_dt.isoformat()} "
                f"stop={stop_dt.isoformat()}"
            )
            activity_id = None
            if schedule_confirmation_activity:
                activity_id = self.schedule_activity(
                    int(lead_id),
                    summary=self.DEFAULT_ACTIVITY_SUMMARY,
                    activity_kind="meeting",
                    hours=24,
                    user_id=user_id,
                    branch_id=branch_id,
                    dry_run=True,
                )
            return TestDriveEventResult(
                event_id=-1,
                lead_id=int(lead_id),
                stage_updated=advance_stage,
                activity_id=activity_id,
                partner_id=partner_id or -1,
                dry_run=True,
            )

        try:
            resolved_partner = partner_id
            resolved_user = user_id
            if resolved_partner is None or resolved_user is None:
                p_id, u_id = self._ensure_partner_for_lead(
                    int(lead_id),
                    customer_name=customer,
                    phone=phone,
                )
                if resolved_partner is None:
                    resolved_partner = p_id
                if resolved_user is None:
                    resolved_user = u_id
            if resolved_user is None:
                resolved_user = self._resolve_follow_up_user_id(
                    branch_id=branch_id, user_id=None
                )

            partner_ids: list[int] = []
            if resolved_partner:
                partner_ids.append(int(resolved_partner))
            if resolved_user:
                try:
                    users = self.execute_kw(
                        "res.users",
                        "read",
                        [[int(resolved_user)]],
                        {"fields": ["partner_id"]},
                    )
                    if users:
                        up = users[0].get("partner_id")
                        uid_partner = (
                            int(up[0])
                            if isinstance(up, (list, tuple)) and up
                            else (int(up) if up else None)
                        )
                        if uid_partner and uid_partner not in partner_ids:
                            partner_ids.append(uid_partner)
                except Exception:
                    pass

            vals: dict[str, Any] = {
                "name": event_name,
                "start": self._format_odoo_datetime(start_dt),
                "stop": self._format_odoo_datetime(stop_dt),
                "user_id": int(resolved_user) if resolved_user else False,
                "partner_ids": [(6, 0, partner_ids)] if partner_ids else False,
                "opportunity_id": int(lead_id),
                "description": (
                    f"Prueba de manejo agendada vía Voice AI.\n"
                    f"Vehículo: {vehicle}\nCliente: {customer}"
                ),
            }

            event_id: int | None = None
            last_exc: BaseException | None = None
            for drop in (
                (),
                ("opportunity_id",),
                ("opportunity_id", "description"),
                ("opportunity_id", "description", "partner_ids"),
                ("opportunity_id", "description", "partner_ids", "user_id"),
            ):
                attempt = dict(vals)
                for key in drop:
                    attempt.pop(key, None)
                if attempt.get("partner_ids") is False:
                    attempt.pop("partner_ids", None)
                if attempt.get("user_id") is False:
                    attempt.pop("user_id", None)
                try:
                    event_id = int(
                        self.execute_kw("calendar.event", "create", [attempt])
                    )
                    break
                except Exception as exc:
                    last_exc = exc
                    continue

            if event_id is None:
                msg = f"calendar.event create failed: {last_exc}"
                print(f"WARN create_test_drive_event lead={lead_id}: {msg}")
                return TestDriveEventResult(
                    event_id=None,
                    lead_id=int(lead_id),
                    partner_id=resolved_partner,
                    error=msg,
                )

            print(
                f"Created test-drive calendar.event id={event_id} "
                f"for lead id={lead_id}"
            )

            stage_updated = False
            if advance_stage:
                stage_id = self._resolve_crm_stage_id(self.TEST_DRIVE_STAGE)
                if stage_id is not None:
                    try:
                        self.execute_kw(
                            "crm.lead",
                            "write",
                            [[int(lead_id)], {"stage_id": int(stage_id)}],
                        )
                        stage_updated = True
                    except Exception as exc:
                        print(
                            f"WARN test-drive stage update lead={lead_id}: {exc}"
                        )

            activity_id = None
            if schedule_confirmation_activity:
                activity_id = self.schedule_activity(
                    int(lead_id),
                    summary=self.DEFAULT_ACTIVITY_SUMMARY,
                    activity_kind="meeting",
                    hours=24,
                    user_id=resolved_user,
                    branch_id=branch_id,
                )

            return TestDriveEventResult(
                event_id=event_id,
                lead_id=int(lead_id),
                stage_updated=stage_updated,
                activity_id=activity_id,
                partner_id=resolved_partner,
            )
        except Exception as exc:
            msg = str(exc)
            print(f"WARN create_test_drive_event lead={lead_id}: {msg}")
            return TestDriveEventResult(
                event_id=None, lead_id=int(lead_id), error=msg
            )
