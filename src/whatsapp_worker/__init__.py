"""WhatsApp API wrapper for open-wa / Evolution API (Phase 2).

Messaging worker — isolated from Playwright scraping sessions under sessions/.
"""
from src.whatsapp_worker.client import (
    WhatsAppWorkerClient,
    WhatsAppWorkerError,
    format_quote_message,
    normalize_phone_number,
)

__all__ = [
    "WhatsAppWorkerClient",
    "WhatsAppWorkerError",
    "format_quote_message",
    "normalize_phone_number",
]
