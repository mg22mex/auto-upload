from __future__ import annotations

import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests

from src.models import Vehicle


def download_vehicle_photos(
    vehicle: Vehicle,
    *,
    max_photos: int,
    timeout_sec: int = 60,
) -> list[Path]:
    """Download up to max_photos images for a vehicle into a temp directory."""
    if max_photos <= 0:
        return []

    tmp_dir = Path(tempfile.mkdtemp(prefix=f"autosell_{vehicle.autosell_id}_"))
    saved: list[Path] = []
    seen_names: set[str] = set()

    for index, url in enumerate(vehicle.image_urls):
        if len(saved) >= max_photos:
            break
        if not url:
            continue

        parsed = urlparse(url)
        suffix = Path(parsed.path).suffix or ".jpg"
        stem = Path(parsed.path).stem or f"photo_{index}"
        if stem in seen_names:
            stem = f"{stem}_{index}"
        seen_names.add(stem)
        dest = tmp_dir / f"{stem}{suffix}"

        response = requests.get(url, timeout=timeout_sec)
        response.raise_for_status()
        dest.write_bytes(response.content)
        saved.append(dest)

    if not saved:
        raise RuntimeError(f"No photos downloaded for {vehicle.autosell_id}")

    return saved
