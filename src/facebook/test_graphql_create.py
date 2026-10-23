from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.facebook.graphql.create_fill import fill_create_vehicle_variables
from src.facebook.graphql.photos import (
    _extract_photo_id,
    _form_and_file_parts,
    _parse_upload_payload,
    _with_upload_query,
    resolve_photo_upload_config,
)
from src.facebook.graphql.session_ctx import FbSessionContext
from src.models import Vehicle


def _vehicle() -> Vehicle:
    return Vehicle(
        autosell_id="obj999",
        slug="ford-mustang",
        title="Mustang GT",
        brand="Ford",
        year="2025",
        price="$850,000",
        mileage="12,000 km",
        version="GT",
        url="https://www.autosell.mx/x",
    )


class ResolvePhotoUploadTests(unittest.TestCase):
    def test_sellerhub_capture_uses_default_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gql = root / "data" / "graphql"
            gql.mkdir(parents=True)
            (gql / "photo_upload.json").write_text(
                json.dumps(
                    {
                        "doc_id": "28470948832605147",
                        "friendly_name": "CometMarketplaceSellerHubContentContainerQuery",
                        "variables": {"count": 3},
                    }
                ),
                encoding="utf-8",
            )
            cfg = resolve_photo_upload_config(root)
        self.assertIn("upload.facebook.com", cfg["url"])
        self.assertIn("react_composer/attachments/photo/upload", cfg["url"])
        self.assertEqual(cfg["field_name"], "farr")
        self.assertNotIn("saveunpublished", cfg["url"])
        self.assertTrue(
            all("saveunpublished" not in u for u in cfg["alt_urls"])
        )

    def test_dead_composerx_url_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gql = root / "data" / "graphql"
            gql.mkdir(parents=True)
            (gql / "photo_upload.json").write_text(
                json.dumps(
                    {
                        "url": (
                            "https://www.facebook.com/ajax/composerx/"
                            "attachment/media/saveunpublished"
                        ),
                        "field_name": "farr",
                    }
                ),
                encoding="utf-8",
            )
            cfg = resolve_photo_upload_config(root)
        self.assertIn("upload.facebook.com", cfg["url"])
        self.assertNotIn("saveunpublished", cfg["url"])

    def test_explicit_url_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gql = root / "data" / "graphql"
            gql.mkdir(parents=True)
            (gql / "photo_upload.json").write_text(
                json.dumps(
                    {
                        "url": "https://example.com/upload",
                        "field_name": "file",
                    }
                ),
                encoding="utf-8",
            )
            cfg = resolve_photo_upload_config(root)
        self.assertEqual(cfg["url"], "https://example.com/upload")
        self.assertEqual(cfg["field_name"], "file")

    def test_multipart_excludes_dtsg_from_body(self):
        from src.facebook.graphql.photos import (
            _STRATEGY_BODY_DTSG,
            _STRATEGY_QUERY_DTSG,
            _upload_headers,
        )

        session = FbSessionContext(
            c_user="615",
            xs="xs",
            fb_dtsg="AQ:token",
            cookies={"c_user": "615", "xs": "xs", "datr": "d"},
            lsd="lsd1",
            jazoest="2123",
            client_revision="10",
        )
        data, files = _form_and_file_parts(
            session,
            form_fields={"source": "8", "waterfallxapp": "comet"},
            field_name="farr",
            filename="car.jpg",
            data_bytes=b"\xff\xd8\xff" + b"\x00" * 200,
            strategy=_STRATEGY_QUERY_DTSG,
        )
        self.assertEqual(data["profile_id"], "615")
        self.assertEqual(data["source"], "8")
        self.assertEqual(data["waterfallxapp"], "comet")
        self.assertIn("upload_id", data)
        self.assertTrue(data["upload_id"].startswith("jsc_c_"))
        self.assertNotIn("fb_dtsg", data)
        self.assertNotIn("lsd", data)
        self.assertNotIn("__user", data)
        self.assertIn("farr", files)
        self.assertEqual(files["farr"][0], "car.jpg")

        qurl = _with_upload_query(
            "https://upload.facebook.com/ajax/react_composer/attachments/photo/upload",
            session,
            strategy=_STRATEGY_QUERY_DTSG,
        )
        self.assertIn("fb_dtsg=", qurl)
        self.assertIn("AQ", qurl)
        self.assertIn("__a=1", qurl)
        self.assertNotIn("__user=", qurl)
        self.assertNotIn("jazoest=", qurl)

        body_data, _ = _form_and_file_parts(
            session,
            form_fields={"source": "8", "waterfallxapp": "comet"},
            field_name="farr",
            filename="car.jpg",
            data_bytes=b"\xff\xd8\xff" + b"\x00" * 200,
            strategy=_STRATEGY_BODY_DTSG,
        )
        self.assertEqual(body_data["fb_dtsg"], "AQ:token")
        self.assertEqual(body_data["lsd"], "lsd1")
        self.assertEqual(body_data["__user"], "615")
        body_q = _with_upload_query(
            "https://upload.facebook.com/ajax/react_composer/attachments/photo/upload",
            session,
            strategy=_STRATEGY_BODY_DTSG,
        )
        self.assertNotIn("fb_dtsg=", body_q)

        headers = _upload_headers(session, upload_host="upload.facebook.com")
        self.assertEqual(headers["sec-fetch-site"], "same-site")
        self.assertEqual(headers["Origin"], "https://www.facebook.com")
        self.assertIn("c_user=615", headers["Cookie"])
        self.assertIn("xs=xs", headers["Cookie"])

    def test_parse_live_composer_photo_id(self):
        """Captured Marketplace vehicle composer upload response."""
        raw = (
            'for (;;);{"__ar":1,"rid":"A7VuLtBPhl50FRFc_tw_YaV",'
            '"payload":{"isSpherical":false,"height":799,'
            '"imageSrc":"https://scontent.xx.fbcdn.net/v/t39.30808-6/x.jpg",'
            '"originalPhotoID":"","photoID":"1661606905435115",'
            '"thumbSrc":"https://scontent.xx.fbcdn.net/v/t39.30808-6/t.jpg",'
            '"width":1200},"lid":"7699905854972708843"}'
        )
        parsed = _parse_upload_payload(raw)
        self.assertIsInstance(parsed, dict)
        self.assertEqual(parsed.get("__ar"), 1)
        self.assertEqual(parsed["payload"]["photoID"], "1661606905435115")
        self.assertEqual(parsed["payload"]["originalPhotoID"], "")

        cfg = resolve_photo_upload_config()
        photo_id = _extract_photo_id(parsed, cfg["id_paths"])
        self.assertEqual(photo_id, "1661606905435115")
        # Empty originalPhotoID must not win over photoID.
        self.assertNotEqual(photo_id, "")


