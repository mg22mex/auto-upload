"""Odoo native WhatsApp template dispatch (whatsapp.template / composer).

Multi-branch routing: Periférico is the primary channel; San Felipe is a
placeholder until ``ODOO_WA_ACCOUNT_SAN_FELIPE`` is set.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol

# Env keys for Odoo WhatsApp account (whatsapp.account) IDs per branch.
ENV_WA_ACCOUNT_PERIFERICO = "ODOO_WA_ACCOUNT_PERIFERICO"
ENV_WA_ACCOUNT_SAN_FELIPE = "ODOO_WA_ACCOUNT_SAN_FELIPE"

PRIMARY_BRANCH = "periferico"
PLACEHOLDER_BRANCH = "san_felipe"

# Canonical aliases → env key. Periférico is always primary/default.
BRANCH_ACCOUNT_ENV: dict[str, str] = {
    PRIMARY_BRANCH: ENV_WA_ACCOUNT_PERIFERICO,
    PLACEHOLDER_BRANCH: ENV_WA_ACCOUNT_SAN_FELIPE,
    # Common aliases
    "periferico": ENV_WA_ACCOUNT_PERIFERICO,
    "periférico": ENV_WA_ACCOUNT_PERIFERICO,
    "san_felipe": ENV_WA_ACCOUNT_SAN_FELIPE,
    "san-felipe": ENV_WA_ACCOUNT_SAN_FELIPE,
    "sanfelipe": ENV_WA_ACCOUNT_SAN_FELIPE,
}


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


# Canonical business templates (match Odoo approved names when possible).
STANDARD_WHATSAPP_TEMPLATES: dict[str, tuple[str, ...]] = {
    "sale_order": ("Sale Order", "sale_order", "Orden de venta", "sales_order"),
    "payment_link": ("Payment Link", "payment_link", "Enlace de pago"),
    "invoice": ("Invoice", "invoice", "Factura"),
    "payment_receipt": ("Payment Receipt", "payment_receipt", "Recibo de pago"),
}


@dataclass(frozen=True)
class WhatsAppSendResult:
    ok: bool
    template_name: str
    message_id: int | None = None
    composer_id: int | None = None
    dry_run: bool = False
    error: str | None = None
    branch: str = PRIMARY_BRANCH
    wa_account_id: int | None = None
    fell_back: bool = False


def parse_wa_account_id(raw: str | None) -> int | None:
    """Parse env value to int; empty/None/null/none → None (placeholder)."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text.lower() in {"none", "null", "0"}:
        return None
    try:
        value = int(text)
    except ValueError:
        return None
    return value if value > 0 else None


def load_whatsapp_branch_accounts() -> dict[str, int | None]:
    """Map branch keys → Odoo WhatsApp account id (or None if not configured).

    Defaults:
    * ``periferico`` — primary; id from ``ODOO_WA_ACCOUNT_PERIFERICO``
    * ``san_felipe`` — placeholder; ``ODOO_WA_ACCOUNT_SAN_FELIPE`` unset/None
    """
    return {
        PRIMARY_BRANCH: parse_wa_account_id(os.getenv(ENV_WA_ACCOUNT_PERIFERICO)),
        PLACEHOLDER_BRANCH: parse_wa_account_id(os.getenv(ENV_WA_ACCOUNT_SAN_FELIPE)),
    }


def normalize_wa_branch(branch: str | None) -> str:
    key = (branch or PRIMARY_BRANCH).strip().lower().replace(" ", "_")
    if key in {"periferico", "periférico", "periferico_sur", "primary", "default"}:
        return PRIMARY_BRANCH
    if key in {"san_felipe", "san-felipe", "sanfelipe", "san_felipe_sur"}:
        return PLACEHOLDER_BRANCH
    return key or PRIMARY_BRANCH


