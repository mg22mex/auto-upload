"""Unit tests — create refuses skip_shelf_gate without delete_verified."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.facebook.errors import FacebookPostingError
from src.facebook.poster import create_vehicle_listing
from src.models import Vehicle


def _vehicle() -> Vehicle:
    return Vehicle(
        autosell_id="obj1",
        slug="obj1",
        title="Car obj1",
        brand="Audi",
        year="2020",
        price="100000",
        mileage="10000 km",
        version="",
        url="https://www.autosell.mx/obj1",
        image_urls=["https://example.com/a.jpg"],
    )


class TestPosterDeleteVerifiedGate(unittest.TestCase):
    def test_skip_shelf_gate_without_delete_verified_raises(self):
        page = MagicMock()
        with self.assertRaises(FacebookPostingError) as cm:
            create_vehicle_listing(
                page,
                _vehicle(),
                fb_config={},
                max_photos=5,
                log_dir=Path("/tmp"),
                photo_paths=[Path("/tmp/a.jpg")],
                skip_shelf_gate=True,
                delete_verified=False,
            )
        self.assertIn("delete_verified", str(cm.exception))
        page.goto.assert_not_called()

    def test_skip_shelf_gate_with_delete_verified_proceeds_past_gate(self):
        page = MagicMock()
        page.url = "https://www.facebook.com/marketplace/create/vehicle"
        page_shows = MagicMock(return_value=False)

        with (
            patch("src.facebook.poster._wait_for_photo_file_input"),
            patch("src.facebook.poster.accept_vehicle_category_suggestion"),
            patch("src.facebook.poster.dismiss_overlays"),
            patch("src.facebook.poster.log_page_state"),
            patch("src.facebook.poster.page_shows_login_form", page_shows),
            patch("src.facebook.poster._ensure_vehicle_create_flow"),
            patch("src.facebook.poster.CREATE_VEHICLE_PATH_RE") as path_re,
            patch(
                "src.facebook.poster._upload_photos",
                side_effect=FacebookPostingError("stop-after-gate"),
            ),
        ):
            path_re.search.return_value = True
            with self.assertRaises(FacebookPostingError) as cm:
                create_vehicle_listing(
                    page,
                    _vehicle(),
                    fb_config={},
                    max_photos=5,
                    log_dir=Path("/tmp"),
                    photo_paths=[Path("/tmp/a.jpg")],
                    skip_shelf_gate=True,
                    delete_verified=True,
                )
        self.assertIn("stop-after-gate", str(cm.exception))
        page.goto.assert_called()


if __name__ == "__main__":
    unittest.main()