class FillCreateVariablesTests(unittest.TestCase):
    def test_rewrites_title_price_photos_actor(self):
        captured = {
            "input": {
                "actor_id": "100047574648142",
                "client_mutation_id": "1",
                "attribution_id_v2": (
                    "CometMarketplaceComposerRoot.react,comet.marketplace.composer,"
                    "unexpected,1792627878534,19126,1606854132932955,,"
                ),
                "audience": {"marketplace": {"marketplace_id": "630562697113569"}},
                "data": {
                    "common": {
                        "category_id": "807311116002614",
                        "description": {"text": "old"},
                        "item_price": {"price": "385000"},
                        "latitude": 28.6,
                        "longitude": -106.0,
                        "serialized_verticals_data": json.dumps(
                            {
                                "make": "410067716491465",
                                "model": "CX5 GT",
                                "year": 2021,
                                "mileage": "87500",
                                "isKM": True,
                                "type": "autos",
                                "vehicleCategory": "car&truck/",
                                "vehicleType": "suv",
                            }
                        ),
                        "title": "2021 Mazda CX5 GT",
                        "photo_ids": ["1", "2", "3"],
                        "video_ids": [],
                        "xpost_target_ids": [],
                    }
                },
            }
        }
        session = FbSessionContext(
            c_user="61568552875980",
            xs="xs",
            fb_dtsg="dtsg",
            cookies={"c_user": "61568552875980", "xs": "xs"},
        )
        variables = fill_create_vehicle_variables(
            captured,
            session=session,
            vehicle=_vehicle(),
            fb_config={
                "location_latitude": 28.64,
                "location_longitude": -106.06,
                "country": "MX",
                "graphql_make_ids": {"ford": "999888777", "mazda": "410067716491465"},
            },
            photo_ids=["111", "222"],
        )
        common = variables["input"]["data"]["common"]
        self.assertEqual(variables["input"]["actor_id"], "61568552875980")
        self.assertIn("Mustang", common["title"])
        self.assertEqual(common["item_price"], {"price": "850000"})
        self.assertNotIn("currency", common["item_price"])
        self.assertEqual(common["photo_ids"], ["111", "222"])
        verticals = json.loads(common["serialized_verticals_data"])
        self.assertEqual(verticals["year"], 2025)
        self.assertEqual(verticals["mileage"], "12000")
        self.assertEqual(verticals["make"], "999888777")
        self.assertIn("Mustang", verticals["model"])
        self.assertNotEqual(verticals["vehicleType"], "car&truck/")
        self.assertEqual(verticals["vehicleCategory"], "car&truck/")

    def test_rejects_foreign_capture_make_for_chevrolet(self):
        captured = {
            "input": {
                "actor_id": "1",
                "client_mutation_id": "1",
                "audience": {"marketplace": {"marketplace_id": "630562697113569"}},
                "data": {
                    "common": {
                        "category_id": "807311116002614",
                        "description": {"text": "old"},
                        "item_price": {"price": "1"},
                        "latitude": 28.6,
                        "longitude": -106.0,
                        "serialized_verticals_data": json.dumps(
                            {
                                "make": "410067716491465",
                                "model": "CX5",
                                "year": 2021,
                                "mileage": "1",
                                "isKM": True,
                                "type": "autos",
                                "vehicleCategory": "car&truck/",
                                "vehicleType": "suv",
                            }
                        ),
                        "title": "old",
                        "photo_ids": [],
                        "video_ids": [],
                        "xpost_target_ids": [],
                    }
                },
            }
        }
        session = FbSessionContext(
            c_user="100047574648142", xs="xs", fb_dtsg="dtsg", cookies={}
        )
        colorado = Vehicle(
            autosell_id="obj1199",
            slug="chevrolet-colorado-zr2-2025",
            title="Colorado ZR2 +",
            brand="Chevrolet",
            year="2025",
            price="$949,000",
            mileage="13,000 kms",
            version="ZR2",
            url="https://www.autosell.mx/x",
        )
        variables = fill_create_vehicle_variables(
            captured,
            session=session,
            vehicle=colorado,
            fb_config={
                "location_latitude": 28.64,
                "location_longitude": -106.06,
                "country": "MX",
                "graphql_make_ids": {
                    "mazda": "410067716491465",
                    "chevrolet": "555555555555555",
                },
            },
            photo_ids=["1661606905435115"],
        )
        common = variables["input"]["data"]["common"]
        verticals = json.loads(common["serialized_verticals_data"])
        self.assertEqual(verticals["make"], "555555555555555")
        # Truck body → vehicleType truck (do not force capture suv).
        self.assertEqual(verticals["vehicleType"], "truck")
        self.assertEqual(verticals["fuelType"], "GASOLINE")
        self.assertEqual(verticals["titleStatus"], "CLEAN")
        self.assertEqual(verticals["vehicleCondition"], "excellent")
        self.assertEqual(verticals["model"], "Colorado ZR2")
        self.assertNotIn("+", verticals["model"])
        self.assertEqual(common["item_price"], {"price": "949000"})
        self.assertEqual(common["category_id"], "807311116002614")
        self.assertNotIn("**", common["description"]["text"])
        self.assertIn("Colorado ZR2", common["title"])
        self.assertNotIn("+", common["title"])
        # Catalog source unchanged; branch marker mapped into Sucursal line.
        self.assertEqual(colorado.title, "Colorado ZR2 +")
        self.assertIn("Sucursal: San Felipe", common["description"]["text"])

    def test_filled_shape_matches_live_chevrolet_capture(self):
        """No invented keys vs data/graphql/create_vehicle.json."""
        root = Path(__file__).resolve().parents[2]
        capture_path = root / "data" / "graphql" / "create_vehicle.json"
        if not capture_path.is_file():
            self.skipTest("create_vehicle.json not present")
        captured = json.loads(capture_path.read_text(encoding="utf-8"))["variables"]
        session = FbSessionContext(
            c_user="100047574648142", xs="xs", fb_dtsg="dtsg", cookies={}
        )
        colorado = Vehicle(
            autosell_id="obj1199",
            slug="chevrolet-colorado-zr2-2025",
            title="Colorado ZR2 +",
            brand="Chevrolet",
            year="2025",
            price="$949,000",
            mileage="13,000 kms",
            version="ZR2",
            url="https://www.autosell.mx/x",
        )
        filled = fill_create_vehicle_variables(
            captured,
            session=session,
            vehicle=colorado,
            fb_config={
                "location_latitude": 28.644104003906,
                "location_longitude": -106.06750488281,
                "country": "MX",
                "graphql_make_ids": {
                    "mazda": "410067716491465",
                    "chevrolet": "1914016008726893",
                },
            },
            photo_ids=["1661633042099168", "1661606905435115"],
        )
        cap_common = captured["input"]["data"]["common"]
        fill_common = filled["input"]["data"]["common"]
        self.assertEqual(set(cap_common.keys()), set(fill_common.keys()))
        self.assertEqual(
            set(captured["input"].keys()), set(filled["input"].keys())
        )
        self.assertEqual(
            set(captured["input"]["data"].keys()),
            set(filled["input"]["data"].keys()),
        )
        self.assertEqual(fill_common["item_price"].keys(), {"price"})
        self.assertEqual(fill_common["category_id"], cap_common["category_id"])
        self.assertIsNone(fill_common["source_type"])
        self.assertIsNone(filled["input"]["data"]["client_application"])
        self.assertIsNone(filled["input"]["vin_autofill_summary"])
        self.assertIsInstance(fill_common["photo_ids"], list)
        self.assertTrue(all(isinstance(p, str) for p in fill_common["photo_ids"]))
        verticals = json.loads(fill_common["serialized_verticals_data"])
        cap_vert = json.loads(cap_common["serialized_verticals_data"])
        self.assertEqual(set(verticals.keys()), set(cap_vert.keys()))
        self.assertEqual(verticals["make"], "1914016008726893")
        self.assertEqual(verticals["vehicleType"], "truck")
        self.assertEqual(verticals["exteriorColor"], "silver")
        self.assertEqual(verticals["fuelType"], "GASOLINE")
        self.assertEqual(verticals["titleStatus"], "CLEAN")
        self.assertEqual(verticals["vehicleCondition"], "excellent")
        self.assertEqual(verticals["interiorColor"], "black")
        self.assertEqual(fill_common["category_id"], "807311116002614")
        self.assertEqual(verticals["vehicleCategory"], "car&truck/")
        self.assertEqual(verticals["model"], "Colorado ZR2")
        self.assertIsInstance(verticals["year"], int)
        self.assertIsInstance(verticals["vehicleFeatures"], list)
        self.assertNotIn("**", fill_common["description"]["text"])
        self.assertEqual(colorado.title, "Colorado ZR2 +")
        self.assertIn("+", colorado.marketplace_title)
        self.assertNotIn("+", fill_common["title"])
        self.assertIn("Sucursal: San Felipe", fill_common["description"]["text"])
        # client_mutation_id must be a plain digit string like the capture.
        self.assertTrue(
            str(filled["input"]["client_mutation_id"]).isdigit(),
            filled["input"]["client_mutation_id"],
        )

    def test_fill_never_mutates_vehicle_title(self):
        """GraphQL fill must not rewrite Vehicle / catalog branch markers."""
        vehicle = Vehicle(
            autosell_id="obj1199",
            slug="chevrolet-colorado-zr2-2025",
            title="Colorado ZR2 +",
            brand="Chevrolet",
            year="2025",
            price="$949,000",
            mileage="13,000 kms",
            version="ZR2",
            url="https://www.autosell.mx/x",
        )
        before = vehicle.to_dict()
        session = FbSessionContext(
            c_user="1", xs="xs", fb_dtsg="dtsg", cookies={}
        )
        template = {
            "input": {
                "actor_id": "1",
                "client_mutation_id": "1",
                "audience": {"marketplace": {"marketplace_id": "630562697113569"}},
                "data": {
                    "client_application": None,
                    "common": {
                        "category_id": "807311116002614",
                        "comments_disabled": False,
                        "description": {"text": "x"},
                        "draft_type": None,
                        "is_photo_order_set_by_seller": False,
                        "item_price": {"price": "1"},
                        "latitude": 28.6,
                        "listing_email_id": None,
                        "longitude": -106.0,
                        "serialized_verticals_data": json.dumps(
                            {
                                "exteriorColor": "white",
                                "fuelType": "GASOLINE",
                                "interiorColor": "black",
                                "isKM": True,
                                "isManualTransmission": False,
                                "make": "1914016008726893",
                                "mileage": "1",
                                "model": "Test",
                                "titleStatus": "CLEAN",
                                "type": "autos",
                                "vehicleCategory": "car&truck/",
                                "vehicleCondition": "excellent",
                                "vehicleFeatures": [],
                                "vehicleType": "suv",
                                "vin": "",
                                "year": 2021,
                            }
                        ),
                        "source_type": None,
                        "title": "old",
                        "video_ids": [],
                        "xpost_target_ids": [],
                        "photo_ids": [],
                    },
                },
                "vin_autofill_summary": None,
            }
        }
        fill_create_vehicle_variables(
            template,
            session=session,
            vehicle=vehicle,
            fb_config={
                "location_latitude": 28.6,
                "location_longitude": -106.0,
                "graphql_make_ids": {"chevrolet": "1914016008726893"},
            },
            photo_ids=["1"],
        )
        self.assertEqual(vehicle.to_dict(), before)
        self.assertTrue(vehicle.title.endswith("+"))


class CreateNoUiFallbackTests(unittest.TestCase):
    def test_create_fallback_defaults_off(self):
        from src.facebook.graphql.mode import graphql_create_ui_fallback

        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("FB_GQL_CREATE_FALLBACK_UI", None)
            self.assertFalse(graphql_create_ui_fallback())


if __name__ == "__main__":
    unittest.main()
