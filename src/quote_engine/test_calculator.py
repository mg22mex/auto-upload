"""Unit tests — Scotiabank sample PDF accuracy + $300k simple quotes."""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.quote_engine.calculator import (
    DEFAULT_ANNUAL_RATE,
    DEFAULT_ORIGINATION_FEE_RATE,
    IVA_RATE,
    MIN_DOWN_PAYMENT_RATE,
    calculate_quote,
    french_payment,
    quote_matrix,
    resolve_down_payment,
)
from src.quote_engine.scotiabank_profile import (
    ANNUAL_ADMIN_FEE,
    ANNUAL_INTEREST_RATE,
    ANNUAL_LIFE_UNEMPLOYMENT,
    IVA_BREAKDOWN,
    MONTHLY_ADMIN_FEE,
    MONTHLY_LIFE_UNEMPLOYMENT,
    OPENING_FEE_RATE,
    SCOTIABANK_PROFILE,
    ScotiabankProfile,
)

PRICE = Decimal("300000.00")
AUTO_ANNUAL = Decimal("12000.00")
LIFE_MONTHLY = Decimal("141.67")


class TestDownPayment(unittest.TestCase):
    def test_min_10_percent(self):
        self.assertEqual(resolve_down_payment(PRICE, None), Decimal("30000.00"))
        self.assertEqual(resolve_down_payment(PRICE, Decimal("10000")), Decimal("30000.00"))
        self.assertEqual(resolve_down_payment(PRICE, Decimal("50000")), Decimal("50000.00"))

    def test_reject_full_price(self):
        with self.assertRaises(ValueError):
            resolve_down_payment(PRICE, PRICE)


class TestFrenchPayment(unittest.TestCase):
    def test_zero_rate(self):
        self.assertEqual(
            french_payment(Decimal("1200"), Decimal("0"), 12),
            Decimal("100.00"),
        )


class TestScotiabankCoefficients(unittest.TestCase):
    def test_extracted_constants(self):
        self.assertEqual(ANNUAL_INTEREST_RATE, Decimal("0.1299"))
        self.assertEqual(OPENING_FEE_RATE, Decimal("0.025"))
        self.assertEqual(ANNUAL_LIFE_UNEMPLOYMENT, Decimal("1700.00"))
        self.assertEqual(MONTHLY_LIFE_UNEMPLOYMENT, Decimal("141.67"))
        self.assertEqual(ANNUAL_ADMIN_FEE, Decimal("696.00"))
        self.assertEqual(MONTHLY_ADMIN_FEE, Decimal("58.00"))
        self.assertEqual(SCOTIABANK_PROFILE.iva_rate, Decimal("0.16"))
        self.assertIn("interest", IVA_BREAKDOWN)
        self.assertIn("opening_fee", IVA_BREAKDOWN)

    def test_day_count_rates(self):
        p = SCOTIABANK_PROFILE
        self.assertEqual(p.monthly_rate_opening(), Decimal("0.1299") / 12)
        expected = Decimal("0.1299") * Decimal("365") / Decimal("360") / Decimal("12")
        self.assertEqual(p.monthly_rate_amortizing(), expected)


class TestScotiabankSamples(unittest.TestCase):
    """Accuracy vs docs/scotiabank_samples/tablaAmortizacion{1,2,3}.pdf."""

    def test_sample1_vento_12m(self):
        q = calculate_quote(
            Decimal("150000"),
            12,
            profile=SCOTIABANK_PROFILE,
            down_payment=Decimal("60000"),
            annual_auto_insurance=Decimal("10533.39"),
            include_certificate_renewal=True,
            enforce_min_down=False,
        )
        self.assertEqual(q.financed_principal, Decimal("113717.39"))
        self.assertEqual(q.origination_fee, Decimal("3297.80"))
        self.assertEqual(q.base_monthly_payment, Decimal("10278.85"))
        self.assertEqual(q.schedule[0].interest, Decimal("1230.99"))
        self.assertEqual(q.schedule[0].total_payment, Decimal("4725.75"))
        self.assertEqual(q.schedule[1].interest, Decimal("1248.09"))
        self.assertEqual(q.schedule[1].base_payment, Decimal("10278.85"))
        self.assertAlmostEqual(
            float(q.schedule[1].principal), 8831.06, delta=0.02
        )

    def test_sample2_hilux_36m(self):
        q = calculate_quote(
            Decimal("475000"),
            36,
            profile=SCOTIABANK_PROFILE,
            down_payment=Decimal("23750"),
            annual_auto_insurance=Decimal("24798.09"),
            include_certificate_renewal=False,
            enforce_min_down=False,
        )
        self.assertEqual(q.financed_principal, Decimal("488304.09"))
        self.assertEqual(q.origination_fee, Decimal("14160.82"))
        self.assertEqual(q.base_monthly_payment, Decimal("18505.36"))
        self.assertEqual(q.schedule[1].interest, Decimal("5359.31"))
        self.assertEqual(q.schedule[1].iva, Decimal("857.49"))
        # Insurance capitalized after month 13 → month 14 start
        self.assertEqual(q.schedule[12].insurance_capitalized, Decimal("27194.09"))
        self.assertEqual(q.schedule[13].beginning_balance, Decimal("357258.64"))

    def test_sample3_sierra_60m(self):
        q = calculate_quote(
            Decimal("1299000"),
            60,
            profile=SCOTIABANK_PROFILE,
            down_payment=Decimal("64950"),
            annual_auto_insurance=Decimal("62125.95"),
            include_certificate_renewal=False,
            enforce_min_down=False,
        )
        self.assertEqual(q.financed_principal, Decimal("1308431.95"))
        self.assertEqual(q.origination_fee, Decimal("37944.53"))
        self.assertEqual(q.base_monthly_payment, Decimal("35606.25"))
        self.assertEqual(q.schedule[0].interest, Decimal("14163.78"))
        self.assertEqual(q.schedule[1].interest, Decimal("14360.49"))


