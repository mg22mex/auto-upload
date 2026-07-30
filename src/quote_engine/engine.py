"""Calibrated Scotiabank quote facade for Phase 2 pipeline."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from src.quote_engine.calculator import QuoteResult, calculate_quote
from src.quote_engine.scotiabank_profile import SCOTIABANK_PROFILE, ScotiabankProfile


class CalibratedQuoteEngine:
    """Scotiabank CrediAuto ANCA quotes via `calculate_quote`."""

    def __init__(self, profile: ScotiabankProfile | None = None) -> None:
        self.profile = profile or SCOTIABANK_PROFILE

    def calculate(
        self,
        vehicle_price: Decimal | int | float | str,
        term_months: int,
        *,
        down_payment: Decimal | int | float | str | None = None,
        net_trade_in_equity: Decimal | int | float | str | None = None,
        annual_auto_insurance: Decimal | int | float | str = Decimal("0"),
        include_additional_coverages: bool = True,
        include_certificate_renewal: bool = False,
        enforce_min_down: bool = True,
        **_: Any,
    ) -> QuoteResult:
        return calculate_quote(
            vehicle_price,
            term_months,
            down_payment=down_payment,
            net_trade_in_equity=net_trade_in_equity,
            annual_auto_insurance=annual_auto_insurance,
            profile=self.profile,
            include_additional_coverages=include_additional_coverages,
            include_certificate_renewal=include_certificate_renewal,
            enforce_min_down=enforce_min_down,
        )
