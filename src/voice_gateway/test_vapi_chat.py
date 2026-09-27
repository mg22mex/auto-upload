"""Unit tests — Vapi WhatsApp text-first chat helpers."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.voice_gateway.vapi_chat import (
    VapiChatSessionStore,
    extract_assistant_text,
    vapi_wa_text_first_enabled,
)


class TestExtractAssistantText(unittest.TestCase):
    def test_last_assistant_content(self):
        payload = {
            "output": [
                {"role": "assistant", "tool_calls": [{"id": "1"}]},
                {"role": "assistant", "content": "Un momento"},
                {"role": "tool", "content": "{}"},
                {
                    "role": "assistant",
                    "content": "Tenemos un Corolla por trescientos mil pesos.",
                },
            ]
        }
        self.assertIn("Corolla", extract_assistant_text(payload))


class TestTextFirstFlag(unittest.TestCase):
    def test_default_off(self):
        with patch.dict("os.environ", {"VAPI_WA_TEXT_FIRST": ""}, clear=False):
            self.assertFalse(vapi_wa_text_first_enabled())

    def test_opt_in(self):
        with patch.dict("os.environ", {"VAPI_WA_TEXT_FIRST": "true"}, clear=False):
            self.assertTrue(vapi_wa_text_first_enabled())


class TestChatStore(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = VapiChatSessionStore(Path(tmp) / "chats.db")
            self.assertIsNone(store.get_chat_id("5216140000000", "autosell"))
            store.set_chat_id("5216140000000", "chat-abc", "autosell")
            self.assertEqual(
                store.get_chat_id("5216140000000", "autosell"), "chat-abc"
            )


if __name__ == "__main__":
    unittest.main()
