from __future__ import annotations

import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests

from src.models import Vehicle

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
}


def vehicle_image_urls(vehicle: Vehicle) -> list[str]:
    """Keep only images that belong to this vehicle's autosell_id."""
    needle = f"/{vehicle.autosell_id}/"
    return [url for url in vehicle.image_urls if needle in url]


def download_vehicle_photos(
    vehicle: Vehicle,
    *,
    max_photos: int,
    timeout_sec: int = 60,
) -> list[Path]:
    """Download up to max_photos images for a vehicle into a temp directory."""
    if max_photos <= 0:
        return []

    urls = vehicle_image_urls(vehicle)
    if not urls:
        raise RuntimeError(f"No image URLs for {vehicle.autosell_id}")

    tmp_dir = Path(tempfile.mkdtemp(prefix=f"autosell_{vehicle.autosell_id}_"))
    saved: list[Path] = []
    seen_names: set[str] = set()

    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)

    for index, url in enumerate(urls):
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

        response = session.get(
            url,
            timeout=timeout_sec,
            headers={"Referer": vehicle.url},
        )
        response.raise_for_status()
        dest.write_bytes(response.content)
        saved.append(dest)

    if not saved:
        raise RuntimeError(f"No photos downloaded for {vehicle.autosell_id}")

    return saved
