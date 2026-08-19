"""Unit tests — Evolution inbound webhook parse."""
from __future__ import annotations

import unittest

from src.whatsapp_worker.inbound import (
    WA_CHANNEL,
    inbound_to_voice_payload,
    parse_evolution_inbound,
)


_UPSERT = {
    "event": "messages.upsert",
    "instance": "autosell_main",
    "data": {
        "key": {
            "remoteJid": "5216141234567@s.whatsapp.net",
            "fromMe": False,
            "id": "ABCD",
        },
        "pushName": "Ana Pérez",
        "message": {"conversation": "Quiero cotizar un Mazda CX-5 a 36 meses"},
    },
}


class TestParseEvolutionInbound(unittest.TestCase):
    def test_conversation_text(self):
        events = parse_evolution_inbound(_UPSERT)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].phone, "5216141234567")
        self.assertEqual(events[0].name, "Ana Pérez")
        self.assertIn("Mazda", events[0].text)
        self.assertEqual(events[0].instance, "autosell_main")

    def test_skips_from_me(self):
        payload = {
            "event": "messages.upsert",
            "data": {
                "key": {
                    "remoteJid": "5216141234567@s.whatsapp.net",
                    "fromMe": True,
                    "id": "1",
                },
                "message": {"conversation": "echo"},
            },
        }
        self.assertEqual(parse_evolution_inbound(payload), [])

    def test_skips_groups(self):
        payload = {
            "event": "messages.upsert",
            "data": {
                "key": {"remoteJid": "120363@g.us", "fromMe": False, "id": "1"},
                "message": {"conversation": "hola"},
            },
        }
        self.assertEqual(parse_evolution_inbound(payload), [])

    def test_extended_text(self):
        payload = {
            "event": "messages.upsert",
            "data": {
                "key": {
                    "remoteJid": "6141234567@s.whatsapp.net",
                    "fromMe": False,
                    "id": "2",
                },
                "pushName": "Luis",
                "message": {"extendedTextMessage": {"text": "precio 150000"}},
            },
        }
        events = parse_evolution_inbound(payload)
        self.assertEqual(events[0].text, "precio 150000")

    def test_voice_payload_shape(self):
        event = parse_evolution_inbound(_UPSERT)[0]
        body = inbound_to_voice_payload(event)
        self.assertEqual(body["channel"], WA_CHANNEL)
        self.assertEqual(body["caller_phone"], event.phone)
        self.assertEqual(body["transcript"], event.text)
        self.assertEqual(body["vehicle_interest"], event.text)


if __name__ == "__main__":
    unittest.main()
