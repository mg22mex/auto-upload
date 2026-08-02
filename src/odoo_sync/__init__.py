"""Odoo CRM XML-RPC integration client (Phase 2).

Modular layout — shared session in ``base.OdooClient``, mixins:

* ``whatsapp`` — native WhatsApp templates
* ``fleet`` — fleet.vehicle VIN/plate → lead
* ``documents`` — ir.attachment helpers
* ``client.OdooCRMClient`` — CRM leads/calendar/inventory (+ all mixins)

Credentials from environment variables — never hardcode secrets.
"""
from src.odoo_sync.base import OdooClient, OdooCRMError
from src.odoo_sync.client import (
    OdooCRMClient,
    QuoteLeadResult,
    TestDriveEventResult,
)
from src.odoo_sync.documents import DocumentsMixin
from src.odoo_sync.fleet import FleetLinkResult, FleetMixin, FleetVehicle
from src.odoo_sync.whatsapp import (
    STANDARD_WHATSAPP_TEMPLATES,
    WhatsAppMixin,
    WhatsAppSendResult,
)

__all__ = [
    "DocumentsMixin",
    "FleetLinkResult",
    "FleetMixin",
    "FleetVehicle",
    "OdooClient",
    "OdooCRMClient",
    "OdooCRMError",
    "QuoteLeadResult",
    "STANDARD_WHATSAPP_TEMPLATES",
    "TestDriveEventResult",
    "WhatsAppMixin",
    "WhatsAppSendResult",
]
