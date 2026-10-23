from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from src.facebook.errors import FacebookPostingError
from src.facebook.graphql.client import GraphQLClient, parse_graphql_response_text
from src.facebook.graphql.errors import (
    extract_listing_id_from_payload,
    is_duplicate_graphql_payload,
    raise_for_graphql_payload,
)
from src.facebook.graphql.session_ctx import FbSessionContext, parse_tokens_from_html
from src.facebook.graphql.templates import build_variables, render_template_value


class ParseResponseTests(unittest.TestCase):
    def test_strips_for_loop_prefix(self):
        payload = parse_graphql_response_text('for (;;);{"data":{"ok":true}}')
        self.assertEqual(payload, {"data": {"ok": True}})

    def test_duplicate_detection(self):
        self.assertTrue(
            is_duplicate_graphql_payload(
                {"errors": [{"message": "This looks like a duplicate listing"}]}
            )
        )
        with self.assertRaises(FacebookPostingError) as cm:
            raise_for_graphql_payload(
                {"errors": [{"message": "publicación duplicada"}]},
                action="create",
            )
        self.assertIn("SKIP_CREATE", str(cm.exception))

    def test_extract_listing_id(self):
        payload = {"data": {"x": {"listing_id": "123456789012345"}}}
        self.assertEqual(extract_listing_id_from_payload(payload), "123456789012345")


class TokenParseTests(unittest.TestCase):
    def test_parse_dtsg_from_html(self):
        html = '["DTSGInitData",[],{"token":"abc:123","async_get_token":"x"}]'
        tokens = parse_tokens_from_html(html)
        self.assertEqual(tokens["fb_dtsg"], "abc:123")


class TemplateTests(unittest.TestCase):
    def test_render_preserves_list_placeholder(self):
        rendered = render_template_value("{{photo_ids}}", {"photo_ids": ["1", "2"]})
        self.assertEqual(rendered, ["1", "2"])

    def test_build_variables(self):
        template = {
            "variables": {
                "input": {
                    "listing_id": "{{item_id}}",
                    "actor_id": "{{c_user}}",
                }
            }
        }
        variables = build_variables(
            template, {"item_id": "99", "c_user": "111", "client_mutation_id": "x"}
        )
        self.assertEqual(variables["input"]["listing_id"], "99")
        self.assertEqual(variables["input"]["actor_id"], "111")


class ClientPostTests(unittest.TestCase):
    def test_post_builds_form_and_parses(self):
        session = FbSessionContext(
            c_user="111",
            xs="xss",
            fb_dtsg="dtsg",
            cookies={"c_user": "111", "xs": "xss"},
            lsd="lsd",
            jazoest="2123",
            client_revision="10",
        )
        response = MagicMock()
        response.status_code = 200
        response.text = 'for (;;);{"data":{"ok":1}}'
        http = MagicMock()
        http.post.return_value = response

        with GraphQLClient(session, client=http) as gql:
            payload = gql.post("999", {"input": {"a": 1}}, friendly_name="TestOp")

        self.assertEqual(payload["data"]["ok"], 1)
        args, kwargs = http.post.call_args
        self.assertIn("facebook.com/api/graphql", args[0])
        form = kwargs["data"]
        self.assertEqual(form["doc_id"], "999")
        self.assertEqual(form["fb_dtsg"], "dtsg")
        self.assertEqual(json.loads(form["variables"]), {"input": {"a": 1}})
        self.assertEqual(form["server_timestamps"], "true")
        self.assertEqual(form["av"], "111")

    def test_post_merges_comet_scaffold(self):
        session = FbSessionContext(
            c_user="111",
            xs="xss",
            fb_dtsg="dtsg",
            cookies={"c_user": "111", "xs": "xss"},
            lsd="lsd",
            jazoest="2123",
            client_revision="10",
            comet_scaffold={
                "__dyn": "abc",
                "__spin_r": "99",
                "__spin_b": "trunk",
                "__crn": "comet.fbweb.CometMarketplaceComposerRoute",
            },
        )
        response = MagicMock()
        response.status_code = 200
        response.text = '{"data":{"ok":1}}'
        http = MagicMock()
        http.post.return_value = response

        with GraphQLClient(session, client=http) as gql:
            gql.post("999", {"input": {}}, friendly_name="Create", require_scaffold=True)

        form = http.post.call_args.kwargs["data"]
        self.assertEqual(form["__dyn"], "abc")
        self.assertEqual(form["__spin_r"], "99")
        self.assertEqual(form["__crn"], "comet.fbweb.CometMarketplaceComposerRoute")
        self.assertEqual(form["doc_id"], "999")
        self.assertIn("__spin_t", form)


class CreateWireTests(unittest.TestCase):
    def test_create_falls_back_when_templates_missing(self):
        from src.facebook.poster import create_vehicle_listing
        from src.models import Vehicle

        vehicle = Vehicle(
            autosell_id="obj1",
            slug="s",
            title="Model",
            brand="Ford",
            year="2020",
            price="$100",
            mileage="1 km",
            version="",
            url="https://example.com",
        )
        page = MagicMock()
        page.context = MagicMock()
        page.url = "https://www.facebook.com/marketplace/create/vehicle"

        with (
            patch.dict(
                "os.environ",
                {"FB_USE_GRAPHQL": "1", "FB_GQL_CREATE_FALLBACK_UI": "0"},
            ),
            patch(
                "src.facebook.graphql.poster.create_vehicle_listing_graphql",
                side_effect=__import__(
                    "src.facebook.graphql.errors", fromlist=["FacebookGraphqlFallback"]
                ).FacebookGraphqlFallback("no template"),
            ),
        ):
            with self.assertRaises(Exception) as cm:
                create_vehicle_listing(
                    page,
                    vehicle,
                    fb_config={},
                    max_photos=1,
                    log_dir=MagicMock(),
                )
            # Create UI fallback is off — do not open Playwright composer.
            self.assertIn("UI fallback disabled", str(cm.exception))
            self.assertIn("no template", str(cm.exception).lower())


if __name__ == "__main__":
    unittest.main()
