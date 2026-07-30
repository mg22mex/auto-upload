"""Odoo CRM XML-RPC integration client (Phase 2).

Credentials and endpoints from environment variables — never hardcode secrets.
"""
from src.odoo_sync.client import OdooCRMClient, OdooCRMError

__all__ = ["OdooCRMClient", "OdooCRMError"]
