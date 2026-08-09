"""Unit tests — resilient Marketplace listing removal."""
from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.facebook.errors import FacebookPostingError
from src.facebook.remover import (
    extract_item_id,
    remove_vehicle_listing,
    _listing_already_gone,
)
from src.facebook.session import format_session_login_error, session_health_report


class TestListingAlreadyGone(unittest.TestCase):
    def test_body_unavailable_es(self):
        page = MagicMock()
        page.url = "https://www.facebook.com/marketplace/item/1/"
        page.get_by_role.return_value.count.return_value = 0
        page.locator.return_value.inner_text.return_value = (
            "Este contenido no está disponible en este momento"
        )
        self.assertTrue(
            _listing_already_gone(page, listing_url=page.url, item_id="1")
        )

    def test_body_unavailable_en(self):
        page = MagicMock()
        page.url = "https://www.facebook.com/marketplace/item/1/"
        page.get_by_role.return_value.count.return_value = 0
        page.locator.return_value.inner_text.return_value = (
            "Sorry, this content isn't available right now"
        )
        self.assertTrue(
            _listing_already_gone(page, listing_url=page.url, item_id="1")
        )

    def test_redirected_off_item_without_live_controls(self):
        page = MagicMock()
        page.url = "https://www.facebook.com/marketplace/"
        # Mark-as-available not present, no live controls
        role_loc = MagicMock()
        role_loc.count.return_value = 0
        page.get_by_role.return_value = role_loc
        page.locator.return_value.inner_text.return_value = "Marketplace"
        page.locator.return_value.count.return_value = 0
        self.assertTrue(
            _listing_already_gone(
                page,
                listing_url="https://www.facebook.com/marketplace/item/99/",
                item_id="99",
            )
        )

    def test_live_listing_not_gone(self):
        page = MagicMock()
        page.url = "https://www.facebook.com/marketplace/item/99/"
        # No mark-as-available (first call)
        # body without gone phrases
        role_loc = MagicMock()
        role_loc.count.return_value = 0
        page.get_by_role.return_value = role_loc
        page.locator.return_value.inner_text.return_value = (
            "2021 GMC Sierra Denali $850,000 Mark as sold"
        )
        page.locator.return_value.count.return_value = 0
        # _has_live_controls may be false with our mock; body has mark as sold text
        # Phrase list does not include mark sold → not gone if short body path not hit
        gone = _listing_already_gone(
            page,
            listing_url=page.url,
            item_id="99",
        )
        # Body length > 400 or no 404 tokens → still False is ideal
        self.assertFalse(gone)


class TestRemoveVehicleListingFallbacks(unittest.TestCase):
    def test_already_gone_skips_delete_and_returns_true(self):
        page = MagicMock()
        page.url = "https://www.facebook.com/marketplace/item/1/"
        log_dir = Path(tempfile.mkdtemp())

        with (
            patch(
                "src.facebook.remover._listing_already_gone",
                return_value=True,
            ),
            patch(
                "src.facebook.remover._verify_listing_removed",
                return_value=True,
            ) as ver,
            patch("src.facebook.remover._delete_listing") as delete,
            patch("src.facebook.remover._remove_from_selling_shelf") as shelf,
        ):
            ok = remove_vehicle_listing(
                page,
                "https://www.facebook.com/marketplace/item/123/",
                autosell_id="obj1",
                removal_action="delete",
                log_dir=log_dir,
                require_verified=True,
            )
        self.assertTrue(ok)
        delete.assert_not_called()
        shelf.assert_not_called()
        ver.assert_called()

    def test_selling_shelf_fallback_when_detail_delete_fails(self):
        page = MagicMock()
        page.url = "https://www.facebook.com/marketplace/item/1773123456/"
        log_dir = Path(tempfile.mkdtemp())
        state = {"gone_after_shelf": False}

        def already_gone(*_a, **_k):
            return state["gone_after_shelf"]

        def shelf_ok(*_a, **_k):
            state["gone_after_shelf"] = True
            return True

        with (
            patch(
                "src.facebook.remover._listing_already_gone",
                side_effect=already_gone,
            ),
            patch(
                "src.facebook.remover._perform_removal_on_current_page",
                side_effect=FacebookPostingError("Delete control not found"),
            ),
            patch(
                "src.facebook.remover._remove_from_selling_shelf",
                side_effect=shelf_ok,
            ) as shelf,
            patch(
                "src.facebook.remover._verify_listing_removed",
                return_value=True,
            ),
        ):
            ok = remove_vehicle_listing(
                page,
                "https://www.facebook.com/marketplace/item/1773123456/",
                autosell_id="obj954",
                removal_action="delete",
                log_dir=log_dir,
                require_verified=True,
            )
        self.assertTrue(ok)
        shelf.assert_called_once()
        self.assertEqual(shelf.call_args.args[1], "1773123456")
        self.assertEqual(shelf.call_args.kwargs.get("action"), "delete")

    def test_raises_when_all_strategies_fail(self):
        page = MagicMock()
        page.url = "https://www.facebook.com/marketplace/item/1/"
        log_dir = Path(tempfile.mkdtemp())

        with (
            patch("src.facebook.remover._listing_already_gone", return_value=False),
            patch(
                "src.facebook.remover._perform_removal_on_current_page",
                side_effect=FacebookPostingError("Delete control not found"),
            ),
            patch(
                "src.facebook.remover._remove_from_selling_shelf",
                return_value=False,
            ),
            patch("src.facebook.remover._verify_listing_removed", return_value=False),
            patch("src.facebook.remover._save_debug"),
        ):
            with self.assertRaises(FacebookPostingError) as ctx:
                remove_vehicle_listing(
                    page,
                    "https://www.facebook.com/marketplace/item/123/",
                    autosell_id="obj1102",
                    removal_action="delete",
                    log_dir=log_dir,
                    require_verified=True,
                )
        self.assertIn("Delete/mark-sold", str(ctx.exception))
        self.assertIn("obj1102", str(ctx.exception))


