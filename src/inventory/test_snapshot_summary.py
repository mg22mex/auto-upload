from __future__ import annotations

from src.inventory.snapshot import (
    branch_tag,
    format_catalog_summary,
    load_catalog_snapshot,
    save_catalog_snapshot,
    summarize_catalog,
)
from src.models import Vehicle


def _veh(autosell_id: str, title: str) -> Vehicle:
    return Vehicle(
        autosell_id=autosell_id,
        slug=autosell_id,
        title=title,
        brand="Mazda",
        year="2021",
        price="100000",
        mileage="10000 km",
        version="",
        url=f"https://www.autosell.mx/{autosell_id}",
        image_urls=[],
    )


def test_branch_tags_counted_per_branch():
    vehicles = [
        _veh("obj1", "Cx 30 IGT *"),
        _veh("obj2", "CX 9 IGT +"),
        _veh("obj3", "Escalade -"),
        _veh("obj4", "Macan S"),
    ]

    summary = summarize_catalog(vehicles)

    assert summary["count"] == 4
    assert summary["tags"] == {"*": 1, "+": 1, "-": 1}
    assert summary["untagged"] == 1
    assert "periferico=1" in format_catalog_summary(vehicles)


def test_branch_tag_survives_snapshot_roundtrip(tmp_path):
    vehicles = [_veh("obj1", "Cx 30 IGT *")]
    path = tmp_path / "catalog.json"
    save_catalog_snapshot(vehicles, path)

    restored = load_catalog_snapshot(path)

    assert restored[0].marketplace_title == "2021 Mazda Cx 30 IGT *"
    assert branch_tag(restored[0]) == "*"
