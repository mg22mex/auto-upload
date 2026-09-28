"""Match free-text purchase interest against ``data/catalog_latest.json``.

Used so "ver un Corolla" binds the live lot (price + branch marker) instead of
falling through to sticky Autométrica trade-in context.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.inventory.snapshot import BRANCH_TAGS, load_catalog_snapshot
from src.models import Vehicle

ENV_CATALOG_PATH = "CATALOG_PATH"
DEFAULT_CATALOG_PATHS = (
    "data/catalog_latest.json",
    "data/snapshots/catalog_latest.json",
)


@dataclass(frozen=True)
class CatalogMatch:
    vehicle: Vehicle
    display_name: str
    branch_key: str | None
    branch_marker: str | None
    price: float | None


def catalog_paths() -> list[Path]:
    raw = (os.getenv(ENV_CATALOG_PATH) or "").strip()
    paths: list[Path] = []
    if raw:
        paths.append(Path(raw))
    paths.extend(Path(p) for p in DEFAULT_CATALOG_PATHS)
    return paths


def load_public_catalog() -> list[Vehicle]:
    for path in catalog_paths():
        if path.is_file():
            try:
                return load_catalog_snapshot(path)
            except Exception:
                continue
    return []


def _parse_price(raw: str | None) -> float | None:
    digits = re.sub(r"[^\d.]", "", str(raw or ""))
    if not digits:
        return None
    try:
        return float(digits)
    except ValueError:
        return None


def branch_from_catalog_vehicle(vehicle: Vehicle) -> tuple[str | None, str | None]:
    """Return ``(branch_key, marker)`` from title trailing ``*`` / ``+`` / ``-``."""
    from src.config import BRANCH_TAG_MAP

    for text in (
        vehicle.marketplace_title or "",
        vehicle.title or "",
    ):
        cleaned = text.rstrip()
        if cleaned and cleaned[-1] in BRANCH_TAGS:
            marker = cleaned[-1]
            return BRANCH_TAG_MAP.get(marker), marker
    return None, None


def _blob(vehicle: Vehicle) -> str:
    return " ".join(
        [
            vehicle.brand or "",
            vehicle.title or "",
            vehicle.version or "",
            vehicle.slug or "",
            vehicle.marketplace_title or "",
            vehicle.year or "",
        ]
    ).casefold()


def match_desired_in_catalog(
    desired: str,
    *,
    vehicles: list[Vehicle] | None = None,
) -> CatalogMatch | None:
    """Best public-stock hit for a desire label like ``Toyota Corolla`` / ``Corolla 2022``."""
    label = (desired or "").strip()
    if not label:
        return None
    stock = vehicles if vehicles is not None else load_public_catalog()
    if not stock:
        return None

    lowered = label.casefold()
    tokens = [t for t in re.split(r"[^\w]+", lowered) if t and t not in {"un", "una", "el", "la"}]
    year_m = re.search(r"(20\d{2}|19\d{2})", lowered)
    want_year = year_m.group(1) if year_m else None
    # Prefer model token (last known car name) over make.
    model_hints = [
        t
        for t in tokens
        if t
        not in {
            "toyota",
            "nissan",
            "mazda",
            "volkswagen",
            "vw",
            "honda",
            "ford",
            "chevrolet",
            "chevy",
            "kia",
            "hyundai",
            "audi",
            "bmw",
            "mercedes",
        }
        and not re.fullmatch(r"20\d{2}|19\d{2}", t)
    ]

    scored: list[tuple[int, Vehicle]] = []
    for vehicle in stock:
        blob = _blob(vehicle)
        score = 0
        for hint in model_hints:
            if hint and hint in blob:
                score += 10
        for token in tokens:
            if token in blob:
                score += 1
        if want_year and want_year in (vehicle.year or ""):
            score += 5
        elif want_year is None and model_hints:
            # Prefer newer lots when year omitted.
            try:
                score += min(3, max(0, int(vehicle.year or 0) - 2018) // 2)
            except ValueError:
                pass
        if score <= 0:
            continue
        # Require at least one model hint hit when present.
        if model_hints and not any(h in blob for h in model_hints):
            continue
        scored.append((score, vehicle))

    if not scored:
        return None
    scored.sort(
        key=lambda item: (
            -item[0],
            -(int(item[1].year) if str(item[1].year).isdigit() else 0),
            item[1].autosell_id,
        )
    )
    best = scored[0][1]
    branch_key, marker = branch_from_catalog_vehicle(best)
    # Keep marker on display so CRM / RR can resolve lot.
    display = (best.marketplace_title or "").strip()
    if not display:
        parts = [best.year, best.brand, best.title]
        display = " ".join(p for p in parts if p).strip()
    return CatalogMatch(
        vehicle=best,
        display_name=display,
        branch_key=branch_key,
        branch_marker=marker,
        price=_parse_price(best.price),
    )


def match_to_meta(match: CatalogMatch) -> dict[str, Any]:
    """Chat-meta fields for a catalog hit."""
    meta: dict[str, Any] = {
        "interested_vehicle": match.display_name,
        "vehicle_name": match.display_name,
        "vehicle_title_raw": match.display_name,
        "autosell_id": match.vehicle.autosell_id,
    }
    if match.price is not None:
        meta["vehicle_price"] = match.price
    if match.vehicle.year:
        try:
            meta["vehicle_year"] = int(match.vehicle.year)
        except ValueError:
            pass
    if match.branch_key:
        meta["vehicle_branch"] = match.branch_key
        meta["physical_location"] = match.branch_key
    if match.branch_marker:
        meta["branch_marker"] = match.branch_marker
    return meta


__all__ = [
    "CatalogMatch",
    "DEFAULT_CATALOG_PATHS",
    "ENV_CATALOG_PATH",
    "branch_from_catalog_vehicle",
    "catalog_paths",
    "load_public_catalog",
    "match_desired_in_catalog",
    "match_to_meta",
]
