"""Tests — interested_vehicle follows the latest inventory/financing inquiry."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.voice_gateway.session_vehicle import (
    inventory_vehicle_label,
    remember_interested_vehicle,
    resolve_interested_vehicle,
)
from src.voice_gateway.vapi_chat import VapiChatSessionStore
from src.whatsapp_worker.inbound import (
    STATE_AI_ACTIVE,
    QualificationSession,
    QualificationStore,
    WhatsAppInboundEvent,
    apply_qualification_to_odoo,
    build_qualification_notes,
    process_qualification_turn,
)


class TestSessionVehicleHelpers(unittest.TestCase):
    def test_inventory_label_prefers_single_hit(self):
        label = inventory_vehicle_label(
            brand="Toyota",
            model="Corolla",
            rows=[{"name": "Toyota Corolla XLE 2022", "list_price": 365000}],
        )
        self.assertIn("Corolla", label)

    def test_remember_overwrites_prior_vehicle(self):
        with tempfile.TemporaryDirectory() as tmp:
            qstore = QualificationStore(Path(tmp) / "q.db")
            cstore = VapiChatSessionStore(Path(tmp) / "chat.db")
            phone = "6141112222"
            sess = QualificationSession(
                phone=phone,
                instance="autosell_periferico",
                state=STATE_AI_ACTIVE,
                vehicle_interest="Ford Ranger XLT 2021",
                updated_at="2026-01-01T00:00:00Z",
            )
            qstore.save(sess)
            cstore.set_chat_id(
                phone,
                "chat1",
                "autosell_periferico",
                meta={"vehicle_name": "Ford Ranger XLT 2021"},
            )

            remember_interested_vehicle(
                "Toyota Corolla 2022",
                phone=phone,
                instance="autosell_periferico",
                price=365000,
                qualification_store=qstore,
                chat_store=cstore,
            )
            refreshed = qstore.get(phone, "autosell_periferico")
            assert refreshed is not None
            self.assertEqual(refreshed.vehicle_interest, "Toyota Corolla 2022")
            meta = cstore.get_meta(phone, "autosell_periferico")
            self.assertEqual(meta.get("vehicle_name"), "Toyota Corolla 2022")
            self.assertEqual(meta.get("interested_vehicle"), "Toyota Corolla 2022")

            resolved = resolve_interested_vehicle(
                phone,
                instance="autosell_periferico",
                fallback="Ford Ranger XLT 2021",
                qualification_store=qstore,
                chat_store=cstore,
            )
            self.assertEqual(resolved, "Toyota Corolla 2022")


class TestCarAThenCarBThenAppointment(unittest.TestCase):
    """Quote Ranger → quote Corolla → cita binds Corolla (not Ranger)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.qstore = QualificationStore(Path(self.tmp.name) / "q.db")
        self.cstore = VapiChatSessionStore(Path(self.tmp.name) / "chat.db")
        self.phone = "6149998877"
        self.instance = "autosell_periferico"
        sess = QualificationSession(
            phone=self.phone,
            instance=self.instance,
            state=STATE_AI_ACTIVE,
            contact_name="Marco",
            branch="periferico",
            physical_location="Periférico",
            initial_message="Hola, quiero una Ford Ranger",
            vehicle_interest="Ford Ranger XLT 2021",
            updated_at="2026-01-01T00:00:00Z",
        )
        self.qstore.save(sess)
        self.cstore.set_chat_id(
            self.phone,
            "c1",
            self.instance,
            meta={"vehicle_name": "Ford Ranger XLT 2021"},
        )

    def tearDown(self):
        self.qstore.close()
        self.tmp.cleanup()

    def _event(self, text: str) -> WhatsAppInboundEvent:
        return WhatsAppInboundEvent(
            phone=self.phone,
            name="Marco",
            text=text,
            instance=self.instance,
            message_id="m1",
        )

    def test_financing_then_appointment_uses_car_b(self):
        # Car A already in session; financing for Car B updates stores.
        remember_interested_vehicle(
            "Toyota Corolla 2022",
            phone=self.phone,
            instance=self.instance,
            price=365000,
            qualification_store=self.qstore,
            chat_store=self.cstore,
        )
        sess = self.qstore.get(self.phone, self.instance)
        assert sess is not None
        self.assertEqual(sess.vehicle_interest, "Toyota Corolla 2022")

        vapi_result = MagicMock()
        vapi_result.ok = True
        vapi_result.reply_text = "Perfecto, te esperamos mañana."
        vapi_result.chat_id = "c2"
        vapi_result.financing_sent = False
        vapi_result.financing_forced = False
        vapi_result.tools_called = []
        vapi_result.vehicle_name = "Toyota Corolla 2022"
        vapi_result.interested_vehicle = "Toyota Corolla 2022"
        vapi_result.error = None

        with (
            patch(
                "src.voice_gateway.vapi_chat.vapi_wa_text_first_enabled",
                return_value=True,
            ),
            patch(
                "src.voice_gateway.vapi_chat.chat_with_beatriz",
                return_value=vapi_result,
            ),
            patch(
                "src.voice_gateway.session_vehicle.resolve_interested_vehicle",
                return_value="Toyota Corolla 2022",
            ),
        ):
            turn = process_qualification_turn(
                self._event("sí, agendemos mañana a las 11"),
                sess,
                branch="periferico",
                branch_id=1,
                physical_location="Periférico",
            )

        self.assertTrue(turn.appointment_handoff)
        self.assertEqual(turn.session.vehicle_interest, "Toyota Corolla 2022")
        self.assertIn("Vehículo de interés: Toyota Corolla 2022", turn.odoo_notes or "")
        self.assertNotIn("Vehículo de interés: Ford Ranger", turn.odoo_notes or "")
        notes = build_qualification_notes(turn.session)
        self.assertIn("Toyota Corolla 2022", notes)

        odoo = MagicMock()
        odoo.create_or_update_lead.return_value = MagicMock(lead_id=99)
        apply_qualification_to_odoo(odoo, self._event("cita"), turn)
        _args, kwargs = odoo.create_or_update_lead.call_args
        # positional: name, phone, vehicle_name, branch_id
        self.assertEqual(_args[2], "Toyota Corolla 2022")

    def test_lead_payload_binds_last_vehicle_on_appointment(self):
        from src.voice_gateway.vapi_bridge import handle_lead_payload

        remember_interested_vehicle(
            "Toyota Corolla 2022",
            phone=self.phone,
            instance=self.instance,
            qualification_store=self.qstore,
            chat_store=self.cstore,
        )
        manager = MagicMock()
        manager.create_or_update_lead.return_value = {
            "status": "updated",
            "lead_id": 55,
            "branch": "periferico",
            "dry_run": False,
            "stage_name": "Beatriz Cita",
            "assignment": {},
        }
        with patch(
            "src.voice_gateway.session_vehicle.resolve_interested_vehicle",
            return_value="Toyota Corolla 2022",
        ):
            resp = handle_lead_payload(
                {
                    "name": "Marco",
                    "phone": self.phone,
                    # Stale LLM arg — should be overridden by session Car B.
                    "interested_vehicle": "Ford Ranger XLT 2021",
                    "appointment_date": "mañana 11am",
                },
                manager=manager,
            )
        self.assertIn("Marco", resp.results[0].result)
        payload = manager.create_or_update_lead.call_args.args[0]
        self.assertEqual(payload["vehicle_name"], "Toyota Corolla 2022")
        self.assertNotIn("Ranger", payload["vehicle_name"])


