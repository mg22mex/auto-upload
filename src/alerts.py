"""Lightweight failure alerts (Telegram bot API and/or Slack incoming webhook).

Stdlib-only and best-effort: a broken webhook must never fail a posting run, and
tokens are never echoed to logs.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from src.facebook.util import env_bool, env_int, env_str

MAX_MESSAGE_CHARS = 3500


@dataclass
class AlertResult:
    sent: list[str]
    failed: list[str]
    skipped_reason: str | None = None

    @property
    def delivered(self) -> bool:
        return bool(self.sent)


def alerts_enabled() -> bool:
    return env_bool("ALERTS_ENABLED", True)


def _timeout() -> int:
    return env_int("ALERT_HTTP_TIMEOUT_SEC", 10)


def _post_json(url: str, payload: dict) -> None:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=_timeout()) as response:
        if response.status >= 400:
            raise urllib.error.HTTPError(url, response.status, "alert rejected", None, None)


def _truncate(text: str) -> str:
    if len(text) <= MAX_MESSAGE_CHARS:
        return text
    return text[: MAX_MESSAGE_CHARS - 20] + "\n… (truncated)"


def send_telegram(text: str) -> bool:
    token = env_str("TELEGRAM_BOT_TOKEN", "")
    chat_id = env_str("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False
    _post_json(
        f"https://api.telegram.org/bot{token}/sendMessage",
        {"chat_id": chat_id, "text": _truncate(text), "disable_web_page_preview": True},
    )
    return True


def send_slack(text: str) -> bool:
    webhook = env_str("SLACK_WEBHOOK_URL", "", "ALERT_WEBHOOK_URL")
    if not webhook:
        return False
    _post_json(webhook, {"text": _truncate(text)})
    return True


def send_alert(message: str, *, subject: str | None = None) -> AlertResult:
    """Fan out one alert to every configured channel. Never raises."""
    if not alerts_enabled():
        return AlertResult(sent=[], failed=[], skipped_reason="ALERTS_ENABLED=false")

    prefix = env_str("ALERT_PREFIX", "auto-upload")
    header = f"[{prefix}] {subject}".strip() if subject else f"[{prefix}]"
    text = f"{header}\n{message}".strip()

    sent: list[str] = []
    failed: list[str] = []
    for channel, sender in (("telegram", send_telegram), ("slack", send_slack)):
        try:
            if sender(text):
                sent.append(channel)
        except Exception as exc:
            failed.append(f"{channel}: {type(exc).__name__}")
            print(f"WARNING: alert via {channel} failed ({type(exc).__name__})", flush=True)

    if not sent and not failed:
        return AlertResult(sent=[], failed=[], skipped_reason="no alert channel configured")
    return AlertResult(sent=sent, failed=failed)


__all__ = ["AlertResult", "alerts_enabled", "send_alert", "send_slack", "send_telegram"]
