from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from src.facebook.errors import FacebookSessionError
from src.facebook.graphql.session_ctx import (
    extract_session_context_from_cookies,
    jazoest_from_dtsg,
)


class SessionExtractTests(unittest.TestCase):
    def test_missing_cookies(self):
        with self.assertRaises(FacebookSessionError):
            extract_session_context_from_cookies({}, fetch_tokens=False)

    def test_from_cookies_with_html_tokens(self):
        html = (
            '["DTSGInitData",[],{"token":"tok:1"}]'
            '["LSD",[],{"token":"lsd1"}]'
            "jazoest=22123"
            '"client_revision":42'
        )
        session = extract_session_context_from_cookies(
            {"c_user": "615", "xs": "abc%3Adef"},
            html=html,
            fetch_tokens=False,
            account_id="account_1",
        )
        self.assertEqual(session.c_user, "615")
        self.assertEqual(session.xs, "abc:def")
        self.assertEqual(session.fb_dtsg, "tok:1")
        self.assertEqual(session.lsd, "lsd1")
        self.assertIn("c_user=615", session.cookie_header)
        self.assertIn("xs=", session.cookie_header)

    def test_jazoest_checksum(self):
        self.assertTrue(jazoest_from_dtsg("ab").startswith("2"))

    def test_http_token_refresh(self):
        html = '["DTSGInitData",[],{"token":"http_tok"}]'
        response = MagicMock()
        response.status_code = 200
        response.text = html
        response.url = "https://www.facebook.com/marketplace/"

        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = None
        mock_client.get.return_value = response

        with patch("src.facebook.graphql.session_ctx.httpx.Client", return_value=mock_client):
            session = extract_session_context_from_cookies(
                {"c_user": "1", "xs": "x"},
                fetch_tokens=True,
            )
        self.assertEqual(session.fb_dtsg, "http_tok")


if __name__ == "__main__":
    unittest.main()
