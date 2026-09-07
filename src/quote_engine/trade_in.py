"""Trade-in valuation — Autotécnica / Libro Azul placeholder.

Outputs net equity for use as `net_trade_in_equity` in `calculate_quote`.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any

TWOPLACES = Decimal("0.01")


def _q(value: Decimal) -> Decimal:
    return value.quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def _d(value: Decimal | int | float | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


class ValuationSource(str, Enum):
    AUTOTECNICA = "autotecnica"
    LIBRO_AZUL = "libro_azul"
    AUTOMETRICA = "autometrica"
    MANUAL = "manual"


@dataclass(frozen=True)
class TradeInVehicle:
    """Minimal identity for guide lookup (placeholders until API wired)."""

    year: int
    make: str
    model: str
    version: str = ""
    mileage_km: int = 0
    vin: str = ""


@dataclass(frozen=True)
class TradeInValuation:
    source: ValuationSource
    guide_value: Decimal
    outstanding_lien: Decimal
    adjustments: Decimal
    net_equity: Decimal
    notes: str
    raw: dict[str, Any]


class TradeInEngine:
    """Trade-in appraiser → net equity (Autométrica Valor Compra preferred).

    Lookup order: ``manual_guide_value`` → Autométrica fallback table → stub.
    """

    # Conservative haircut on stub guide values (not live market data)
    STUB_GUIDE_HAIRCUT = Decimal("0.92")

    def __init__(
        self, *, preferred_source: ValuationSource = ValuationSource.AUTOMETRICA
    ):
        self.preferred_source = preferred_source

    def value(
        self,
        vehicle: TradeInVehicle,
        *,
        outstanding_lien: Decimal | int | float | str = Decimal("0"),
        condition_adjustment: Decimal | int | float | str = Decimal("0"),
        manual_guide_value: Decimal | int | float | str | None = None,
        source: ValuationSource | None = None,
    ) -> TradeInValuation:
        src = source or self.preferred_source
        lien = _q(_d(outstanding_lien))
        adj = _q(_d(condition_adjustment))

        if manual_guide_value is not None:
            guide = _q(_d(manual_guide_value))
            notes = "manual guide override"
            raw: dict[str, Any] = {"mode": "manual"}
            used = ValuationSource.MANUAL
        elif src == ValuationSource.AUTOMETRICA:
            guide, notes, raw = self._fetch_autometrica(vehicle)
            used = ValuationSource.AUTOMETRICA
        else:
            guide, notes, raw = self._fetch_guide_value(vehicle, src)
            used = src

        net = _q(guide + adj - lien)
        if net < 0:
            net = Decimal("0.00")

        return TradeInValuation(
            source=used,
            guide_value=guide,
            outstanding_lien=lien,
            adjustments=adj,
            net_equity=net,
            notes=notes,
            raw=raw,
        )

    def net_equity_for_down(
        self,
        vehicle: TradeInVehicle,
        **kwargs: Any,
    ) -> Decimal:
        """Convenience: equity ready for `calculate_quote(..., net_trade_in_equity=...)`."""
        return self.value(vehicle, **kwargs).net_equity

    def _fetch_autometrica(
        self, vehicle: TradeInVehicle
    ) -> tuple[Decimal, str, dict[str, Any]]:
        from src.quote_engine.autometrica import lookup_valor_compra

        val = lookup_valor_compra(
            year=int(vehicle.year),
            make=vehicle.make,
            model=vehicle.model,
            version=vehicle.version,
            mileage_km=int(vehicle.mileage_km or 0),
        )
        notes = (
            f"Autométrica Valor Compra={val.valor_compra} "
            f"(mileage_adj={val.mileage_adjustment}, matched={val.matched})"
        )
        raw = {
            "mode": "autometrica",
            "source": val.source,
            "valor_compra": str(val.valor_compra),
            "valor_venta": str(val.valor_venta),
            "baseline_km": val.baseline_km,
            "mileage_km": val.mileage_km,
            "mileage_adjustment": str(val.mileage_adjustment),
            "matched": val.matched,
            "notes": val.notes,
        }
        return val.valor_compra, notes, raw

    def _fetch_guide_value(
        self,
        vehicle: TradeInVehicle,
        source: ValuationSource,
    ) -> tuple[Decimal, str, dict[str, Any]]:
        """Stub lookup — replace with Autotécnica / Libro Azul API clients."""
        # Placeholder: age-decay stub so pipeline works without credentials.
        age = max(0, 2026 - int(vehicle.year))
        base = Decimal("280000") - (Decimal(age) * Decimal("18000"))
        if vehicle.mileage_km > 100_000:
            base -= Decimal("15000")
        elif vehicle.mileage_km > 60_000:
            base -= Decimal("8000")
        base = max(base, Decimal("25000"))
        guide = _q(base * self.STUB_GUIDE_HAIRCUT)
        notes = (
            f"PLACEHOLDER {source.value}: stub estimate only — "
            "prefer Autométrica; drop Scotiabank PDFs in docs/scotiabank_samples/"
        )
        raw = {
            "mode": "placeholder",
            "source": source.value,
            "year": vehicle.year,
            "make": vehicle.make,
            "model": vehicle.model,
            "version": vehicle.version,
            "mileage_km": vehicle.mileage_km,
            "stub_base_before_haircut": str(_q(base)),
        }
        return guide, notes, raw
