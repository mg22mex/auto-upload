from __future__ import annotations

import json
from pathlib import Path

from src.models import Vehicle


def save_catalog_snapshot(vehicles: list[Vehicle], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "count": len(vehicles),
        "vehicles": [vehicle.to_dict() for vehicle in vehicles],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_catalog_snapshot(path: Path) -> list[Vehicle]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    vehicles: list[Vehicle] = []
    for item in payload.get("vehicles", []):
        vehicles.append(
            Vehicle(
                autosell_id=item["autosell_id"],
                slug=item["slug"],
                title=item["title"],
                brand=item["brand"],
                year=item.get("year", ""),
                price=item.get("price", ""),
                mileage=item.get("mileage", ""),
                version=item.get("version", ""),
                url=item["url"],
                image_urls=list(item.get("image_urls", [])),
                specs=dict(item.get("specs", {})),
            )
        )
    return vehicles
