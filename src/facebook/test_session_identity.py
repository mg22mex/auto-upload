"""Unit tests — Facebook profile (c_user) session gate."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from src.facebook.errors import FacebookSessionError
from src.facebook.session import assert_session_identity, read_session_c_user


class TestSessionIdentity(unittest.TestCase):
    def test_read_c_user_from_cookies(self):
        context = MagicMock()
        context.cookies.return_value = [
            {"name": "xs", "value": "secret"},
            {"name": "c_user", "value": "61568552875980"},
        ]
        self.assertEqual(read_session_c_user(context), "61568552875980")

    def test_assert_matches_config(self):
        context = MagicMock()
        context.cookies.return_value = [{"name": "c_user", "value": "100047574648142"}]
        config = {
            "accounts": [
                {
                    "id": "account_2",
                    "facebook_c_user": "100047574648142",
                    "facebook_profile": "Marco",
                }
            ]
        }
        self.assertEqual(
            assert_session_identity(context, config, "account_2"),
            "100047574648142",
        )

    def test_assert_mismatch_raises(self):
        context = MagicMock()
        context.cookies.return_value = [{"name": "c_user", "value": "111"}]
        config = {
            "accounts": [
                {"id": "account_1", "facebook_c_user": "61568552875980"},
            ]
        }
        with self.assertRaises(FacebookSessionError) as cm:
            assert_session_identity(context, config, "account_1")
        self.assertIn("wrong Facebook profile", str(cm.exception))
        self.assertIn("111", str(cm.exception))

    def test_assert_missing_cookie_raises(self):
        context = MagicMock()
        context.cookies.return_value = []
        config = {
            "accounts": [
                {"id": "account_1", "facebook_c_user": "61568552875980"},
            ]
        }
        with self.assertRaises(FacebookSessionError) as cm:
            assert_session_identity(context, config, "account_1")
        self.assertIn("no c_user", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
