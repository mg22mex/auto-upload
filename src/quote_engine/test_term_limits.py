"""Unit tests — CrediAuto year-based max term matrix."""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.quote_engine.engine import CalibratedQuoteEngine
from src.quote_engine.term_limits import (
    TERM_CAP_NOTE_TEMPLATE,
    extract_model_year,
    max_term_months_for_year,
    resolve_crediauto_term,
)


class TestExtractModelYear(unittest.TestCase):
    def test_int_and_title(self):
        self.assertEqual(extract_model_year(2021), 2021)
        self.assertEqual(extract_model_year("Ford Ranger XLT 2021"), 2021)
        self.assertEqual(
            extract_model_year("Hola, quiero cotizar una Ford Ranger XLT 2021"),
            2021,
        )
        self.assertIsNone(extract_model_year(None))
        self.assertIsNone(extract_model_year("sin año"))


class TestMaxTermMatrix(unittest.TestCase):
    """Matrix anchored at reference_year=2026."""

    REF = 2026

    def test_bands(self):
        self.assertEqual(max_term_months_for_year(2026, reference_year=self.REF), 60)
        self.assertEqual(max_term_months_for_year(2024, reference_year=self.REF), 60)
        self.assertEqual(max_term_months_for_year(2023, reference_year=self.REF), 48)
        self.assertEqual(max_term_months_for_year(2022, reference_year=self.REF), 48)
        self.assertEqual(max_term_months_for_year(2021, reference_year=self.REF), 36)
        self.assertEqual(max_term_months_for_year(2018, reference_year=self.REF), 36)
        self.assertIsNone(max_term_months_for_year(None, reference_year=self.REF))


class TestResolveCrediautoTerm(unittest.TestCase):
    def test_2021_caps_60_to_36(self):
        r = resolve_crediauto_term(60, 2021, reference_year=2026)
        self.assertTrue(r.capped)
        self.assertEqual(r.term_months, 36)
        self.assertEqual(r.max_allowed_term, 36)
        self.assertEqual(
            r.note,
            TERM_CAP_NOTE_TEMPLATE.format(year=2021, max_allowed_term=36),
        )

    def test_2022_caps_60_to_48(self):
        r = resolve_crediauto_term(60, 2022, reference_year=2026)
        self.assertTrue(r.capped)
        self.assertEqual(r.term_months, 48)

    def test_2024_allows_60(self):
        r = resolve_crediauto_term(60, 2024, reference_year=2026)
        self.assertFalse(r.capped)
        self.assertEqual(r.term_months, 60)
        self.assertIsNone(r.note)

    def test_unknown_year_keeps_request(self):
        r = resolve_crediauto_term(60, None, reference_year=2026)
        self.assertFalse(r.capped)
        self.assertEqual(r.term_months, 60)
        self.assertIsNone(r.note)


class TestEngineTermCap(unittest.TestCase):
    def test_calculate_caps_2021_at_36(self):
        quote = CalibratedQuoteEngine().calculate(
            Decimal("450000"),
            60,
            down_payment=Decimal("90000"),
            vehicle_year=2021,
        )
        self.assertEqual(quote.term_months, 36)
        self.assertEqual(quote.requested_term_months, 60)
        self.assertEqual(quote.vehicle_year, 2021)
        self.assertIn("36 meses", quote.term_cap_note or "")
        self.assertEqual(len(quote.schedule), 37)  # opening + 36 amort


if __name__ == "__main__":
    unittest.main()
