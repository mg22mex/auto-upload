"""Sync remove path hard-deletes via GraphQL when FB_USE_GRAPHQL=1."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.facebook.remover import remove_vehicle_listing


class SyncHardDeleteTests(unittest.TestCase):
    def test_mark_sold_coerced_when_graphql_enabled(self):
        page = MagicMock()
        page.context = MagicMock()
        with (
            patch("src.facebook.graphql.mode.graphql_enabled", return_value=True),
            patch("src.facebook.graphql.mode.graphql_ui_fallback", return_value=False),
            patch(
                "src.facebook.graphql.remover.remove_vehicle_listing_graphql",
                return_value=True,
            ) as gql_remove,
        ):
            ok = remove_vehicle_listing(
                page,
                "https://www.facebook.com/marketplace/item/123456789012345/",
                autosell_id="obj1",
                removal_action="mark_sold",
                log_dir=Path("/tmp"),
            )
        self.assertTrue(ok)
        gql_remove.assert_called_once()
        self.assertEqual(gql_remove.call_args.kwargs["removal_action"], "delete")

    def test_executor_coerces_removal_action_under_graphql(self):
        from contextlib import contextmanager

        from src.facebook.executor import execute_actions
        from src.models import SyncAction
        from src.store.db import SyncStore
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            store = SyncStore(Path(tmp) / "sync.db")
            store.upsert_fb_listing(
                "obj1",
                "account_1",
                fb_listing_url="https://www.facebook.com/marketplace/item/123456789012345/",
                content_hash="abc",
                status="live",
            )
            action = SyncAction(
                action="remove",
                autosell_id="obj1",
                slug="obj1",
                account_id="account_1",
                reason="gone from catalog",
            )
            config = {
                "facebook": {"headless": True},
                "sync": {"removal_action": "mark_sold"},
            }
            seen: dict[str, str] = {}

            def capture_remove(*_a, **kwargs):
                seen["removal_action"] = kwargs.get("removal_action", "")
                return True

            @contextmanager
            def fake_ctx(*_a, **_k):
                yield MagicMock()

            with (
                patch.dict("os.environ", {"FB_USE_GRAPHQL": "1"}, clear=False),
                patch("src.facebook.executor.open_account_context", fake_ctx),
                patch("src.facebook.executor.get_page", return_value=MagicMock()),
                patch("src.facebook.executor.is_logged_in", return_value=True),
                patch("src.facebook.executor.page_shows_login_form", return_value=False),
                patch("src.facebook.executor.random_delay"),
                patch("src.facebook.executor.ensure_log_dir", return_value=Path(tmp)),
                patch(
                    "src.facebook.executor.remove_vehicle_listing",
                    side_effect=capture_remove,
                ),
            ):
                result = execute_actions(
                    [action],
                    store,
                    config,
                    root=Path(tmp),
                    account_order=["account_1"],
                )
            self.assertEqual(result.removals, 1)
            self.assertEqual(seen["removal_action"], "delete")
            store.close()


if __name__ == "__main__":
    unittest.main()
