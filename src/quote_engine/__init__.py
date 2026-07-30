"""Local French Amortization financial calculator (Phase 2).

Pure Python — no network I/O. Compute quotes locally before any external payload.
"""
from src.quote_engine.calculator import (
    DEFAULT_ANNUAL_RATE,
    DEFAULT_ORIGINATION_FEE_RATE,
    IVA_RATE,
    MIN_DOWN_PAYMENT_RATE,
    QuoteResult,
    ScheduleRow,
    calculate_quote,
    calculate_quote_scotiabank,
    french_payment,
    quote_matrix,
    resolve_down_with_trade_in,
)
from src.quote_engine.engine import CalibratedQuoteEngine
from src.quote_engine.scotiabank_profile import (
    SCOTIABANK_PROFILE,
    ScotiabankProfile,
)
from src.quote_engine.trade_in import (
    TradeInEngine,
    TradeInValuation,
    TradeInVehicle,
    ValuationSource,
)

__all__ = [
    "DEFAULT_ANNUAL_RATE",
    "DEFAULT_ORIGINATION_FEE_RATE",
    "IVA_RATE",
    "MIN_DOWN_PAYMENT_RATE",
    "CalibratedQuoteEngine",
    "QuoteResult",
    "SCOTIABANK_PROFILE",
    "ScheduleRow",
    "ScotiabankProfile",
    "TradeInEngine",
    "TradeInValuation",
    "TradeInVehicle",
    "ValuationSource",
    "calculate_quote",
    "calculate_quote_scotiabank",
    "french_payment",
    "quote_matrix",
    "resolve_down_with_trade_in",
]
