"""Unit tests — Autométrica Valor Compra lookup + mileage adjustment."""
from __future__ import annotations

import unittest
from decimal import Decimal

from src.quote_engine.autometrica import apply_mileage_adjustment, lookup_valor_compra
from src.quote_engine.calculator import calculate_quote
from src.quote_engine.trade_in import TradeInEngine, TradeInVehicle, ValuationSource


class TestAutometricaLookup(unittest.TestCase):
    def test_corolla_le_mileage_adjustment(self):
        val = lookup_valor_compra(
            year=2020,
            make="Toyota",
            model="Corolla",
            version="LE",
            mileage_km=85000,
        )
        self.assertTrue(val.matched)
        self.assertEqual(val.baseline_km, 75000)
        # +10k km × -3500 = -3500
        self.assertEqual(val.mileage_adjustment, Decimal("-3500.00"))
        self.assertEqual(val.valor_compra, Decimal("194500.00"))

    def test_valor_compra_feeds_amortization(self):
        val = lookup_valor_compra(
            year=2020,
            make="Toyota",
            model="Corolla",
            version="LE",
            mileage_km=85000,
        )
        quote = calculate_quote(
            450000,
            36,
            net_trade_in_equity=val.valor_compra,
        )
        self.assertEqual(quote.net_trade_in_equity, val.valor_compra)
        self.assertEqual(quote.down_payment, val.valor_compra)
        self.assertLess(quote.financed_principal, Decimal("450000"))

    def test_trade_in_engine_prefers_autometrica(self):
        engine = TradeInEngine(preferred_source=ValuationSource.AUTOMETRICA)
        result = engine.value(
            TradeInVehicle(2020, "Toyota", "Corolla", version="Base", mileage_km=80000)
        )
        self.assertEqual(result.source, ValuationSource.AUTOMETRICA)
        self.assertEqual(result.net_equity, Decimal("185000.00"))

    def test_apply_mileage_adjustment_math(self):
        adjusted, delta = apply_mileage_adjustment(
            Decimal("100000"),
            mileage_km=100000,
            baseline_km=80000,
            per_10k=Decimal("-3500"),
        )
        self.assertEqual(delta, Decimal("-7000.00"))
        self.assertEqual(adjusted, Decimal("93000.00"))


if __name__ == "__main__":
    unittest.main()