def resolve_whatsapp_account(
    branch: str = PRIMARY_BRANCH,
    *,
    accounts: dict[str, int | None] | None = None,
) -> tuple[str, int | None, bool]:
    """Resolve (effective_branch, wa_account_id, fell_back).

    San Felipe without a configured account → soft fallback to Periférico.
    """
    requested = normalize_wa_branch(branch)
    table = accounts if accounts is not None else load_whatsapp_branch_accounts()
    primary_id = table.get(PRIMARY_BRANCH)
    account_id = table.get(requested)

    if requested == PLACEHOLDER_BRANCH and account_id is None:
        print(
            f"WARN WhatsApp branch {PLACEHOLDER_BRANCH!r}: "
            f"{ENV_WA_ACCOUNT_SAN_FELIPE} not configured; "
            f"falling back to primary {PRIMARY_BRANCH!r} "
            f"({ENV_WA_ACCOUNT_PERIFERICO}={primary_id!r})"
        )
        return PRIMARY_BRANCH, primary_id, True

    if account_id is None and requested != PRIMARY_BRANCH:
        print(
            f"WARN WhatsApp branch {requested!r}: no account id; "
            f"falling back to primary {PRIMARY_BRANCH!r}"
        )
        return PRIMARY_BRANCH, primary_id, True

    return requested, account_id, False