class TestQuote300k(unittest.TestCase):
    """$300k vehicle @ min 10% down — simple (non-bank) path."""

    @classmethod
    def setUpClass(cls):
        cls.quotes = quote_matrix(
            PRICE,
            terms=(12, 24, 36, 48),
            annual_auto_insurance=AUTO_ANNUAL,
            monthly_life_insurance=LIFE_MONTHLY,
        )

    def test_shared_principal_math(self):
        for n, q in self.quotes.items():
            with self.subTest(term=n):
                self.assertEqual(q.profile_name, "simple")
                self.assertEqual(q.vehicle_price, PRICE)
                self.assertEqual(q.annual_rate, DEFAULT_ANNUAL_RATE)
                self.assertEqual(q.down_payment, Decimal("30000.00"))
                self.assertEqual(q.cash_down_payment, Decimal("30000.00"))
                self.assertEqual(q.net_trade_in_equity, Decimal("0.00"))
                self.assertEqual(q.down_payment_pct, Decimal("10.00"))
                self.assertEqual(q.amount_to_finance, Decimal("270000.00"))
                self.assertEqual(
                    q.origination_fee,
                    (Decimal("270000.00") * DEFAULT_ORIGINATION_FEE_RATE).quantize(
                        Decimal("0.01")
                    ),
                )
                self.assertEqual(q.origination_fee, Decimal("6750.00"))
                self.assertEqual(q.financed_principal, Decimal("276750.00"))
                self.assertEqual(q.monthly_auto_insurance, Decimal("1000.00"))
                self.assertEqual(q.monthly_life_insurance, LIFE_MONTHLY)
                self.assertGreaterEqual(
                    q.down_payment / q.vehicle_price, MIN_DOWN_PAYMENT_RATE
                )

    def test_base_payments_by_term(self):
        expected_base = {
            12: Decimal("24717.26"),
            24: Decimal("13155.90"),
            36: Decimal("9323.47"),
            48: Decimal("7423.14"),
        }
        for n, base in expected_base.items():
            with self.subTest(term=n):
                self.assertEqual(self.quotes[n].base_monthly_payment, base)

    def test_estimated_monthly_by_term(self):
        expected_est = {
            12: Decimal("26123.69"),
            24: Decimal("14557.51"),
            36: Decimal("10726.90"),
            48: Decimal("8830.01"),
        }
        for n, est in expected_est.items():
            with self.subTest(term=n):
                q = self.quotes[n]
                self.assertEqual(q.estimated_monthly_payment, est)
                rebuilt = (
                    q.base_monthly_payment
                    + q.monthly_auto_insurance
                    + q.monthly_life_insurance
                    + q.average_monthly_iva
                ).quantize(Decimal("0.01"))
                self.assertEqual(q.estimated_monthly_payment, rebuilt)

    def test_schedule_invariants(self):
        for n, q in self.quotes.items():
            with self.subTest(term=n):
                self.assertEqual(len(q.schedule), n)
                self.assertEqual(q.schedule[0].beginning_balance, q.financed_principal)
                self.assertEqual(q.schedule[-1].ending_balance, Decimal("0.00"))
                for row in q.schedule:
                    self.assertEqual(
                        row.iva, (row.interest * IVA_RATE).quantize(Decimal("0.01"))
                    )
                    self.assertEqual(
                        row.total_payment,
                        (
                            row.base_payment
                            + row.iva
                            + row.auto_insurance
                            + row.life_insurance
                        ).quantize(Decimal("0.01")),
                    )
                    self.assertEqual(
                        row.ending_balance,
                        (row.beginning_balance - row.principal).quantize(Decimal("0.01")),
                    )

    def test_below_min_down_bumped(self):
        q = calculate_quote(
            PRICE,
            24,
            down_payment=Decimal("5000"),
            annual_auto_insurance=AUTO_ANNUAL,
            monthly_life_insurance=LIFE_MONTHLY,
        )
        self.assertEqual(q.down_payment, Decimal("30000.00"))
        self.assertEqual(q.cash_down_payment, Decimal("30000.00"))
        self.assertEqual(q.financed_principal, Decimal("276750.00"))

    def test_trade_in_cuts_cash_and_principal(self):
        q = calculate_quote(
            PRICE,
            24,
            net_trade_in_equity=Decimal("50000"),
            annual_auto_insurance=AUTO_ANNUAL,
            monthly_life_insurance=LIFE_MONTHLY,
        )
        self.assertEqual(q.net_trade_in_equity, Decimal("50000.00"))
        self.assertEqual(q.cash_down_payment, Decimal("0.00"))
        self.assertEqual(q.down_payment, Decimal("50000.00"))
        self.assertEqual(q.amount_to_finance, Decimal("250000.00"))
        self.assertEqual(q.origination_fee, Decimal("6250.00"))
        self.assertEqual(q.financed_principal, Decimal("256250.00"))
        self.assertLess(q.base_monthly_payment, self.quotes[24].base_monthly_payment)

    def test_trade_in_partial_cash(self):
        q = calculate_quote(
            PRICE,
            12,
            net_trade_in_equity=Decimal("10000"),
        )
        self.assertEqual(q.cash_down_payment, Decimal("20000.00"))
        self.assertEqual(q.down_payment, Decimal("30000.00"))
        self.assertEqual(q.amount_to_finance, Decimal("270000.00"))


if __name__ == "__main__":
    unittest.main()
