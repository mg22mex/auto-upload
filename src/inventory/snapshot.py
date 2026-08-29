from __future__ import annotations

import json
from pathlib import Path

from src.models import Vehicle

# Trailing marker on autosell.mx titles; carried verbatim into Marketplace titles.
BRANCH_TAGS = {"*": "periferico", "+": "san_felipe", "-": "consignment"}


def branch_tag(vehicle: Vehicle) -> str | None:
    """Return the trailing branch marker of a vehicle title, when present."""
    title = (vehicle.marketplace_title or "").rstrip()
    if title and title[-1] in BRANCH_TAGS:
        return title[-1]
    return None


def summarize_catalog(vehicles: list[Vehicle]) -> dict[str, object]:
    """Vehicle count plus branch-tag breakdown (``*`` / ``+`` / ``-`` / untagged)."""
    tags = {tag: 0 for tag in BRANCH_TAGS}
    untagged = 0
    for vehicle in vehicles:
        tag = branch_tag(vehicle)
        if tag is None:
            untagged += 1
        else:
            tags[tag] += 1
    return {"count": len(vehicles), "tags": tags, "untagged": untagged}


def format_catalog_summary(vehicles: list[Vehicle]) -> str:
    """One-line catalog report used by the bump/sync CLIs."""
    summary = summarize_catalog(vehicles)
    tags = summary["tags"]
    parts = [
        f"{tag} {BRANCH_TAGS[tag]}={count}" for tag, count in tags.items()  # type: ignore[index]
    ]
    return (
        f"Catalog: {summary['count']} vehicle(s)  |  "
        + "  ".join(parts)
        + f"  untagged={summary['untagged']}"
    )


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
