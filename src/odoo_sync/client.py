"""Odoo CRM XML-RPC client — leads, round-robin advisors, chatter."""
from __future__ import annotations

import os
import xmlrpc.client
from typing import Any


class OdooCRMError(RuntimeError):
    """Raised when Odoo auth or RPC calls fail."""


class OdooCRMClient:
    """Thin xmlrpc.client wrapper. Secrets from env only."""

    ENV_URL = "ODOO_URL"
    ENV_DB = "ODOO_DB"
    ENV_USER = "ODOO_USERNAME"
    ENV_KEY = "ODOO_API_KEY"

    def __init__(
        self,
        *,
        url: str | None = None,
        db: str | None = None,
        username: str | None = None,
        api_key: str | None = None,
        common: Any | None = None,
        models: Any | None = None,
    ) -> None:
        self.url = (url or os.getenv(self.ENV_URL, "")).rstrip("/")
        self.db = db or os.getenv(self.ENV_DB, "")
        # CI secrets may use ODOO_USER / ODOO_PASSWORD aliases.
        self.username = (
            username
            or os.getenv(self.ENV_USER, "")
            or os.getenv("ODOO_USER", "")
        )
        self.api_key = (
            api_key
            or os.getenv(self.ENV_KEY, "")
            or os.getenv("ODOO_PASSWORD", "")
        )
        self.uid: int | None = None
        self._common = common
        self._models = models
        # In-process RR cursor fallback when ir.config_parameter unavailable
        self._rr_cursor: dict[int, int] = {}

    def authenticate(self) -> int:
        """Authenticate; set uid. Reads URL/DB/username/API key from env if unset."""
        missing = [
            name
            for name, val in (
                (self.ENV_URL, self.url),
                (self.ENV_DB, self.db),
                (self.ENV_USER, self.username),
                (self.ENV_KEY, self.api_key),
            )
            if not val
        ]
        if missing:
            raise OdooCRMError(f"Missing Odoo env: {', '.join(missing)}")

        common = self._common or xmlrpc.client.ServerProxy(
            f"{self.url}/xmlrpc/2/common", allow_none=True
        )
        self._common = common
        uid = common.authenticate(self.db, self.username, self.api_key, {})
        if not uid:
            raise OdooCRMError("Odoo authenticate failed (check DB/user/API key)")
        self.uid = int(uid)
        if self._models is None:
            self._models = xmlrpc.client.ServerProxy(
                f"{self.url}/xmlrpc/2/object", allow_none=True
            )
        return self.uid

    def _ensure_auth(self) -> tuple[int, Any]:
        if self.uid is None or self._models is None:
            self.authenticate()
        assert self.uid is not None and self._models is not None
        return self.uid, self._models

    def execute_kw(
        self,
        model: str,
        method: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> Any:
        uid, models = self._ensure_auth()
        return models.execute_kw(
            self.db,
            uid,
            self.api_key,
            model,
            method,
            args or [],
            kwargs or {},
        )

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
            rows = self.execute_kw(
                "product.template",
                "search_read",
                [[("default_code", "=", default_code)]],
                {"fields": fields, "limit": 1},
            )
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

        Returns {"id", "action": "created"|"updated", "name", "list_price"}.
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

    def create_or_update_lead(
        self,
        name: str,
        phone: str,
        vehicle_name: str,
        branch_id: int,
    ) -> int:
        """Find crm.lead by phone; create or update. Returns lead_id."""
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

        vals: dict[str, Any] = {
            "name": name.strip(),
            "contact_name": name.strip(),
            "phone": phone_norm,
            "description": f"Vehicle interest: {vehicle_name}".strip(),
            "type": "opportunity",
            "team_id": int(branch_id),
        }
        # Optional custom field when present on the database
        vals["x_vehicle_name"] = vehicle_name

        def _write_or_create(existing_id: int | None) -> int:
            attempt_vals = dict(vals)
            last_exc: BaseException | None = None
            for drop in (
                (),
                ("x_vehicle_name",),
                ("x_vehicle_name", "team_id"),
                ("x_vehicle_name", "team_id", "type"),
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

        if found:
            return _write_or_create(int(found[0]))
        return _write_or_create(None)
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
