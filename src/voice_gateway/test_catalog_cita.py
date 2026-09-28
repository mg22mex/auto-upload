"""Catalog desire bind + cross-branch cita without sticky Autométrica."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.inventory.catalog_match import match_desired_in_catalog
from src.models import Vehicle
from src.voice_gateway import vapi_chat as vc
from src.voice_gateway.vapi_chat import VapiChatSessionStore, force_book_appointment


def _corolla_stock() -> list[Vehicle]:
    return [
        Vehicle(
            autosell_id="obj1196",
            slug="toyota-corolla-xle-2022",
            title="Corolla XLE *",
            brand="Toyota",
            year="2022",
            price="$365,000",
            mileage="40,000 kms",
            version="XLE",
            url="https://www.autosell.mx/catalogo/toyota-corolla-xle-2022",
            image_urls=[],
        )
    ]


class TestCatalogMatchCorolla(unittest.TestCase):
    def test_matches_xle_2022_periferico(self):
        hit = match_desired_in_catalog("Toyota Corolla", vehicles=_corolla_stock())
        assert hit is not None
        self.assertEqual(hit.vehicle.autosell_id, "obj1196")
        self.assertEqual(hit.branch_key, "periferico")
        self.assertEqual(hit.price, 365000.0)
        self.assertIn("Corolla", hit.display_name)
        self.assertIn("*", hit.display_name)


class TestForceBookNoStickyTradein(unittest.TestCase):
    def test_viewing_cita_drops_autometrica_note(self):
        with patch.object(vc, "create_vapi_lead", create=True):
            pass
        with patch(
            "src.voice_gateway.vapi_bridge.create_vapi_lead",
            return_value={"status": "created", "lead_id": 1, "dry_run": True},
        ):
            booked = force_book_appointment(
                text="Quiero agendar una cita para ver un Corolla",
                phone="5216141754852",
                customer_name="Marco",
                branch="san_felipe",
                channel_branch="san_felipe",
                meta={
                    "interested_vehicle": "2022 Toyota Corolla XLE *",
                    "vehicle_branch": "periferico",
                    "vehicle_price": 365000,
                    "trade_in_label": "Toyota Corolla 2020 LE",
                    "valor_compra": 195500,
                    "tradein_summary": "Autométrica ~$195,500",
                },
            )
        self.assertIsNone(booked.get("tradein_note"))
        self.assertEqual(booked["branch"], "periferico")
        self.assertTrue(booked["cross_branch"])
        self.assertIn("Periférico", booked["speech"])
        self.assertIn("Corolla", booked["speech"])
        self.assertNotIn("195,500", booked["speech"])
        self.assertNotIn("Autométrica", booked["speech"] or "")


class TestChatWithBeatrizCatalogCita(unittest.TestCase):
    def test_cita_ver_corolla_no_tradein_tool(self):
        msg = (
            "Hola. Quiero agendar una cita para ver un Corolla que tienen, "
            "por favor. Se puede en media hora?"
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = VapiChatSessionStore(Path(tmp) / "chats.db")
            # Sticky prior Autométrica that must NOT win.
            store.set_chat_id(
                "5216141754852",
                "prev",
                meta={
                    "trade_in_label": "Toyota Corolla 2020 LE",
                    "valor_compra": 195500,
                    "tradein_summary": "Estimación ~$195,500",
                    "interested_vehicle": "Ford Mustang GT 2025",
                },
            )
            with patch.object(vc, "_api_key", return_value="k"), patch.object(
                vc, "_assistant_id", return_value="asst"
            ), patch.object(vc, "_http_json") as http, patch(
                "src.inventory.catalog_match.load_public_catalog",
                return_value=_corolla_stock(),
            ), patch(
                "src.voice_gateway.vapi_bridge.create_vapi_lead",
                return_value={
                    "status": "created",
                    "lead_id": 99,
                    "dry_run": True,
                    "branch": "periferico",
                },
            ) as crm:
                result = vc.chat_with_beatriz(
                    text=msg,
                    phone="5216141754852",
                    customer_name="Marco Gastelum",
                    branch="san_felipe",
                    instance="autosell_san_felipe",
                    store=store,
                )
                meta = store.get_meta("5216141754852", "autosell_san_felipe")
        http.assert_not_called()
        self.assertIn("book_appointment", result.tools_called)
        self.assertNotIn("get_tradein_valuation", result.tools_called)
        self.assertIsNone(result.tradein_summary)
        self.assertIn("Corolla", result.interested_vehicle or "")
        self.assertEqual(float(meta.get("vehicle_price") or 0), 365000.0)
        self.assertIn("Periférico", result.reply_text)
        self.assertNotIn("195,500", result.reply_text)
        crm.assert_called_once()
        args = crm.call_args[0][0]
        self.assertEqual(args.branch, "periferico")
        self.assertIsNone(args.tradein_summary)


if __name__ == "__main__":
    unittest.main()