class TestOwnerVsVisitorChrome(unittest.TestCase):
    def test_owner_chrome_mark_sold(self):
        from src.facebook.remover import _is_owner_listing_view, _is_visitor_listing_view

        page = MagicMock()

        def get_by_role(role, name=None, **_k):
            loc = MagicMock()
            pat = getattr(name, "pattern", str(name or ""))
            if "marcar como vendido|mark as sold" in pat or (
                isinstance(name, re.Pattern) and name.search("Marcar como vendido")
            ):
                loc.count.return_value = 1
                loc.first.is_visible.return_value = True
            else:
                loc.count.return_value = 0
                loc.first.is_visible.return_value = False
            return loc

        page.get_by_role.side_effect = get_by_role
        self.assertTrue(_is_owner_listing_view(page))
        self.assertFalse(_is_visitor_listing_view(page))

    def test_visitor_chrome_message(self):
        from src.facebook.remover import _is_owner_listing_view, _is_visitor_listing_view

        page = MagicMock()

        def get_by_role(role, name=None, **_k):
            loc = MagicMock()
            if isinstance(name, re.Pattern) and name.search("Enviar mensaje"):
                loc.count.return_value = 1
                loc.first.is_visible.return_value = True
            else:
                loc.count.return_value = 0
                loc.first.is_visible.return_value = False
            return loc

        page.get_by_role.side_effect = get_by_role
        page.locator.return_value.inner_text.return_value = "x"
        self.assertFalse(_is_owner_listing_view(page))
        self.assertTrue(_is_visitor_listing_view(page))


class TestSessionHealth(unittest.TestCase):
    def test_missing_dir_looks_empty(self):
        path = Path(tempfile.mkdtemp()) / "nope"
        health = session_health_report(path)
        self.assertFalse(health["exists"])
        self.assertTrue(health["looks_empty"])
        msg = format_session_login_error("account_1", path)
        self.assertIn("account_1", msg)
        self.assertIn("fb_login.py", msg)
        self.assertIn("missing", msg.lower())

    def test_empty_dir_looks_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            health = session_health_report(Path(tmp))
            self.assertTrue(health["exists"])
            self.assertTrue(health["looks_empty"])

    def test_cookies_file_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cookies = root / "Default" / "Cookies"
            cookies.parent.mkdir(parents=True)
            cookies.write_bytes(b"x" * 100)
            (root / "Default" / "Preferences").write_text("{}", encoding="utf-8")
            (root / "Local State").write_text("{}", encoding="utf-8")
            health = session_health_report(root)
            self.assertTrue(health["has_cookies_file"])
            self.assertFalse(health["looks_empty"])


class TestExtractItemId(unittest.TestCase):
    def test_id(self):
        self.assertEqual(
            extract_item_id(
                "https://www.facebook.com/marketplace/item/236275427101637/"
            ),
            "236275427101637",
        )


if __name__ == "__main__":
    unittest.main()
