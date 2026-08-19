"""WhatsApp API wrapper for open-wa / Evolution API (Phase 2).

Messaging worker — isolated from Playwright scraping sessions under sessions/.
"""
from src.whatsapp_worker.client import (
    WhatsAppWorkerClient,
    WhatsAppWorkerError,
    format_quote_message,
    normalize_phone_number,
)
from src.whatsapp_worker.inbound import (
    WA_CHANNEL,
    WhatsAppInboundEvent,
    parse_evolution_inbound,
)
from src.whatsapp_worker.routing import (
    apply_whatsapp_branch_context,
    branch_context_for_instance,
    resolve_instance_for_branch,
)

__all__ = [
    "WA_CHANNEL",
    "WhatsAppInboundEvent",
    "WhatsAppWorkerClient",
    "WhatsAppWorkerError",
    "apply_whatsapp_branch_context",
    "branch_context_for_instance",
    "format_quote_message",
    "normalize_phone_number",
    "parse_evolution_inbound",
    "resolve_instance_for_branch",
]
