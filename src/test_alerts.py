"""Unit tests — failure alert fan-out (Telegram + Slack)."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from src import alerts

_ALERT_ENV = (
    "ALERTS_ENABLED",
    "ALERT_PREFIX",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "SLACK_WEBHOOK_URL",
    "ALERT_WEBHOOK_URL",
)


class AlertTestCase(unittest.TestCase):
    """Isolates alert env so a host .env cannot leak real webhooks into tests."""

    def setUp(self) -> None:
        patcher = patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in _ALERT_ENV:
            os.environ.pop(key, None)

    def capture_posts(self) -> list[tuple[str, dict]]:
        calls: list[tuple[str, dict]] = []
        patcher = patch.object(
            alerts,
            "_post_json",
            lambda url, payload: calls.append((url, payload)),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls


class TestAlertChannels(AlertTestCase):
    def test_no_channel_configured_is_skipped(self):
        result = alerts.send_alert("boom")

        self.assertFalse(result.delivered)
        self.assertEqual(result.skipped_reason, "no alert channel configured")

    def test_disabled_short_circuits(self):
        os.environ.update(
            ALERTS_ENABLED="false", SLACK_WEBHOOK_URL="https://hooks.example/x"
        )
        calls = self.capture_posts()

        result = alerts.send_alert("boom")

        self.assertEqual(calls, [])
        self.assertEqual(result.skipped_reason, "ALERTS_ENABLED=false")

    def test_sends_to_both_channels(self):
        os.environ.update(
            TELEGRAM_BOT_TOKEN="tok",
            TELEGRAM_CHAT_ID="42",
            SLACK_WEBHOOK_URL="https://hooks.example/x",
        )
        calls = self.capture_posts()

        result = alerts.send_alert("session expired", subject="repost")

        self.assertEqual(result.sent, ["telegram", "slack"])
        self.assertEqual(calls[0][1]["chat_id"], "42")
        self.assertIn("[auto-upload] repost", calls[0][1]["text"])
        self.assertIn("session expired", calls[1][1]["text"])

    def test_telegram_targets_bot_api_send_message(self):
        os.environ.update(TELEGRAM_BOT_TOKEN="tok", TELEGRAM_CHAT_ID="42")
        calls = self.capture_posts()

        alerts.send_alert("boom")

        self.assertEqual(calls[0][0], "https://api.telegram.org/bottok/sendMessage")

    def test_telegram_requires_both_token_and_chat(self):
        os.environ.update(TELEGRAM_BOT_TOKEN="tok")
        calls = self.capture_posts()

        result = alerts.send_alert("boom")

        self.assertEqual(calls, [])
        self.assertEqual(result.skipped_reason, "no alert channel configured")

    def test_channel_failure_does_not_raise(self):
        os.environ["SLACK_WEBHOOK_URL"] = "https://hooks.example/x"

        def boom(url, payload):
            raise OSError("network down")

        with patch.object(alerts, "_post_json", boom):
            result = alerts.send_alert("boom")

        self.assertEqual(result.sent, [])
        self.assertEqual(result.failed, ["slack: OSError"])

    def test_long_messages_are_truncated(self):
        os.environ["SLACK_WEBHOOK_URL"] = "https://hooks.example/x"
        calls = self.capture_posts()

        alerts.send_alert("x" * 10_000)

        self.assertLessEqual(len(calls[0][1]["text"]), alerts.MAX_MESSAGE_CHARS)


if __name__ == "__main__":
    unittest.main()
