"""Unit tests for listing repost cache / photo prefetch."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.facebook.repost_cache import ListingRepostCache, build_listing_cache, prefetch_photos
from src.models import Vehicle


def _vehicle() -> Vehicle:
    return Vehicle(
        autosell_id="obj1126",
        slug="obj1126",
        title="Lobo Platinum",
        brand="Ford",
        year="2021",
        price="$850,000",
        mileage="45000 km",
        version="Platinum",
        url="https://www.autosell.mx/obj1126",
        image_urls=[
            "https://cdn.example.com/obj1126/a.jpg",
            "https://cdn.example.com/obj1126/b.jpg",
        ],
    )


class TestListingRepostCache(unittest.TestCase):
    def test_build_snapshots_title_price_description_images(self):
        cache = build_listing_cache(_vehicle())
        self.assertEqual(cache.autosell_id, "obj1126")
        self.assertIn("Ford", cache.title)
        self.assertIn("Lobo", cache.title)
        self.assertEqual(cache.price, "$850,000")
        self.assertEqual(cache.price_digits, "850000")
        self.assertEqual(len(cache.image_urls), 2)
        self.assertTrue(cache.description)
        self.assertEqual(cache.photo_count, 0)

    def test_prefetch_attaches_paths_and_cleanup(self):
        cache = build_listing_cache(_vehicle())
        tmp = Path(tempfile.mkdtemp(prefix="autosell_obj1126_"))
        fake = [tmp / "a.jpg", tmp / "b.jpg"]
        for p in fake:
            p.write_bytes(b"x")

        with patch(
            "src.facebook.repost_cache.download_vehicle_photos",
            return_value=fake,
        ):
            prefetch_photos(cache, max_photos=20)

        self.assertEqual(cache.photo_count, 2)
        self.assertTrue(all(p.exists() for p in cache.photo_paths))
        cache.cleanup()
        self.assertEqual(cache.photo_count, 0)
        self.assertFalse(tmp.exists())


if __name__ == "__main__":
    unittest.main()
