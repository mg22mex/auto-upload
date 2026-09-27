"""Unit tests for Beatriz qualify-first inventory prompt helpers."""
from __future__ import annotations

import unittest

from src.voice_gateway.prompts import (
    ANTI_SILENCE_MARKER,
    QUALIFY_FIRST_MARKER,
    qualify_prompt_for_branch,
    upsert_prompt_block,
)
from scripts.configure_vapi_inventory_tool import patch_system_prompt


class TestQualifyPrompts(unittest.TestCase):
    def test_qualify_prompt_branch_labels(self):
        text = qualify_prompt_for_branch("san_felipe")
        self.assertIn("San Felipe", text)
        self.assertIn("SUV", text)
        self.assertIn("presupuesto", text.casefold())

    def test_upsert_and_patch_system_prompt(self):
        base = (
            "Identidad\n\n"
            "## REGLA ANTI-SILENCIO (OBLIGATORIO)\n"
            "- Cuando el cliente pida un vehículo, llama INMEDIATAMENTE.\n\n"
            "## PROHIBIDO HABLAR ANTES DEL TOOL (CRÍTICO)\n"
            "- Old anti silence.\n"
        )
        patched = patch_system_prompt(base)
        self.assertIn(QUALIFY_FIRST_MARKER, patched)
        self.assertIn(ANTI_SILENCE_MARKER, patched)
        self.assertIn("forma ABIERTA", patched)
        self.assertNotIn("## REGLA ANTI-SILENCIO (OBLIGATORIO)", patched)
        # Idempotent upsert
        again = upsert_prompt_block(
            patched, QUALIFY_FIRST_MARKER, "\n\n" + QUALIFY_FIRST_MARKER + "\n- once\n"
        )
        self.assertEqual(again.count(QUALIFY_FIRST_MARKER), 1)


if __name__ == "__main__":
    unittest.main()
