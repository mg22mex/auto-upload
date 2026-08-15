"""Unit tests — Marketplace response listener must not crash on binary bodies."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from src.facebook.network import MarketplaceItemCapture, _looks_binary_content_type


class TestLooksBinaryContentType(unittest.TestCase):
    def test_images_and_octet_stream(self):
        self.assertTrue(_looks_binary_content_type("image/jpeg"))
        self.assertTrue(_looks_binary_content_type("application/octet-stream"))
        self.assertFalse(_looks_binary_content_type("application/json"))
        self.assertFalse(_looks_binary_content_type("text/html; charset=utf-8"))


class TestOnResponseSafe(unittest.TestCase):
    def test_utf8_decode_error_is_swallowed(self):
        cap = MarketplaceItemCapture()
        response = MagicMock()
        response.status = 200
        response.url = "https://www.facebook.com/ajax/foo"
        response.headers = {"content-type": "application/json"}
        response.text.side_effect = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")
        cap._on_response(response)
        self.assertEqual(cap.item_ids, [])

    def test_binary_content_type_skips_text(self):
        cap = MarketplaceItemCapture()
        response = MagicMock()
        response.status = 200
        response.url = "https://www.facebook.com/photo.jpg"
        response.headers = {"content-type": "image/jpeg"}
        cap._on_response(response)
        response.text.assert_not_called()
        self.assertEqual(cap.item_ids, [])

    def test_json_body_captures_item_id(self):
        cap = MarketplaceItemCapture()
        response = MagicMock()
        response.status = 200
        response.url = "https://www.facebook.com/api/graphql"
        response.headers = {"content-type": "application/json"}
        response.text.return_value = '{"listing_id": "12345678901"}'
        cap._on_response(response)
        self.assertEqual(cap.item_ids, ["12345678901"])


if __name__ == "__main__":
    unittest.main()