class TestFinancingUpdatesSession(unittest.TestCase):
    def test_handle_financing_remembers_vehicle(self):
        from src.voice_gateway.vapi_bridge import handle_financing_payload

        with tempfile.TemporaryDirectory() as tmp:
            qstore = QualificationStore(Path(tmp) / "q.db")
            cstore = VapiChatSessionStore(Path(tmp) / "chat.db")
            phone = "6145551212"
            qstore.save(
                QualificationSession(
                    phone=phone,
                    instance="",
                    state=STATE_AI_ACTIVE,
                    vehicle_interest="Ford Ranger XLT 2021",
                    updated_at="2026-01-01T00:00:00Z",
                )
            )
            with patch(
                "src.voice_gateway.session_vehicle.remember_interested_vehicle",
                wraps=remember_interested_vehicle,
            ) as remember:
                # Force stores via side_effect wrapper
                def _remember(vehicle, **kwargs):
                    return remember_interested_vehicle(
                        vehicle,
                        phone=kwargs.get("phone") or phone,
                        price=kwargs.get("price"),
                        qualification_store=qstore,
                        chat_store=cstore,
                    )

                remember.side_effect = _remember
                handle_financing_payload(
                    {
                        "vehicle_price": 365000,
                        "term_months": 48,
                        "down_payment": 73000,
                        "vehicle_name": "Toyota Corolla 2022",
                        "phone": phone,
                    }
                )
            sess = qstore.get(phone, "")
            assert sess is not None
            self.assertEqual(sess.vehicle_interest, "Toyota Corolla 2022")


if __name__ == "__main__":
    unittest.main()
