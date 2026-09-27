"""Inbound Autosell.mx webform leads → Odoo CRM + Beatriz WhatsApp outreach."""
from __future__ import annotations

from src.web_leads.models import WebLead
from src.web_leads.parser import parse_web_lead_email
from src.web_leads.pipeline import ingest_web_lead, process_web_lead_batch

__all__ = [
    "WebLead",
    "ingest_web_lead",
    "parse_web_lead_email",
    "process_web_lead_batch",
]
