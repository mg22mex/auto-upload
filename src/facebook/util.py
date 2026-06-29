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


def vehicle_description(vehicle: Vehicle) -> str:
    lines = [vehicle.marketplace_title]
    for key, value in vehicle.specs.items():
        if value:
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
