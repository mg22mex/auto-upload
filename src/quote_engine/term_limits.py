"""Scotiabank CrediAuto — max term (plazo) by vehicle model year.

Local-only. No network I/O.

Matrix (relative to *reference_year*, default = calendar year today):

| Model year age vs reference | Max term |
|-----------------------------|----------|
| year >= reference − 2       | 60 mo    |
| year in {reference−3, −4}   | 48 mo    |
| year <= reference − 5       | 36 mo    |

Example for reference_year=2026: 2024–2026→60, 2022–2023→48, ≤2021→36.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

_YEAR_RE = re.compile(r"(?:19|20)\d{2}")

TERM_CAP_NOTE_TEMPLATE = (
    "Nota: Por el año del vehículo ({year}), el plazo máximo disponible "
    "con Scotiabank es de {max_allowed_term} meses."
)


def reference_model_year(*, today: date | None = None) -> int:
    """Calendar year used as CrediAuto matrix anchor."""
    return (today or date.today()).year


def extract_model_year(value: Any) -> int | None:
    """Pull a 4-digit model year (1950–2100) from int/str / vehicle title."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    if isinstance(value, int):
        return value if 1950 <= value <= 2100 else None
    text = str(value).strip()
    if not text:
        return None
    matches = _YEAR_RE.findall(text)
    for token in reversed(matches):
        year = int(token)
        if 1950 <= year <= 2100:
            return year
    return None


def max_term_months_for_year(
    vehicle_year: int | None,
    *,
    reference_year: int | None = None,
) -> int | None:
    """Return CrediAuto max plazo for *vehicle_year*, or None if year unknown."""
    if vehicle_year is None:
        return None
    year = int(vehicle_year)
    if year < 1950 or year > 2100:
        return None
    ref = int(reference_year) if reference_year is not None else reference_model_year()
    age = ref - year
    if age <= 2:
        return 60
    if age in (3, 4):
        return 48
    return 36


@dataclass(frozen=True)
class TermResolution:
    """Requested vs effective CrediAuto term after year-based cap."""

    requested_term_months: int
    term_months: int
    max_allowed_term: int | None
    vehicle_year: int | None
    capped: bool
    note: str | None


def resolve_crediauto_term(
    requested_term_months: int,
    vehicle_year: int | None = None,
    *,
    reference_year: int | None = None,
) -> TermResolution:
    """Cap *requested_term_months* to the CrediAuto year matrix when year known.

    Terms are normalized to a positive multiple of 12 before capping (Scotiabank
    ANCA path). Without a usable year, the (normalized) request is unchanged.
    """
    requested = int(requested_term_months)
    if requested <= 0:
        raise ValueError("requested_term_months must be positive")
    # Align to 12-month buckets used by CrediAuto ANCA.
    if requested % 12 != 0:
        normalized = max(12, round(requested / 12) * 12)
    else:
        normalized = requested

    year = extract_model_year(vehicle_year)
    max_allowed = max_term_months_for_year(year, reference_year=reference_year)
    if max_allowed is None:
        return TermResolution(
            requested_term_months=requested,
            term_months=normalized,
            max_allowed_term=None,
            vehicle_year=year,
            capped=False,
            note=None,
        )

    effective = min(normalized, max_allowed)
    capped = effective < normalized
    note = (
        TERM_CAP_NOTE_TEMPLATE.format(year=year, max_allowed_term=max_allowed)
        if capped and year is not None
        else None
    )
    return TermResolution(
        requested_term_months=requested,
        term_months=effective,
        max_allowed_term=max_allowed,
        vehicle_year=year,
        capped=capped,
        note=note,
    )
