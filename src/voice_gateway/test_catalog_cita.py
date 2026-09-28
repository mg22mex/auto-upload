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
    def test_viewing_cita_asks_pending_not_crm(self):
        with patch(
            "src.voice_gateway.vapi_bridge.create_vapi_lead",
            return_value={"status": "created", "lead_id": 1, "dry_run": True},
        ) as crm:
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
        crm.assert_not_called()
        self.assertTrue(booked.get("pending_confirmation"))
        self.assertEqual(booked["tool"], "pending_appointment_confirmation")
        self.assertEqual(booked["branch"], "periferico")
        self.assertIn("Periférico", booked["speech"])
        self.assertNotIn("195,500", booked["speech"])

    def test_confirm_books_periferico(self):
        with patch(
            "src.voice_gateway.vapi_bridge.create_vapi_lead",
            return_value={"status": "created", "lead_id": 2, "dry_run": True},
        ) as crm:
            booked = force_book_appointment(
                text="Confirma a esa hora, 5:30 pm",
                phone="5216141754852",
                customer_name="Marco Gastelum",
                branch="san_felipe",
                channel_branch="san_felipe",
                confirm=True,
                meta={
                    "pending_appointment_confirmation": "1",
                    "pending_appointment_when": "en media hora",
                    "pending_appointment_vehicle": "2022 Toyota Corolla XLE *",
                    "pending_appointment_branch": "periferico",
                    "interested_vehicle": "2022 Toyota Corolla XLE *",
                    "vehicle_branch": "periferico",
                    "vehicle_price": 365000,
                },
            )
        crm.assert_called_once()
        args = crm.call_args[0][0]
        self.assertEqual(args.branch, "periferico")
        self.assertIsNone(args.tradein_summary)
        self.assertIn("5:30", booked["when"])
        self.assertIn("Cita confirmada", booked["speech"])
        self.assertIn("Periférico", booked["speech"])
        self.assertIn("Corolla", booked["speech"])
        self.assertFalse(booked.get("pending_confirmation"))


class TestChatWithBeatrizCatalogCita(unittest.TestCase):
    def test_cita_ver_corolla_asks_pending_then_confirm_books(self):
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
                pending = vc.chat_with_beatriz(
                    text=msg,
                    phone="5216141754852",
                    customer_name="Marco Gastelum",
                    branch="san_felipe",
                    instance="autosell_san_felipe",
                    store=store,
                )
                meta_pending = store.get_meta(
                    "5216141754852", "autosell_san_felipe"
                )
                self.assertIn(
                    "pending_appointment_confirmation", pending.tools_called
                )
                self.assertNotIn("get_tradein_valuation", pending.tools_called)
                self.assertIsNone(pending.tradein_summary)
                self.assertIn("Corolla", pending.interested_vehicle or "")
                self.assertEqual(
                    meta_pending.get("pending_appointment_confirmation"), "1"
                )
                self.assertEqual(
                    meta_pending.get("pending_appointment_branch"), "periferico"
                )
                self.assertIn(
                    "confirmo esa visita", pending.reply_text.casefold()
                )
                self.assertNotIn("195,500", pending.reply_text)
                crm.assert_not_called()

                confirmed = vc.chat_with_beatriz(
                    text="Confirma a esa hora, 5:30 pm",
                    phone="5216141754852",
                    customer_name="Marco Gastelum",
                    branch="san_felipe",
                    instance="autosell_san_felipe",
                    store=store,
                )
                meta_done = store.get_meta(
                    "5216141754852", "autosell_san_felipe"
                )
        http.assert_not_called()
        self.assertIn("book_appointment", confirmed.tools_called)
        self.assertIn("Cita confirmada", confirmed.reply_text)
        self.assertIn("5:30", confirmed.reply_text)
        self.assertIn("Periférico", confirmed.reply_text)
        self.assertEqual(meta_done.get("pending_appointment_confirmation"), "")
        crm.assert_called_once()
        args = crm.call_args[0][0]
        self.assertEqual(args.branch, "periferico")
        self.assertIsNone(args.tradein_summary)



if __name__ == "__main__":
    unittest.main()
