from __future__ import annotations

import random
import re
import time
from pathlib import Path

from src.models import Vehicle


def parse_mxn_price(price: str) -> str:
    digits = re.sub(r"[^\d]", "", price)
    if not digits:
        raise ValueError(f"Could not parse price: {price!r}")
    return digits


DEFAULT_MILEAGE_KM = "12345"


def parse_mileage_km(mileage: str) -> str:
    match = re.search(r"[\d,]+", mileage or "")
    if not match:
        return DEFAULT_MILEAGE_KM
    digits = match.group(0).replace(",", "")
    if not digits or digits == "0":
        return DEFAULT_MILEAGE_KM
    return digits


def mileage_for_listing(mileage: str) -> str:
    return parse_mileage_km(mileage)


def infer_fb_vehicle_type(vehicle: Vehicle) -> str:
    """Best-effort FB Marketplace vehicle type (English UI labels)."""
    haystack = " ".join(
        part
        for part in (vehicle.brand, vehicle.title, vehicle.version, vehicle.slug)
        if part
    ).lower()

    truck_markers = (
        "ram", "f-150", "f150", "silverado", "tundra", "tacoma", "ranger",
        "frontier", "np300", "titan", "1500", "2500", "3500", "cheyenne",
        "l200", "amarok", "colorado", "canyon", "maverick", "ridgeline",
        "camioneta", "pickup", "truck",
    )
    suv_markers = (
        "suv", "cx-", "cx50", "cx-5", "cx-9", "x-trail", "xtrail", "cr-v",
        "rav4", "tucson", "explorer", "expedition", "equinox", "traverse",
        "santa fe", "tahoe", "suburban", "q3", "q5", "q7", "q8", "x3", "x5",
        "glc", "gle", "macan", "cayenne", "escape", "edge", "bronco",
        "pathfinder", "murano", "rogue", "kicks", "seltos", "sportage",
        "outlander", "pilot", "highlander", "4runner", "sequoia",
    )
    van_markers = (
        "sedona", "caravan", "pacifica", "odyssey", "sienna", "transit",
        "sprinter", "promaster", "nv200", "nv350", "urvan", "minivan",
    )
    coupe_markers = ("coupe", "coupé", "mustang", "camaro", "corvette", "86", "brz")
    hatch_markers = ("hatch", "golf", "fit", "march", "spark", "i10", "i20")

    if any(marker in haystack for marker in truck_markers):
        return "Truck"
    if any(marker in haystack for marker in van_markers):
        return "Minivan"
    if any(marker in haystack for marker in suv_markers):
        return "SUV"
    if any(marker in haystack for marker in coupe_markers):
        return "Coupe"
    if any(marker in haystack for marker in hatch_markers):
        return "Hatchback"
    return "Sedan"


def fb_vehicle_type_candidates(vehicle: Vehicle) -> tuple[str, ...]:
    primary = infer_fb_vehicle_type(vehicle)
    aliases: dict[str, tuple[str, ...]] = {
        "Sedan": ("Sedan", "Sedán", "Saloon", "Car"),
        "SUV": ("SUV", "Crossover", "Camioneta SUV"),
        "Truck": ("Truck", "Pickup truck", "Pick-up", "Camioneta", "Pickup"),
        "Minivan": ("Minivan", "Van", "Minivan / Van"),
        "Coupe": ("Coupe", "Coupé"),
        "Hatchback": ("Hatchback", "Compact"),
    }
    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in (primary, *aliases.get(primary, ()), "Other", "Otro"):
        key = candidate.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(candidate)
    return tuple(ordered)


def vehicle_description(vehicle: Vehicle) -> str:
    lines = [vehicle.marketplace_title]
    lines.append(f"Kilometraje: {mileage_for_listing(vehicle.mileage)} km")
    for key, value in vehicle.specs.items():
        if value and key.lower() not in {"kilometraje", "precio"}:
            lines.append(f"{key}: {value}")
    lines.append(f"Más información: {vehicle.url}")
    return "\n".join(lines)


def random_delay(min_sec: float, max_sec: float) -> None:
    if max_sec <= 0:
        return
    low = max(0.0, min_sec)
    high = max(low, max_sec)
    time.sleep(random.uniform(low, high))


def ensure_log_dir(log_dir: str | Path) -> Path:
    path = Path(log_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path
