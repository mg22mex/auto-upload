from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.facebook.graphql.errors import FacebookGraphqlError, is_phantom_delete_error
from src.facebook.graphql.purge import purge_old_listings_graphql
from src.facebook.graphql.selling import (
    parse_active_listings_from_text,
    parse_active_listings_payload,
)
from src.facebook.graphql.session_ctx import FbSessionContext
from src.facebook.graphql.templates import prepare_mutation_variables, substitute_known_ids


class SellingParseTests(unittest.TestCase):
    def test_parse_item_urls_and_for_sale_ids(self):
        blob = (
            'href="/marketplace/item/1111222233334444/" '
            '"for_sale_item_id":"5555666677778888" '
            '"marketplace_listing_title":"Ford Mustang GT" '
            '"creation_time":1700000000'
        )
        items = parse_active_listings_from_text(blob)
        ids = {i.item_id for i in items}
        self.assertIn("1111222233334444", ids)
        self.assertIn("5555666677778888", ids)

    def test_parse_payload_nodes(self):
        payload = {
            "data": {
                "viewer": {
                    "edges": [
                        {
                            "node": {
                                "__typename": "GroupCommerceProductItem",
                                "id": "9999888877776666",
                                "marketplace_listing_title": "Mazda CX-5",
                                "creation_time": 1600000000,
                            }
                        }
                    ]
                }
            }
        }
        items = parse_active_listings_payload(payload)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].item_id, "9999888877776666")
        self.assertIn("Mazda", items[0].title)
        self.assertIsNotNone(items[0].age_days)


class DeleteVariableTests(unittest.TestCase):
    def test_substitute_for_sale_item_id(self):
        captured = {
            "input": {
                "actor_id": "100047574648142",
                "for_sale_item_id": "2577518139402437",
                "client_mutation_id": "1",
                "batch_delete_variants": True,
                "surface": "MARKETPLACE_PAGE_SELLING",
            }
        }
        out = substitute_known_ids(
            captured,
            {
                "c_user": "61568552875980",
                "item_id": "1111222233334444",
                "client_mutation_id": "abc",
            },
        )
        self.assertEqual(out["input"]["actor_id"], "61568552875980")
        self.assertEqual(out["input"]["for_sale_item_id"], "1111222233334444")
        self.assertEqual(out["input"]["client_mutation_id"], "abc")

    def test_prepare_with_placeholders(self):
        template = {
            "variables": {
                "input": {
                    "actor_id": "{{c_user}}",
                    "for_sale_item_id": "{{item_id}}",
                    "client_mutation_id": "{{client_mutation_id}}",
                }
            }
        }
        variables = prepare_mutation_variables(
            template,
            {
                "c_user": "1",
                "item_id": "99",
                "client_mutation_id": "cm",
                "actor_id": "1",
                "listing_id": "99",
                "for_sale_item_id": "99",
            },
        )
        self.assertEqual(variables["input"]["for_sale_item_id"], "99")


