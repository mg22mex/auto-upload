"""Scotiabank CrediAuto ANCA — coefficients from docs/scotiabank_samples/.

Calibrated against:
  tablaAmortizacion1.pdf  Vento 2018 / 12m / 40% down
  tablaAmortizacion2.pdf  Hilux 2023 / 36m / 5% down
  tablaAmortizacion3.pdf  Sierra 2024 / 60m / 5% down
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

# --- Exact coefficients (all three samples agree) ---

ANNUAL_INTEREST_RATE = Decimal("0.1299")
"""Tasa fija anual."""

OPENING_FEE_RATE = Decimal("0.025")
"""Comisión por apertura / contratación-crédito (% of importe a financiar)."""

IVA_RATE = Decimal("0.16")
"""IVA 16%: (1) on interest each period (2) on opening commission."""

ANNUAL_LIFE_UNEMPLOYMENT = Decimal("1700.00")
"""Seguro de vida con desempleo — flat annual premium (all samples)."""

MONTHLY_LIFE_UNEMPLOYMENT = (
    ANNUAL_LIFE_UNEMPLOYMENT / Decimal("12")
).quantize(Decimal("0.01"))
"""Monthly life/unemployment factor = 1700/12 → 141.67."""

ANNUAL_ADMIN_FEE = Decimal("696.00")
"""Costo administración — financed once per insurance year."""

MONTHLY_ADMIN_FEE = (ANNUAL_ADMIN_FEE / Decimal("12")).quantize(Decimal("0.01"))
"""Monthly admin/account fee equivalent = 696/12 → 58.00."""

ADDITIONAL_COVERAGES = Decimal("9860.00")
"""Coberturas adicionales — financed into importe."""

CERTIFICATE_RENEWAL_DEFAULT = Decimal("928.00")
"""Certificado de renovación (optional; 0 on some corridas)."""

MIN_DOWN_PAYMENT_RATE = Decimal("0.10")
"""Dealer floor for cash quotes (bank samples may show 5%)."""

# Day-count: opening month uses tasa/12; amortizing months use 365/360.
DAY_COUNT_NUMERATOR = 365
DAY_COUNT_DENOMINATOR = 360


@dataclass(frozen=True)
class ScotiabankProfile:
    """Bank profile bundle for `calculate_quote(..., profile=...)`."""

    name: str = "scotiabank_crediauto_anca"
    annual_interest_rate: Decimal = ANNUAL_INTEREST_RATE
    opening_fee_rate: Decimal = OPENING_FEE_RATE
    iva_rate: Decimal = IVA_RATE
    annual_life_unemployment: Decimal = ANNUAL_LIFE_UNEMPLOYMENT
    annual_admin_fee: Decimal = ANNUAL_ADMIN_FEE
    additional_coverages: Decimal = ADDITIONAL_COVERAGES
    certificate_renewal: Decimal = Decimal("0.00")
    min_down_payment_rate: Decimal = MIN_DOWN_PAYMENT_RATE
    day_count_numerator: int = DAY_COUNT_NUMERATOR
    day_count_denominator: int = DAY_COUNT_DENOMINATOR

    def monthly_rate_opening(self) -> Decimal:
        """Month-1 interest rate (tasa/12)."""
        return self.annual_interest_rate / Decimal("12")

    def monthly_rate_amortizing(self) -> Decimal:
        """Amortizing months: tasa × 365/360/12."""
        return (
            self.annual_interest_rate
            * Decimal(self.day_count_numerator)
            / Decimal(self.day_count_denominator)
            / Decimal("12")
        )

    def monthly_life_unemployment(self) -> Decimal:
        return (self.annual_life_unemployment / Decimal("12")).quantize(Decimal("0.01"))

    def monthly_admin_fee(self) -> Decimal:
        return (self.annual_admin_fee / Decimal("12")).quantize(Decimal("0.01"))

    def opening_fee_gross(self, financed_principal: Decimal) -> Decimal:
        """Contratación-crédito incl. IVA = financed × 2.5% × 1.16."""
        from decimal import ROUND_HALF_UP

        raw = financed_principal * self.opening_fee_rate * (Decimal("1") + self.iva_rate)
        return raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def opening_fee_base(self, financed_principal: Decimal) -> Decimal:
        """Commission before IVA."""
        from decimal import ROUND_HALF_UP

        raw = financed_principal * self.opening_fee_rate
        return raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def annual_renewal_premium(self, annual_auto_insurance: Decimal) -> Decimal:
        """Amount capitalized each policy year (daños + vida + admin)."""
        return annual_auto_insurance + self.annual_life_unemployment + self.annual_admin_fee


# Default profile (cert renewal off — enable per deal when PDF shows 928).
SCOTIABANK_PROFILE = ScotiabankProfile()

IVA_BREAKDOWN = {
    "interest": "IVA_RATE × interest each schedule row (amortizing + opening)",
    "opening_fee": "OPENING_FEE_RATE × financed, then × (1+IVA_RATE) → Contratación-crédito",
    "not_on_principal": "IVA is not charged on abono a capital",
    "not_on_insurance_premium": "Seguro daños/vida/admin enter financed principal pre-IVA",
}
