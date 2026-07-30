"""French Amortization Engine — simple quotes + Scotiabank CrediAuto profile.

Local-only. No network I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from src.quote_engine.scotiabank_profile import ScotiabankProfile

TWOPLACES = Decimal("0.01")

# Backward-compatible defaults (= Scotiabank headline rate/fee)
DEFAULT_ANNUAL_RATE = Decimal("0.1299")
DEFAULT_ORIGINATION_FEE_RATE = Decimal("0.025")
MIN_DOWN_PAYMENT_RATE = Decimal("0.10")
IVA_RATE = Decimal("0.16")
DEFAULT_ANNUAL_LIFE_INSURANCE = Decimal("1700.00")


def _q(value: Decimal) -> Decimal:
    return value.quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def _d(value: Decimal | int | float | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True)
class ScheduleRow:
    month: int
    beginning_balance: Decimal
    interest: Decimal
    iva: Decimal
    principal: Decimal
    base_payment: Decimal
    auto_insurance: Decimal
    life_insurance: Decimal
    total_payment: Decimal
    ending_balance: Decimal
    opening_fee: Decimal = Decimal("0.00")
    admin_fee: Decimal = Decimal("0.00")
    is_opening: bool = False
    insurance_capitalized: Decimal = Decimal("0.00")


@dataclass(frozen=True)
class QuoteResult:
    vehicle_price: Decimal
    term_months: int
    annual_rate: Decimal
    down_payment: Decimal  # total credit: cash + trade-in
    cash_down_payment: Decimal
    net_trade_in_equity: Decimal
    down_payment_pct: Decimal
    amount_to_finance: Decimal
    origination_fee: Decimal
    financed_principal: Decimal
    base_monthly_payment: Decimal
    monthly_auto_insurance: Decimal
    monthly_life_insurance: Decimal
    average_monthly_iva: Decimal
    estimated_monthly_payment: Decimal
    schedule: tuple[ScheduleRow, ...]
    profile_name: str = "simple"
    monthly_admin_fee: Decimal = Decimal("0.00")
    opening_fee_iva: Decimal = Decimal("0.00")


def french_payment(principal: Decimal, monthly_rate: Decimal, term_months: int) -> Decimal:
    """Classic French fixed installment (interest + principal), or IVA-gross rate."""
    if term_months <= 0:
        raise ValueError("term_months must be positive")
    if principal <= 0:
        return _q(Decimal("0"))
    if monthly_rate == 0:
        return _q(principal / term_months)
    factor = (Decimal("1") + monthly_rate) ** term_months
    return _q(principal * monthly_rate * factor / (factor - Decimal("1")))


def resolve_down_payment(
    vehicle_price: Decimal,
    down_payment: Decimal | None,
    *,
    min_rate: Decimal = MIN_DOWN_PAYMENT_RATE,
) -> Decimal:
    """Enforce min enganche (cash-only path, no trade-in)."""
    minimum = _q(vehicle_price * min_rate)
    if down_payment is None:
        return minimum
    requested = _q(_d(down_payment))
    if requested < minimum:
        return minimum
    if requested >= vehicle_price:
        raise ValueError("down_payment must be less than vehicle_price")
    return requested


def resolve_down_with_trade_in(
    vehicle_price: Decimal,
    down_payment: Decimal | None,
    net_trade_in_equity: Decimal,
    *,
    min_rate: Decimal = MIN_DOWN_PAYMENT_RATE,
) -> tuple[Decimal, Decimal, Decimal]:
    """Split total down into cash + trade-in; equity cuts cash need and principal."""
    minimum = _q(vehicle_price * min_rate)
    equity = _q(max(Decimal("0"), net_trade_in_equity))
    if equity >= vehicle_price:
        raise ValueError("net_trade_in_equity must be less than vehicle_price")

    if down_payment is None:
        cash = _q(max(Decimal("0"), minimum - equity))
    else:
        cash = _q(_d(down_payment))
        if cash < 0:
            raise ValueError("down_payment cannot be negative")
        if cash + equity < minimum:
            cash = _q(minimum - equity)

    total = _q(cash + equity)
    if total >= vehicle_price:
        raise ValueError("cash + trade-in equity must be less than vehicle_price")
    return cash, equity, total


def _pv_future_renewals(
    renewal: Decimal,
    rate_eff: Decimal,
    term_months: int,
) -> Decimal:
    """PV of insurance renewals capitalized at months 12, 24, … before term end."""
    years = term_months // 12
    if years <= 1 or renewal <= 0:
        return Decimal("0.00")
    total = Decimal("0")
    one = Decimal("1")
    for y in range(1, years):
        total += renewal / ((one + rate_eff) ** (12 * y))
    return total


def calculate_quote_scotiabank(
    vehicle_price: Decimal | int | float | str,
    term_months: int,
    *,
    profile: ScotiabankProfile,
    down_payment: Decimal | int | float | str | None = None,
    net_trade_in_equity: Decimal | int | float | str | None = None,
    annual_auto_insurance: Decimal | int | float | str = Decimal("0"),
    include_additional_coverages: bool = True,
    include_certificate_renewal: bool = False,
    enforce_min_down: bool = True,
) -> QuoteResult:
    """Bank-accurate CrediAuto ANCA quote (matches docs/scotiabank_samples/)."""
    price = _q(_d(vehicle_price))
    if price <= 0:
        raise ValueError("vehicle_price must be positive")
    if term_months <= 0 or term_months % 12 != 0:
        raise ValueError("term_months must be a positive multiple of 12")

    auto_annual = _q(_d(annual_auto_insurance))
    equity_in = Decimal("0") if net_trade_in_equity is None else _d(net_trade_in_equity)
    min_rate = profile.min_down_payment_rate if enforce_min_down else Decimal("0")

    if enforce_min_down:
        cash, equity, total_down = resolve_down_with_trade_in(
            price,
            None if down_payment is None else _d(down_payment),
            equity_in,
            min_rate=min_rate,
        )
    else:
        # Honor sample enganches below dealer 10% floor (e.g. bank 5%).
        equity = _q(max(Decimal("0"), equity_in))
        if down_payment is None:
            cash = _q(price * Decimal("0.05"))
        else:
            cash = _q(_d(down_payment))
        total_down = _q(cash + equity)
        if total_down <= 0 or total_down >= price:
            raise ValueError("invalid down payment")

    cob = profile.additional_coverages if include_additional_coverages else Decimal("0.00")
    cert = (
        profile.certificate_renewal
        if include_certificate_renewal
        else Decimal("0.00")
    )
    if include_certificate_renewal and cert == 0:
        from src.quote_engine.scotiabank_profile import CERTIFICATE_RENEWAL_DEFAULT

        cert = CERTIFICATE_RENEWAL_DEFAULT

    net_vehicle = _q(price - total_down)
    financed = _q(
        net_vehicle
        + auto_annual
        + profile.annual_admin_fee
        + profile.annual_life_unemployment
        + cob
        + cert
    )
    opening_gross = profile.opening_fee_gross(financed)
    opening_base = profile.opening_fee_base(financed)
    opening_iva = _q(opening_gross - opening_base)

    r_open = profile.monthly_rate_opening()
    r_amort = profile.monthly_rate_amortizing()
    iva_r = profile.iva_rate
    rate_eff = r_amort * (Decimal("1") + iva_r)
    renewal = profile.annual_renewal_premium(auto_annual)
    p_eff = financed + _pv_future_renewals(renewal, rate_eff, term_months)
    mensualidad = french_payment(p_eff, rate_eff, term_months)

    life_m = profile.monthly_life_unemployment()
    admin_m = profile.monthly_admin_fee()
    auto_m = _q(auto_annual / 12)

    schedule: list[ScheduleRow] = []
    balance = financed
    iva_sum = Decimal("0")

    # Month 1 — comisión + interest + IVA only (no principal)
    interest = _q(balance * r_open)
    iva_amt = _q(interest * iva_r)
    iva_sum += iva_amt
    pay1 = _q(opening_gross + interest + iva_amt)
    schedule.append(
        ScheduleRow(
            month=1,
            beginning_balance=balance,
            interest=interest,
            iva=iva_amt,
            principal=Decimal("0.00"),
            base_payment=pay1,
            auto_insurance=Decimal("0.00"),
            life_insurance=Decimal("0.00"),
            total_payment=pay1,
            ending_balance=balance,
            opening_fee=opening_gross,
            admin_fee=Decimal("0.00"),
            is_opening=True,
            insurance_capitalized=renewal,
        )
    )

    for amort_i in range(1, term_months + 1):
        month = amort_i + 1
        beginning = balance
        interest = _q(beginning * r_amort)
        iva_amt = _q(interest * iva_r)
        iva_sum += iva_amt
        if amort_i == term_months:
            principal = beginning
            pay = _q(principal + interest + iva_amt)
        else:
            pay = mensualidad
            principal = _q(pay - interest - iva_amt)
            if principal > beginning:
                principal = beginning
                pay = _q(principal + interest + iva_amt)
        ending = _q(beginning - principal)
        if ending < 0:
            ending = Decimal("0.00")
        schedule.append(
            ScheduleRow(
                month=month,
                beginning_balance=beginning,
                interest=interest,
                iva=iva_amt,
                principal=principal,
                base_payment=pay,
                auto_insurance=Decimal("0.00"),
                life_insurance=Decimal("0.00"),
                total_payment=pay,
                ending_balance=ending,
                opening_fee=Decimal("0.00"),
                admin_fee=Decimal("0.00"),
                is_opening=False,
            )
        )
        balance = ending
        if amort_i % 12 == 0 and amort_i < term_months:
            balance = _q(balance + renewal)
            # annotate last row's ending after cap for clarity on next begin
            prev = schedule[-1]
            schedule[-1] = ScheduleRow(
                **{
                    **prev.__dict__,
                    "insurance_capitalized": renewal,
                    "ending_balance": balance,
                }
            )

    # IVA average over amortizing months (exclude opening for estimate stability)
    amort_rows = [r for r in schedule if not r.is_opening]
    avg_iva = _q(sum((r.iva for r in amort_rows), Decimal("0")) / term_months)

    return QuoteResult(
        vehicle_price=price,
        term_months=term_months,
        annual_rate=profile.annual_interest_rate,
        down_payment=total_down,
        cash_down_payment=cash,
        net_trade_in_equity=equity,
        down_payment_pct=_q(total_down / price * 100),
        amount_to_finance=net_vehicle,
        origination_fee=opening_gross,
        financed_principal=financed,
        base_monthly_payment=mensualidad,
        monthly_auto_insurance=auto_m,
        monthly_life_insurance=life_m,
        average_monthly_iva=avg_iva,
        estimated_monthly_payment=mensualidad,
        schedule=tuple(schedule),
        profile_name=profile.name,
        monthly_admin_fee=admin_m,
        opening_fee_iva=opening_iva,
    )


def calculate_quote(
    vehicle_price: Decimal | int | float | str,
    term_months: int,
    *,
    down_payment: Decimal | int | float | str | None = None,
    net_trade_in_equity: Decimal | int | float | str | None = None,
    annual_rate: Decimal | int | float | str = DEFAULT_ANNUAL_RATE,
    origination_fee_rate: Decimal | int | float | str = DEFAULT_ORIGINATION_FEE_RATE,
    annual_auto_insurance: Decimal | int | float | str = Decimal("0"),
    monthly_life_insurance: Decimal | int | float | str | None = None,
    iva_rate: Decimal | int | float | str = IVA_RATE,
    profile: ScotiabankProfile | None = None,
    include_additional_coverages: bool = True,
    include_certificate_renewal: bool = False,
    enforce_min_down: bool = True,
) -> QuoteResult:
    """Build quote. Pass `profile=SCOTIABANK_PROFILE` for bank-accurate ANCA math."""
    if profile is not None:
        return calculate_quote_scotiabank(
            vehicle_price,
            term_months,
            profile=profile,
            down_payment=down_payment,
            net_trade_in_equity=net_trade_in_equity,
            annual_auto_insurance=annual_auto_insurance,
            include_additional_coverages=include_additional_coverages,
            include_certificate_renewal=include_certificate_renewal,
            enforce_min_down=enforce_min_down,
        )

    price = _q(_d(vehicle_price))
    if price <= 0:
        raise ValueError("vehicle_price must be positive")
    if term_months <= 0:
        raise ValueError("term_months must be positive")

    rate = _d(annual_rate)
    fee_rate = _d(origination_fee_rate)
    iva_r = _d(iva_rate)
    auto_annual = _q(_d(annual_auto_insurance))
    if monthly_life_insurance is None:
        life_m = _q(DEFAULT_ANNUAL_LIFE_INSURANCE / 12)
    else:
        life_m = _q(_d(monthly_life_insurance))

    equity_in = (
        Decimal("0") if net_trade_in_equity is None else _d(net_trade_in_equity)
    )
    cash, equity, total_down = resolve_down_with_trade_in(
        price,
        None if down_payment is None else _d(down_payment),
        equity_in,
    )
    net = _q(price - total_down)
    origination = _q(net * fee_rate)
    financed = _q(net + origination)
    monthly_rate = rate / 12
    base = french_payment(financed, monthly_rate, term_months)
    auto_m = _q(auto_annual / 12)

    schedule: list[ScheduleRow] = []
    balance = financed
    iva_sum = Decimal("0")

    for month in range(1, term_months + 1):
        beginning = balance
        interest = _q(beginning * monthly_rate)
        iva = _q(interest * iva_r)
        if month == term_months:
            principal = beginning
            base_pay = _q(principal + interest)
        else:
            principal = _q(base - interest)
            if principal > beginning:
                principal = beginning
                base_pay = _q(principal + interest)
            else:
                base_pay = base
        ending = _q(beginning - principal)
        if ending < 0:
            ending = Decimal("0.00")
        total = _q(base_pay + iva + auto_m + life_m)
        iva_sum += iva
        schedule.append(
            ScheduleRow(
                month=month,
                beginning_balance=beginning,
                interest=interest,
                iva=iva,
                principal=principal,
                base_payment=base_pay,
                auto_insurance=auto_m,
                life_insurance=life_m,
                total_payment=total,
                ending_balance=ending,
            )
        )
        balance = ending

    avg_iva = _q(iva_sum / term_months)
    estimated = _q(base + auto_m + life_m + avg_iva)

    return QuoteResult(
        vehicle_price=price,
        term_months=term_months,
        annual_rate=rate,
        down_payment=total_down,
        cash_down_payment=cash,
        net_trade_in_equity=equity,
        down_payment_pct=_q(total_down / price * 100),
        amount_to_finance=net,
        origination_fee=origination,
        financed_principal=financed,
        base_monthly_payment=base,
        monthly_auto_insurance=auto_m,
        monthly_life_insurance=life_m,
        average_monthly_iva=avg_iva,
        estimated_monthly_payment=estimated,
        schedule=tuple(schedule),
        profile_name="simple",
    )


def quote_matrix(
    vehicle_price: Decimal | int | float | str,
    terms: Sequence[int] = (12, 24, 36, 48),
    **kwargs,
) -> dict[int, QuoteResult]:
    return {n: calculate_quote(vehicle_price, n, **kwargs) for n in terms}