class GraphqlPurgeTests(unittest.TestCase):
    def test_purge_deletes_old_via_graphql(self):
        session = FbSessionContext(
            c_user="42", xs="xs", fb_dtsg="dtsg", cookies={"c_user": "42", "xs": "xs"}
        )
        store = MagicMock()
        store.get_live_listings.return_value = []

        old = MagicMock()
        old.item_id = "1111222233334444"
        old.title = "Old Car"
        old.age_days = 5.0
        old.age_text = "5d"
        old.href = "https://www.facebook.com/marketplace/item/1111222233334444/"
        old.source = "test"
        old.card_text_like = lambda: "Old Car"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gql_dir = root / "data" / "graphql"
            gql_dir.mkdir(parents=True)
            (gql_dir / "delete_listing.json").write_text(
                json.dumps(
                    {
                        "doc_id": "30073389588942699",
                        "friendly_name": "useCometMarketplaceForSaleItemDeleteMutation",
                        "variables": {
                            "input": {
                                "actor_id": "{{c_user}}",
                                "for_sale_item_id": "{{item_id}}",
                                "client_mutation_id": "{{client_mutation_id}}",
                                "batch_delete_variants": True,
                                "surface": "MARKETPLACE_PAGE_SELLING",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            client = MagicMock()
            client.post.return_value = {"data": {"ok": True}}

            with (
                patch(
                    "src.facebook.graphql.purge.fetch_active_selling_listings",
                    return_value=[old],
                ),
                patch(
                    "src.facebook.graphql.purge.delete_listing_graphql",
                    return_value=True,
                ) as delete,
                patch("src.facebook.graphql.purge.random_delay"),
                patch(
                    "src.facebook.graphql.purge._catalog_by_autosell_id",
                    return_value={},
                ),
            ):
                stats = purge_old_listings_graphql(
                    session,
                    store,
                    account_id="account_1",
                    max_days=0.0,
                    dry_run=False,
                    root=root,
                    client=client,
                )

        self.assertEqual(stats.found, 1)
        self.assertEqual(stats.deleted, 1)
        delete.assert_called_once()

    def test_max_days_zero_purges_unknown_age(self):
        session = FbSessionContext(
            c_user="42", xs="xs", fb_dtsg="dtsg", cookies={"c_user": "42", "xs": "xs"}
        )
        store = MagicMock()
        store.get_live_listings.return_value = []

        unknown = MagicMock()
        unknown.item_id = "2222333344445555"
        unknown.title = "No Timestamp Car"
        unknown.age_days = None
        unknown.age_text = ""
        unknown.href = "https://www.facebook.com/marketplace/item/2222333344445555/"
        unknown.source = "test"
        unknown.card_text_like = lambda: "No Timestamp Car"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data" / "graphql").mkdir(parents=True)
            with (
                patch(
                    "src.facebook.graphql.purge.fetch_active_selling_listings",
                    return_value=[unknown],
                ),
                patch(
                    "src.facebook.graphql.purge.delete_listing_graphql",
                    return_value=True,
                ) as delete,
                patch("src.facebook.graphql.purge.random_delay"),
                patch(
                    "src.facebook.graphql.purge._catalog_by_autosell_id",
                    return_value={},
                ),
            ):
                stats = purge_old_listings_graphql(
                    session,
                    store,
                    account_id="account_1",
                    max_days=0.0,
                    dry_run=False,
                    root=root,
                    client=MagicMock(),
                )

        self.assertEqual(stats.skipped_unknown_age, 0)
        self.assertEqual(stats.deleted, 1)
        delete.assert_called_once()

    def test_max_days_positive_still_skips_unknown_age(self):
        session = FbSessionContext(
            c_user="42", xs="xs", fb_dtsg="dtsg", cookies={"c_user": "42", "xs": "xs"}
        )
        store = MagicMock()
        store.get_live_listings.return_value = []
        unknown = MagicMock()
        unknown.item_id = "3333444455556666"
        unknown.title = "Skip Me"
        unknown.age_days = None
        unknown.age_text = ""
        unknown.href = "https://www.facebook.com/marketplace/item/3333444455556666/"
        unknown.source = "test"
        unknown.card_text_like = lambda: "Skip Me"

        with (
            patch(
                "src.facebook.graphql.purge.fetch_active_selling_listings",
                return_value=[unknown],
            ),
            patch(
                "src.facebook.graphql.purge.delete_listing_graphql",
                return_value=True,
            ) as delete,
            patch(
                "src.facebook.graphql.purge._catalog_by_autosell_id",
                return_value={},
            ),
        ):
            stats = purge_old_listings_graphql(
                session,
                store,
                account_id="account_1",
                max_days=3.0,
                dry_run=False,
                root=Path("."),
                client=MagicMock(),
            )

        self.assertEqual(stats.skipped_unknown_age, 1)
        self.assertEqual(stats.deleted, 0)
        delete.assert_not_called()

    def test_field_exception_unknown_age_is_skipped_not_fail(self):
        self.assertTrue(
            is_phantom_delete_error(
                FacebookGraphqlError(
                    "GraphQL errors",
                    payload={
                        "errors": [
                            {
                                "message": "A server error field_exception occurred",
                                "severity": "field_exception",
                            }
                        ]
                    },
                )
            )
        )
        session = FbSessionContext(
            c_user="42", xs="xs", fb_dtsg="dtsg", cookies={"c_user": "42", "xs": "xs"}
        )
        store = MagicMock()
        store.get_live_listings.return_value = []
        ghost = MagicMock()
        ghost.item_id = "4444555566667777"
        ghost.title = "Phantom"
        ghost.age_days = None
        ghost.age_text = "unknown age (total purge)"
        ghost.href = "https://www.facebook.com/marketplace/item/4444555566667777/"
        ghost.source = "test"
        ghost.card_text_like = lambda: "Phantom"

        with (
            patch(
                "src.facebook.graphql.purge.fetch_active_selling_listings",
                return_value=[ghost],
            ),
            patch(
                "src.facebook.graphql.purge.delete_listing_graphql",
                side_effect=FacebookGraphqlError(
                    "GraphQL useCometMarketplaceForSaleItemDeleteMutation errors: "
                    "field_exception",
                    payload={"errors": [{"message": "field_exception"}]},
                ),
            ),
            patch(
                "src.facebook.graphql.purge._catalog_by_autosell_id",
                return_value={},
            ),
        ):
            stats = purge_old_listings_graphql(
                session,
                store,
                account_id="account_1",
                max_days=0.0,
                dry_run=False,
                root=Path("."),
                client=MagicMock(),
            )

        self.assertEqual(stats.failed, 0)
        self.assertEqual(stats.skipped_phantom, 1)
        self.assertEqual(stats.deleted, 0)


if __name__ == "__main__":
    unittest.main()
