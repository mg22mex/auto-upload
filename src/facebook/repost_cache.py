"""Prefetch / freeze listing payload for fast delete→recreate.

Downloads photos and snapshots title/description/price/images *before*
removing the live Marketplace post so create can fill the form immediately
after delete (no download dead time, shorter visibility gap).
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from src.facebook.photos import download_vehicle_photos, vehicle_image_urls
from src.facebook.util import log_step, parse_mxn_price, vehicle_description
from src.models import Vehicle


@dataclass
class ListingRepostCache:
    """Frozen vehicle fields + local photo files for one relist."""

    autosell_id: str
    title: str
    description: str
    price: str
    price_digits: str
    image_urls: list[str] = field(default_factory=list)
    photo_paths: list[Path] = field(default_factory=list)
    vehicle: Vehicle | None = None

    @property
    def photo_count(self) -> int:
        return len(self.photo_paths)

    def cleanup(self) -> None:
        """Remove temp photo dir created by ``prefetch_photos``."""
        if not self.photo_paths:
            return
        parent = self.photo_paths[0].parent
        for path in self.photo_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        self.photo_paths = []
        try:
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
            elif parent.is_dir() and parent.name.startswith("autosell_"):
                shutil.rmtree(parent, ignore_errors=True)
        except OSError:
            pass


def build_listing_cache(vehicle: Vehicle, *, branch: str | None = None) -> ListingRepostCache:
    """Snapshot title / description / price / image URLs from catalog vehicle."""
    urls = vehicle_image_urls(vehicle) or list(vehicle.image_urls or [])
    try:
        price_digits = parse_mxn_price(vehicle.price)
    except ValueError:
        price_digits = ""
    return ListingRepostCache(
        autosell_id=vehicle.autosell_id,
        title=vehicle.marketplace_title,
        description=vehicle_description(vehicle, branch=branch),
        price=vehicle.price,
        price_digits=price_digits,
        image_urls=list(urls),
        vehicle=vehicle,
    )


def prefetch_photos(
    cache: ListingRepostCache,
    *,
    max_photos: int,
) -> ListingRepostCache:
    """Download images into a temp dir; attach paths on ``cache``."""
    if cache.vehicle is None:
        raise RuntimeError(f"ListingRepostCache {cache.autosell_id}: missing vehicle")
    log_step(
        f"CACHE_PREFETCH {cache.autosell_id} "
        f"title={cache.title!r} price={cache.price} images={len(cache.image_urls)}"
    )
    paths = download_vehicle_photos(cache.vehicle, max_photos=max_photos)
    cache.photo_paths = list(paths)
    log_step(
        f"CACHE_READY {cache.autosell_id} photos={cache.photo_count} "
        f"dir={paths[0].parent if paths else 'none'}"
    )
    return cache
