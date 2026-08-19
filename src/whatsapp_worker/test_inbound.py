"""Unit tests — Evolution inbound parse + qualification flow."""
from __future__ import annotations

import unittest

from src.whatsapp_worker.inbound import (
    PAYMENT_FINANCING,
    PAYMENT_TRADE_IN,
    STATE_AWAITING_DOWN_PAYMENT,
    STATE_AWAITING_PAYMENT_METHOD,
    STATE_AWAITING_TRADE_IN,
    STATE_HANDOFF_TO_HUMAN,
    STATE_NEW_LEAD,
    WA_CHANNEL,
    QualificationStore,
    WhatsAppInboundEvent,
    build_qualification_notes,
    inbound_to_voice_payload,
    parse_evolution_inbound,
    parse_payment_choice,
    process_qualification_turn,
)

_UPSERT = {
    "event": "messages.upsert",
    "instance": "autosell_main",
    "data": {
        "key": {
            "remoteJid": "5216141234567@s.whatsapp.net",
            "fromMe": False,
            "id": "ABCD",
        },
        "pushName": "Ana Pérez",
        "message": {"conversation": "Quiero cotizar un Mazda CX-5 a 36 meses"},
    },
}


def _event(
    text: str,
    *,
    phone: str = "5216141234567",
    name: str = "Ana",
    instance: str = "autosell_san_felipe",
) -> WhatsAppInboundEvent:
    return WhatsAppInboundEvent(
        phone=phone,
        name=name,
        text=text,
        instance=instance,
        message_id="msg",
    )


class TestParseEvolutionInbound(unittest.TestCase):
    def test_conversation_text(self):
        events = parse_evolution_inbound(_UPSERT)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].phone, "5216141234567")
        self.assertEqual(events[0].name, "Ana Pérez")
        self.assertIn("Mazda", events[0].text)
        self.assertEqual(events[0].instance, "autosell_main")

    def test_skips_from_me(self):
        payload = {
            "event": "messages.upsert",
            "data": {
                "key": {
                    "remoteJid": "5216141234567@s.whatsapp.net",
                    "fromMe": True,
                    "id": "1",
                },
                "message": {"conversation": "echo"},
            },
        }
        self.assertEqual(parse_evolution_inbound(payload), [])

    def test_skips_groups(self):
        payload = {
            "event": "messages.upsert",
            "data": {
                "key": {"remoteJid": "120363@g.us", "fromMe": False, "id": "1"},
                "message": {"conversation": "hola"},
            },
        }
        self.assertEqual(parse_evolution_inbound(payload), [])

    def test_extended_text(self):
        payload = {
            "event": "messages.upsert",
            "data": {
                "key": {
                    "remoteJid": "6141234567@s.whatsapp.net",
                    "fromMe": False,
                    "id": "2",
                },
                "pushName": "Luis",
                "message": {"extendedTextMessage": {"text": "precio 150000"}},
            },
        }
        events = parse_evolution_inbound(payload)
        self.assertEqual(events[0].text, "precio 150000")

    def test_voice_payload_shape(self):
        event = parse_evolution_inbound(_UPSERT)[0]
        body = inbound_to_voice_payload(event)
        self.assertEqual(body["channel"], WA_CHANNEL)
        self.assertEqual(body["caller_phone"], event.phone)
        self.assertEqual(body["transcript"], event.text)
        self.assertEqual(body["vehicle_interest"], event.text)


class TestPaymentParsing(unittest.TestCase):
    def test_parse_payment_choices(self):
        self.assertEqual(parse_payment_choice("2"), PAYMENT_FINANCING)
        self.assertEqual(parse_payment_choice("financiamiento"), PAYMENT_FINANCING)
        self.assertEqual(parse_payment_choice("permuta"), PAYMENT_TRADE_IN)
        self.assertIsNone(parse_payment_choice("maybe later"))


class TestQualificationFlow(unittest.TestCase):
    def setUp(self) -> None:
        self.store = QualificationStore(":memory:")
        self.branch = {
            "branch": "san_felipe",
            "branch_id": 5,
            "physical_location": "San Felipe",
        }

    def tearDown(self) -> None:
        self.store.close()

    def _turn(self, text: str, session=None):
        return process_qualification_turn(
            _event(text),
            session,
            branch=self.branch["branch"],
            branch_id=self.branch["branch_id"],
            physical_location=self.branch["physical_location"],
        )

    def test_new_lead_welcome_and_payment_prompt(self):
        turn = self._turn("Busco una Hilux")
        self.assertEqual(turn.session.state, STATE_AWAITING_PAYMENT_METHOD)
        self.assertTrue(turn.odoo_create)
        self.assertIn("Autosell San Felipe", turn.reply_text)
        self.assertIn("financiamiento", turn.reply_text.lower())

    def test_financing_multi_turn_to_handoff(self):
        t1 = self._turn("Busco una Hilux")
        self.store.save(t1.session)
        t2 = self._turn("financiamiento", self.store.get("5216141234567", "autosell_san_felipe"))
        self.assertEqual(t2.session.state, STATE_AWAITING_DOWN_PAYMENT)
        self.store.save(t2.session)
        t3 = self._turn(
            "$80,000",
            self.store.get("5216141234567", "autosell_san_felipe"),
        )
        self.assertEqual(t3.session.state, STATE_HANDOFF_TO_HUMAN)
        self.assertTrue(t3.odoo_handoff)
        self.assertIn("Enganche indicado: $80,000", build_qualification_notes(t3.session))
        self.assertIn("asesor", t3.reply_text.lower())

    def test_trade_in_multi_turn_to_handoff(self):
        t1 = self._turn("Cotización Vento")
        self.store.save(t1.session)
        t2 = self._turn("3", self.store.get("5216141234567", "autosell_san_felipe"))
        self.assertEqual(t2.session.state, STATE_AWAITING_TRADE_IN)
        self.store.save(t2.session)
        t3 = self._turn(
            "2018 Nissan Sentra",
            self.store.get("5216141234567", "autosell_san_felipe"),
        )
        self.assertEqual(t3.session.state, STATE_HANDOFF_TO_HUMAN)
        self.assertIn("2018 Nissan Sentra", t3.session.trade_in_vehicle)
        self.assertTrue(t3.odoo_handoff)

    def test_cash_skips_to_handoff(self):
        t1 = self._turn("Hola")
        self.store.save(t1.session)
        t2 = self._turn("contado", self.store.get("5216141234567", "autosell_san_felipe"))
        self.assertEqual(t2.session.state, STATE_HANDOFF_TO_HUMAN)
        self.assertTrue(t2.odoo_handoff)

    def test_invalid_payment_reprompts(self):
        t1 = self._turn("Hola")
        self.store.save(t1.session)
        t2 = self._turn("xyz", self.store.get("5216141234567", "autosell_san_felipe"))
        self.assertEqual(t2.session.state, STATE_AWAITING_PAYMENT_METHOD)
        self.assertIn("No entendí", t2.reply_text)

    def test_handoff_post_messages(self):
        t1 = self._turn("Hola")
        t1.session.state = STATE_HANDOFF_TO_HUMAN
        t2 = self._turn("¿y ahora?", t1.session)
        self.assertEqual(t2.session.state, STATE_HANDOFF_TO_HUMAN)
        self.assertIn("asesor", t2.reply_text.lower())


if __name__ == "__main__":
    unittest.main()
