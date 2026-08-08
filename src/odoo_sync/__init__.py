"""Odoo CRM XML-RPC integration client (Phase 2).

Modular layout — shared session in ``base.OdooClient``:

* ``client.OdooCRMClient`` — CRM leads/calendar/inventory (+ WhatsApp/Fleet/Documents mixins)
* ``crm.CRMLeadManager`` — payload upsert, phone dedupe, branch teams, fleet location routing
* ``quotes.QuotePDFManager`` — branch-branded PDF + attach to lead
* ``triggers`` — stage/webhook automation (PDF + WA queue pending Meta)
* ``whatsapp`` — native templates (**Meta Cloud API paused**)
* ``fleet`` — fleet.vehicle VIN/plate → lead
* ``documents`` — ir.attachment helpers

Credentials from environment variables — never hardcode secrets.
"""
from src.odoo_sync.base import OdooClient, OdooCRMError
from src.odoo_sync.client import (
    OdooCRMClient,
    QuoteLeadResult,
    TestDriveEventResult,
)
from src.odoo_sync.crm import (
    CRMLeadManager,
    load_branch_teams,
    resolve_team_id,
)
from src.odoo_sync.documents import DocumentsMixin
from src.odoo_sync.fleet import FleetLinkResult, FleetMixin, FleetVehicle
from src.odoo_sync.quotes import (
    QuotePDFManager,
    resolve_quote_branch,
)
from src.odoo_sync.triggers import (
    OdooTriggerManager,
    process_incoming_webhook,
)
from src.odoo_sync.whatsapp import (
    PRIMARY_BRANCH,
    STANDARD_WHATSAPP_TEMPLATES,
    WhatsAppMixin,
    WhatsAppSendResult,
    load_whatsapp_branch_accounts,
    resolve_whatsapp_account,
)

__all__ = [
    "CRMLeadManager",
    "DocumentsMixin",
    "FleetLinkResult",
    "FleetMixin",
    "FleetVehicle",
    "OdooClient",
    "OdooCRMClient",
    "OdooCRMError",
    "OdooTriggerManager",
    "PRIMARY_BRANCH",
    "QuoteLeadResult",
    "QuotePDFManager",
    "STANDARD_WHATSAPP_TEMPLATES",
    "TestDriveEventResult",
    "WhatsAppMixin",
    "WhatsAppSendResult",
    "load_branch_teams",
    "load_whatsapp_branch_accounts",
    "process_incoming_webhook",
    "resolve_quote_branch",
    "resolve_team_id",
    "resolve_whatsapp_account",
]
