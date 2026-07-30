"""Official Meta Graph API client for Facebook Page Messenger replies."""
from __future__ import annotations

import os
from typing import Any

import requests


class MetaGraphAPIError(RuntimeError):
    """Raised when Messenger configuration or Graph API requests fail."""


class MessengerClient:
    """Send Facebook Page messages through Graph API `/me/messages`."""

    ENV_PAGE_ACCESS_TOKEN = "FB_PAGE_ACCESS_TOKEN"
    GRAPH_API_URL = "https://graph.facebook.com"

    def __init__(
        self,
        *,
        page_access_token: str | None = None,
        session: requests.Session | None = None,
        timeout_sec: float = 15.0,
    ) -> None:
        self.page_access_token = (
            page_access_token or os.getenv(self.ENV_PAGE_ACCESS_TOKEN, "")
        ).strip()
        self.timeout_sec = timeout_sec
        self._session = session or requests.Session()

    def send_text_message(self, recipient_id: str, text: str) -> dict[str, Any]:
        """Send a plain-text Messenger response."""
        recipient = (recipient_id or "").strip()
        body = (text or "").strip()
        if not self.page_access_token:
            raise MetaGraphAPIError(f"Missing {self.ENV_PAGE_ACCESS_TOKEN}")
        if not recipient:
            raise MetaGraphAPIError("recipient_id is required")
        if not body:
            raise MetaGraphAPIError("message text is required")

        response = self._session.post(
            f"{self.GRAPH_API_URL}/me/messages",
            params={"access_token": self.page_access_token},
            json={
                "recipient": {"id": recipient},
                "messaging_type": "RESPONSE",
                "message": {"text": body[:2000]},
            },
            timeout=self.timeout_sec,
        )
        if response.status_code >= 400:
            raise MetaGraphAPIError(
                f"Meta Graph API {response.status_code}: {response.text[:300]}"
            )
        try:
            payload = response.json()
        except ValueError:
            return {"ok": True}
        return payload if isinstance(payload, dict) else {"ok": True, "data": payload}
