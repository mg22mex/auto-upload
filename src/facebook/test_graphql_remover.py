from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.facebook.graphql.errors import FacebookGraphqlFallback
from src.facebook.graphql.remover import (
    delete_listing_graphql,
    extract_item_id,
    remove_vehicle_listing_graphql,
)
from src.facebook.graphql.session_ctx import FbSessionContext


class RemoverGraphqlTests(unittest.TestCase):
    def test_extract_item_id(self):
        self.assertEqual(
            extract_item_id("https://www.facebook.com/marketplace/item/1234567890/"),
            "1234567890",
        )

    def test_mark_sold_coerced_to_hard_delete(self):
        """Sync sold/removed must GraphQL-delete, not fall back for mark_sold."""
        context = MagicMock()
        page = MagicMock()
        session = FbSessionContext(c_user="1", xs="x", fb_dtsg="d", cookies={})
        with (
            patch(
                "src.facebook.graphql.remover.extract_session_context",
                return_value=session,
            ),
            patch(
                "src.facebook.graphql.remover.delete_listing_graphql",
                return_value=True,
            ) as delete,
        ):
            ok = remove_vehicle_listing_graphql(
                context,
                "https://www.facebook.com/marketplace/item/123456789012345/",
                autosell_id="obj99",
                removal_action="mark_sold",
                page=page,
            )
        self.assertTrue(ok)
        delete.assert_called_once()
        self.assertEqual(delete.call_args.kwargs.get("autosell_id"), "obj99")

    def test_delete_posts_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gql_dir = root / "data" / "graphql"
            gql_dir.mkdir(parents=True)
            (gql_dir / "delete_listing.json").write_text(
                json.dumps(
                    {
                        "doc_id": "555",
                        "friendly_name": "Del",
                        "variables": {
                            "input": {
                                "listing_id": "{{item_id}}",
                                "actor_id": "{{c_user}}",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            session = FbSessionContext(
                c_user="42",
                xs="xs",
                fb_dtsg="dtsg",
                cookies={"c_user": "42", "xs": "xs"},
            )
            client = MagicMock()
            client.post.return_value = {"data": {"ok": True}}

            with patch(
                "src.facebook.graphql.remover.listing_unavailable_http",
                return_value=True,
            ):
                ok = delete_listing_graphql(
                    session,
                    "https://www.facebook.com/marketplace/item/999888777666/",
                    autosell_id="obj1",
                    root=root,
                    client=client,
                    verify=True,
                )
            self.assertTrue(ok)
            args, kwargs = client.post.call_args
            self.assertEqual(args[0], "555")
            self.assertEqual(args[1]["input"]["listing_id"], "999888777666")
            self.assertEqual(args[1]["input"]["actor_id"], "42")

    def test_missing_template_raises_fallback(self):
        session = FbSessionContext(c_user="1", xs="x", fb_dtsg="d", cookies={})
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FacebookGraphqlFallback):
                delete_listing_graphql(
                    session,
                    "https://www.facebook.com/marketplace/item/1234567890/",
                    root=Path(tmp),
                )


if __name__ == "__main__":
    unittest.main()
