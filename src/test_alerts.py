from __future__ import annotations

import pytest

from src import alerts


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in (
        "ALERTS_ENABLED",
        "ALERT_PREFIX",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "SLACK_WEBHOOK_URL",
        "ALERT_WEBHOOK_URL",
    ):
        monkeypatch.delenv(key, raising=False)


def test_no_channel_configured_is_skipped():
    result = alerts.send_alert("boom")
    assert not result.delivered
    assert result.skipped_reason == "no alert channel configured"


def test_disabled_short_circuits(monkeypatch):
    monkeypatch.setenv("ALERTS_ENABLED", "false")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.example/x")
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(alerts, "_post_json", lambda url, payload: calls.append((url, payload)))

    result = alerts.send_alert("boom")

    assert calls == []
    assert result.skipped_reason == "ALERTS_ENABLED=false"


def test_sends_to_both_channels(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.example/x")
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(alerts, "_post_json", lambda url, payload: calls.append((url, payload)))

    result = alerts.send_alert("session expired", subject="repost")

    assert result.sent == ["telegram", "slack"]
    assert calls[0][1]["chat_id"] == "42"
    assert "[auto-upload] repost" in calls[0][1]["text"]
    assert "session expired" in calls[1][1]["text"]


def test_channel_failure_does_not_raise(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.example/x")

    def boom(url, payload):
        raise OSError("network down")

    monkeypatch.setattr(alerts, "_post_json", boom)

    result = alerts.send_alert("boom")

    assert result.sent == []
    assert result.failed == ["slack: OSError"]


def test_long_messages_are_truncated(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.example/x")
    captured: dict = {}
    monkeypatch.setattr(alerts, "_post_json", lambda url, payload: captured.update(payload))

    alerts.send_alert("x" * 10_000)

    assert len(captured["text"]) <= alerts.MAX_MESSAGE_CHARS
