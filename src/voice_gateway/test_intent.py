"""Unit tests — voice intent / STT parsing + TTS formatting."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.voice_gateway.intent import (
    TRANSFER_PROMPT_ES,
    VOICE_CHANNEL,
    extract_intent_from_transcript,
    format_tts_quote,
    parse_voice_intent,
)


class TestTranscriptIntent(unittest.TestCase):
    def test_extracts_vehicle_term_enganche(self):
        hints = extract_intent_from_transcript(
            "Busco un Mazda CX-5 2020 a 36 meses con enganche de 30000"
        )
        self.assertIn("Mazda", hints.get("vehicle_name", ""))
        self.assertEqual(hints.get("term_months"), 36)
        self.assertEqual(hints.get("down_payment"), 30000)


class TestParseVoiceIntent(unittest.TestCase):
    def test_structured_quote(self):
        intent = parse_voice_intent(
            {
                "caller_phone": "6141234567",
                "caller_name": "Ana",
                "vehicle_interest": {"name": "CX-5", "price": 300000, "sku": "obj969"},
                "term": 36,
            }
        )
        self.assertTrue(intent.ok)
        self.assertEqual(intent.mode, "quote")
        lead = intent.to_lead_data()
        self.assertEqual(lead["channel"], VOICE_CHANNEL)
        self.assertEqual(lead["sku"], "obj969")

    def test_degraded_audio_fallback(self):
        intent = parse_voice_intent(
            {
                "caller_phone": "6149998888",
                "caller_name": "Luis",
                "audio_status": "degraded",
                "stt_confidence": 0.1,
                "transcript": "ehhh no se escucha",
            }
        )
        self.assertTrue(intent.ok)
        self.assertEqual(intent.mode, "generic_capture")
        self.assertTrue(intent.audio_degraded)
        self.assertIn("asesor", intent.tts_fallback.lower())
        self.assertTrue(intent.to_lead_data().get("soft_capture"))

    def test_transfer_without_phone(self):
        intent = parse_voice_intent({"caller_name": "X", "stt_failed": True})
        self.assertFalse(intent.ok)
        self.assertEqual(intent.mode, "transfer")
        self.assertEqual(intent.tts_fallback, TRANSFER_PROMPT_ES)

    def test_tts_quote_spanish(self):
        text = format_tts_quote(
            name="Ana",
            vehicle_name="Mazda CX-5",
            monthly="10891.67",
            down_payment="30000",
            term_months=36,
        )
        self.assertIn("Ana", text)
        self.assertIn("Mazda CX-5", text)
        self.assertIn("36 meses", text)
        self.assertIn("mensualidad", text.lower())


if __name__ == "__main__":
    unittest.main()