class WhatsAppMixin:
    """Send WhatsApp templates via Odoo WhatsApp module (soft-fail)."""

    def resolve_whatsapp_template_id(
        self: _OdooSession,
        template_name: str,
    ) -> tuple[int | None, str | None]:
        """Return (template_id, resolved_name) or (None, None)."""
        aliases = list(STANDARD_WHATSAPP_TEMPLATES.get(template_name.lower(), ()))
        aliases.append(template_name)
        # Dedupe preserving order
        seen: set[str] = set()
        names: list[str] = []
        for name in aliases:
            key = name.strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            names.append(name.strip())

        for model in ("whatsapp.template", "whatsapp.message.template"):
            for name in names:
                try:
                    rows = self.execute_kw(
                        model,
                        "search_read",
                        [[("name", "ilike", name)]],
                        {"fields": ["id", "name"], "limit": 5},
                    )
                except Exception:
                    rows = []
                if not rows:
                    continue
                lowered = name.lower()
                for row in rows:
                    if str(row.get("name") or "").strip().lower() == lowered:
                        return int(row["id"]), str(row["name"])
                return int(rows[0]["id"]), str(rows[0].get("name") or name)
        return None, None

    def send_whatsapp_template(
        self: _OdooSession,
        lead_id: int,
        template_name: str,
        variables: dict[str, Any] | None = None,
        branch: str = PRIMARY_BRANCH,
        *,
        dry_run: bool | None = None,
    ) -> WhatsAppSendResult:
        """Send an approved WhatsApp template related to ``crm.lead``.

        ``branch`` selects the Odoo WhatsApp account:
        * ``periferico`` (default) — primary active phone
        * ``san_felipe`` — falls back to Periférico until configured

        Uses ``whatsapp.template`` / ``whatsapp.composer`` when available.
        Soft-logs and returns ``ok=False`` when the module or template is missing.
        """
        use_dry = self._use_dry_run(dry_run)
        label = (template_name or "").strip()
        effective_branch, wa_account_id, fell_back = resolve_whatsapp_account(branch)

        if not label:
            return WhatsAppSendResult(
                ok=False,
                template_name="",
                error="template_name is required",
                dry_run=use_dry,
                branch=effective_branch,
                wa_account_id=wa_account_id,
                fell_back=fell_back,
            )

        if use_dry:
            print(
                f"DRY-RUN send_whatsapp_template lead={lead_id} "
                f"template={label!r} variables={variables or {}} "
                f"branch={effective_branch!r} wa_account_id={wa_account_id!r} "
                f"fell_back={fell_back}"
            )
            return WhatsAppSendResult(
                ok=True,
                template_name=label,
                message_id=-1,
                composer_id=-1,
                dry_run=True,
                branch=effective_branch,
                wa_account_id=wa_account_id,
                fell_back=fell_back,
            )

        try:
            template_id, resolved = self.resolve_whatsapp_template_id(label)
            if template_id is None:
                msg = (
                    f"WhatsApp template not found/configured for {label!r} "
                    "(install Odoo WhatsApp or approve the template)"
                )
                print(f"WARN send_whatsapp_template lead={lead_id}: {msg}")
                return WhatsAppSendResult(
                    ok=False,
                    template_name=label,
                    error=msg,
                    branch=effective_branch,
                    wa_account_id=wa_account_id,
                    fell_back=fell_back,
                )

            # Resolve partner from lead for the composer
            partner_id: int | None = None
            phone: str | None = None
            try:
                leads = self.execute_kw(
                    "crm.lead",
                    "read",
                    [[int(lead_id)]],
                    {"fields": ["partner_id", "phone", "mobile"]},
                )
                if leads:
                    pr = leads[0].get("partner_id")
                    if isinstance(pr, (list, tuple)) and pr:
                        partner_id = int(pr[0])
                    elif pr:
                        partner_id = int(pr)
                    phone = str(
                        leads[0].get("mobile") or leads[0].get("phone") or ""
                    ).strip() or None
            except Exception as exc:
                print(f"WARN whatsapp lead read {lead_id}: {exc}")

            free_text = ""
            if variables:
                free_text = "\n".join(f"{k}: {v}" for k, v in variables.items())

            composer_vals: dict[str, Any] = {
                "wa_template_id": int(template_id),
                "res_model": "crm.lead",
                "res_ids": str(int(lead_id)),
            }
            if wa_account_id is not None:
                composer_vals["wa_account_id"] = int(wa_account_id)
            if partner_id:
                composer_vals["partner_ids"] = [(6, 0, [partner_id])]
            if phone:
                composer_vals["phone"] = phone
            if free_text:
                composer_vals["body"] = free_text

            composer_id: int | None = None
            message_id: int | None = None
            last_exc: BaseException | None = None

            for model in ("whatsapp.composer", "whatsapp.message"):
                try:
                    if model == "whatsapp.composer":
                        attempt = dict(composer_vals)
                        for drop in (
                            (),
                            ("body",),
                            ("phone",),
                            ("phone", "body"),
                            ("wa_account_id",),
                            ("partner_ids", "phone", "body"),
                            ("wa_account_id", "partner_ids", "phone", "body"),
                        ):
                            vals = dict(attempt)
                            for key in drop:
                                vals.pop(key, None)
                            try:
                                composer_id = int(
                                    self.execute_kw(model, "create", [vals])
                                )
                                break
                            except Exception as exc:
                                last_exc = exc
                                continue
                        if composer_id is None:
                            continue
                        # Prefer action_send_whatsapp / action_send_message
                        for method in (
                            "action_send_whatsapp_template",
                            "send_whatsapp_template",
                            "action_send_whatsapp",
                            "action_send",
                        ):
                            try:
                                self.execute_kw(
                                    model, method, [[int(composer_id)]]
                                )
                                message_id = composer_id
                                break
                            except Exception as exc:
                                last_exc = exc
                                continue
                        if message_id is not None or composer_id is not None:
                            break
                    else:
                        # Fallback: create whatsapp.message directly
                        msg_vals: dict[str, Any] = {
                            "wa_template_id": int(template_id),
                            "res_model": "crm.lead",
                            "res_id": int(lead_id),
                        }
                        if wa_account_id is not None:
                            msg_vals["wa_account_id"] = int(wa_account_id)
                        if partner_id:
                            msg_vals["partner_id"] = partner_id
                        if free_text:
                            msg_vals["body"] = free_text
                        message_id = int(
                            self.execute_kw(model, "create", [msg_vals])
                        )
                        break
                except Exception as exc:
                    last_exc = exc
                    continue

            if composer_id is None and message_id is None:
                msg = (
                    f"WhatsApp send failed (module/credentials?): {last_exc}"
                )
                print(f"WARN send_whatsapp_template lead={lead_id}: {msg}")
                return WhatsAppSendResult(
                    ok=False,
                    template_name=resolved or label,
                    error=msg,
                    branch=effective_branch,
                    wa_account_id=wa_account_id,
                    fell_back=fell_back,
                )

            print(
                f"Sent WhatsApp template {resolved or label!r} "
                f"to lead id={lead_id} branch={effective_branch} "
                f"wa_account_id={wa_account_id} "
                f"(composer={composer_id}, message={message_id})"
            )
            return WhatsAppSendResult(
                ok=True,
                template_name=resolved or label,
                message_id=message_id,
                composer_id=composer_id,
                branch=effective_branch,
                wa_account_id=wa_account_id,
                fell_back=fell_back,
            )
        except Exception as exc:
            msg = str(exc)
            print(f"WARN send_whatsapp_template lead={lead_id}: {msg}")
            return WhatsAppSendResult(
                ok=False,
                template_name=label,
                error=msg,
                branch=effective_branch,
                wa_account_id=wa_account_id,
                fell_back=fell_back,
            )
