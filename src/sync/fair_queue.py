"""Brand-fair queue ordering for Marketplace create / relist batches.

Stops alphabetical Audi-first bias under daily posting caps by:
1. Preferring newest catalog entries (year, then obj id).
2. Round-robin across brands when ages/tiers tie.
"""
from __future__ import annotations

import re
from collections import defaultdict, deque
from typing import Callable, Iterable, Sequence, TypeVar

from src.models import Vehicle

T = TypeVar("T")

_YEAR_RE = re.compile(r"(20\d{2}|19\d{2})")


def vehicle_year_int(vehicle: Vehicle | None) -> int:
    if vehicle is None:
        return 0
    text = str(vehicle.year or "")
    match = _YEAR_RE.search(text)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return 0
    return 0


def autosell_id_number(autosell_id: str | None) -> int:
    digits = re.sub(r"\D", "", str(autosell_id or ""))
    if not digits:
        return 0
    try:
        return int(digits)
    except ValueError:
        return 0


def newest_catalog_key(vehicle: Vehicle) -> tuple[int, int, str]:
    """Sort key: newer model year first, then higher obj id, then id."""
    return (
        -vehicle_year_int(vehicle),
        -autosell_id_number(vehicle.autosell_id),
        vehicle.autosell_id,
    )


def order_vehicles_newest_first(vehicles: Sequence[Vehicle]) -> list[Vehicle]:
    """Deterministic newest-first catalog order (replaces A→Z slug sort)."""
    return sorted(vehicles, key=newest_catalog_key)


def brand_key(vehicle: Vehicle | None) -> str:
    brand = (vehicle.brand if vehicle else "") or ""
    brand = brand.strip().casefold()
    return brand or "?"


def brand_fair_round_robin(
    items: Sequence[T],
    *,
    brand_of: Callable[[T], str],
    primary_key: Callable[[T], tuple],
) -> list[T]:
    """Order ``items`` by ``primary_key`` within each brand, then interleave.

    Each round takes one item from every brand that still has remaining
    inventory (brand order = first appearance under ``primary_key``). This
    prevents a single marque from consuming a daily posting cap when many
    units share the same age / tier.
    """
    if not items:
        return []
    ranked = sorted(items, key=primary_key)
    buckets: dict[str, deque[T]] = defaultdict(deque)
    brand_order: list[str] = []
    for item in ranked:
        brand = brand_of(item) or "?"
        if brand not in buckets:
            brand_order.append(brand)
        buckets[brand].append(item)

    out: list[T] = []
    while any(buckets[b] for b in brand_order):
        progressed = False
        for brand in brand_order:
            if buckets[brand]:
                out.append(buckets[brand].popleft())
                progressed = True
        if not progressed:
            break
    return out


def brand_fair_ids(
    autosell_ids: Iterable[str],
    vehicles_by_id: dict[str, Vehicle],
    *,
    primary_key: Callable[[str], tuple] | None = None,
) -> list[str]:
    """Brand-fair order of ids using vehicle metadata."""

    def _primary(aid: str) -> tuple:
        if primary_key is not None:
            return primary_key(aid)
        vehicle = vehicles_by_id.get(aid)
        if vehicle is None:
            return (10_000, 0, aid)
        return newest_catalog_key(vehicle)

    ids = list(autosell_ids)
    return brand_fair_round_robin(
        ids,
        brand_of=lambda aid: brand_key(vehicles_by_id.get(aid)),
        primary_key=_primary,
    )


__all__ = [
    "autosell_id_number",
    "brand_fair_ids",
    "brand_fair_round_robin",
    "brand_key",
    "newest_catalog_key",
    "order_vehicles_newest_first",
    "vehicle_year_int",
]
