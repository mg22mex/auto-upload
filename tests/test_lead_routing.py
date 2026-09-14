"""Unit tests — MG Quote Lead AI routing and appointment handoff."""
from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from src.lead_routing import (
    AGENT_AI,
    AGENT_HUMAN,
    MG_QUOTE_LEAD_TAG,
    PAYMENT_FINANCING_TRADE_IN,
    PAYMENT_TRADE_IN,
    STAGE_CITA,
    STAGE_PRIMER_CONTACTO,
    advance_trade_in_qualification,
    detect_appointment_intent,
    detect_forma_pago_permuta,
    extract_tags,
    format_ai_reply,
    handoff_appointment_to_rep,
    has_mg_quote_tag,
    parse_payment_intent,
    parse_trade_in_details,
    route_inbound_lead,
    should_defer_human_assignment,
)
from src.odoo_sync.crm import RepAssignment, reset_round_robin
from src.whatsapp_worker.client import QUOTE_DISCLAIMER


class TestTagExtraction(unittest.TestCase):
    def test_extracts_from_tag_names_and_m2m(self):
        tags = extract_tags(
            {
                "tag_names": "MG Quote Lead, Hot",
                "tag_ids": [[9, "MG Quote Lead"], [11, "Messenger Bot"]],
            }
        )
        self.assertIn(MG_QUOTE_LEAD_TAG, tags)
        self.assertIn("Hot", tags)
        self.assertIn("Messenger Bot", tags)

    def test_has_mg_quote_tag(self):
        self.assertTrue(has_mg_quote_tag(["MG Quote Lead"]))
        self.assertTrue(has_mg_quote_tag(payload={"tags": ["mg quote lead"]}))
        self.assertFalse(has_mg_quote_tag(["Other"]))


class TestRoutingDecision(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.dict(os.environ, {"AI_MG_QUOTE_LEADS": "true"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_mg_quote_defers_human(self):
        decision = route_inbound_lead({"tags": [MG_QUOTE_LEAD_TAG]})
        self.assertEqual(decision.agent, AGENT_AI)
        self.assertEqual(decision.stage_name, STAGE_PRIMER_CONTACTO)
        self.assertFalse(decision.assign_human)
        self.assertTrue(should_defer_human_assignment(payload={"tags": [MG_QUOTE_LEAD_TAG]}))

    def test_appointment_forces_human(self):
        intent = detect_appointment_intent("Quiero una cita mañana")
        decision = route_inbound_lead({"tags": [MG_QUOTE_LEAD_TAG]}, appointment=intent)
        self.assertEqual(decision.agent, AGENT_HUMAN)
        self.assertEqual(decision.stage_name, STAGE_CITA)
        self.assertTrue(decision.assign_human)


class TestAppointmentIntent(unittest.TestCase):
    def test_detects_cita_and_when(self):
        intent = detect_appointment_intent("Quiero agendar una cita mañana a las 11")
        self.assertTrue(intent.requested)
        self.assertEqual(intent.kind, "cita")
        self.assertIn("mañana", intent.when_text.lower())

    def test_detects_test_drive(self):
        intent = detect_appointment_intent("Me gustaría una prueba de manejo")
        self.assertTrue(intent.requested)
        self.assertEqual(intent.kind, "prueba_manejo")

    def test_ignores_generic_chat(self):
        intent = detect_appointment_intent("¿Cuál es el enganche mínimo?")
        self.assertFalse(intent.requested)


class TestAiReply(unittest.TestCase):
    def test_financing_reply(self):
        text = format_ai_reply(
            name="Marco",
            text="¿Opciones de financiamiento?",
            branch_name="San Felipe",
        )
        self.assertIn("financiamiento", text.lower())
        self.assertIn("cita", text.lower())

    def test_appointment_confirmation(self):
        intent = detect_appointment_intent("prueba de manejo mañana")
        text = format_ai_reply(
            name="Marco",
            text="prueba de manejo mañana",
            branch_name="San Felipe",
            appointment=intent,
        )
        self.assertIn("asesor", text.lower())
        self.assertIn("San Felipe", text)

    def test_permuta_reply_asks_version_km(self):
        text = format_ai_reply(
            name="Marco",
            text="Forma de pago: Auto a cambio (trade-in)",
            branch_name="San Felipe",
        )
        self.assertIn("versión", text.casefold())
        self.assertIn("kilometraje", text.casefold())
        self.assertIn("auto a cambio", text.casefold())


class TestTradeInQualification(unittest.TestCase):
    def test_detect_permuta(self):
        self.assertTrue(detect_forma_pago_permuta("Forma de pago: Auto a cambio (trade-in)"))
        self.assertTrue(detect_forma_pago_permuta("quiero permuta"))
        self.assertTrue(detect_forma_pago_permuta("a cambio"))
        self.assertFalse(detect_forma_pago_permuta("contado"))

    def test_combined_payment_intent(self):
        for text in (
            "2 y 3",
            "financiamiento y a cambio",
            "financiamiento con auto a cambio",
            "3 y 2",
        ):
            intent = parse_payment_intent(text)
            self.assertTrue(intent.financing, text)
            self.assertTrue(intent.trade_in, text)
            self.assertTrue(intent.is_combined_financing_trade_in, text)
            self.assertEqual(intent.session_key, PAYMENT_FINANCING_TRADE_IN, text)

    def test_prompts_for_version_and_km(self):
        details, reply, meta = advance_trade_in_qualification(
            "Forma de pago: Auto a cambio (trade-in). Auto a cambio: Toyota Corolla 2020."
        )
        self.assertTrue(details.is_trade_in)
        self.assertEqual(details.year, 2020)
        self.assertEqual(details.make, "Toyota")
        self.assertEqual(details.model, "Corolla")
        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn("Versión", reply)
        self.assertIn("Kilometraje", reply)
        self.assertIn("auto a cambio", reply.casefold())
        self.assertIsNone(meta)

    def test_combined_financing_trade_in_quote(self):
        prior = parse_trade_in_details(
            "financiamiento y a cambio. Toyota Corolla 2020",
        )
        self.assertTrue(prior.wants_financing)
        self.assertTrue(prior.is_trade_in)
        details, reply, meta = advance_trade_in_qualification(
            "Versión LE, Kilometraje 85000 km",
            prior=prior,
            lead_name="Marco Gastelum",
            vehicle_interest="camioneta",
            vehicle_price=450000,
        )
        self.assertEqual(details.version.upper(), "LE")
        self.assertEqual(details.mileage_km, 85000)
        self.assertTrue(details.wants_financing)
        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn(QUOTE_DISCLAIMER, reply)
        self.assertIn("Trade-in (equity)", reply)
        self.assertIn("enganche", reply.casefold())
        self.assertIsNotNone(meta)
        assert meta is not None
        self.assertTrue(meta["disclaimer_present"])
        self.assertTrue(meta["financing"])
        self.assertEqual(meta["payment_method"], PAYMENT_FINANCING_TRADE_IN)
        self.assertGreater(float(meta["valor_compra"]), 0)

    def test_voice_outbound_script_and_queue(self):
        from src.lead_routing import (
            QuoteVoiceContext,
            build_voice_agent_script,
            handle_outbound_voice_request,
            queue_outbound_voice_call,
        )

        ctx = QuoteVoiceContext(
            lead_id=1937,
            phone="5216140001937",
            vehicle_of_interest="Camioneta",
            valuation_amount="194500.00",
            monthly_payment="9212.13",
        )
        script = build_voice_agent_script(ctx)
        self.assertIn("Camioneta", script)
        self.assertIn("WhatsApp", script)
        self.assertIn("auto a cambio", script.casefold())
        with patch.dict(
            os.environ,
            {"VOICE_OUTBOUND_ENABLED": "true", "VOICE_OUTBOUND_DRY_RUN": "true"},
        ):
            queued = queue_outbound_voice_call(ctx)
        self.assertTrue(queued["queued"])
        self.assertTrue(queued["dry_run"])
        self.assertEqual(queued["payload"]["lead_id"], 1937)
        self.assertEqual(queued["payload"]["valuation_amount"], "194500.00")
        body = handle_outbound_voice_request(ctx.as_dict())
        self.assertEqual(body["status"], "queued")
        self.assertEqual(body["monthly_payment"], "9212.13")

    def test_quote_applies_valor_compra_and_disclaimer(self):
        prior = parse_trade_in_details(
            "Forma de pago: Auto a cambio (trade-in). Toyota Corolla 2020",
        )
        details, reply, meta = advance_trade_in_qualification(
            "Versión LE, Kilometraje 85000 km",
            prior=prior,
            lead_name="Marco Gastelum",
            vehicle_interest="camioneta",
            vehicle_price=450000,
        )
        self.assertEqual(details.version.upper(), "LE")
        self.assertEqual(details.mileage_km, 85000)
        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn(QUOTE_DISCLAIMER, reply)
        self.assertIn("Trade-in (equity)", reply)
        self.assertIsNotNone(meta)
        assert meta is not None
        self.assertTrue(meta["disclaimer_present"])
        self.assertGreater(float(meta["valor_compra"]), 0)
        self.assertEqual(meta["payment_method"], PAYMENT_TRADE_IN)

    def test_complete_voice_appointment_sets_handoff_flag(self):
        from src.lead_routing import complete_voice_appointment_handoff
        from src.odoo_sync.crm import RepAssignment, reset_round_robin

        reset_round_robin()
        self.addCleanup(reset_round_robin)
        odoo = MagicMock()
        odoo._resolve_crm_stage_id.return_value = 20
        odoo.assign_lead_advisor.return_value = True
        odoo.post_quote_to_chatter.return_value = 1
        wa = MagicMock()
        wa.send_text_message.return_value = {"ok": True}
        with patch.dict(
            os.environ,
            {
                "REPS_SAN_FELIPE": (
                    '[{"odoo_id": 21, "phone": "+526142417711", "name": "Francisco"}]'
                ),
                "REP_NOTIFY_ENABLED": "true",
            },
            clear=False,
        ), patch("src.alerts.send_alert") as send_alert:
            send_alert.return_value = MagicMock(
                sent=["slack"], failed=[], skipped_reason=None
            )
            result = complete_voice_appointment_handoff(
                lead_id=1937,
                client_phone="5216140001937",
                appointment_time="hoy a las 16",
                vehicle_of_interest="Camioneta",
                valuation_amount="194500.00",
                monthly_payment="9212.13",
                payment_method="financing_trade_in",
                branch="san_felipe",
                client_name="Marco",
                odoo=odoo,
                whatsapp_client=wa,
            )
        self.assertTrue(result.handoff_to_advisor)
        self.assertEqual(result.stage_name, STAGE_CITA)
        summary = (result.channel_alerts or {}).get("summary") or ""
        self.assertIn("Valor auto a cambio", summary)
        self.assertIn("Mensualidad", summary)
        self.assertIn("hoy a las 16", summary)
        sent_text = wa.send_text_message.call_args.args[1]
        self.assertIn("194500.00", sent_text)
        self.assertIn("9212.13", sent_text)


class TestAppointmentHandoff(unittest.TestCase):
    def setUp(self) -> None:
        reset_round_robin()
        self.addCleanup(reset_round_robin)
        patcher = patch.dict(
            os.environ,
            {
                "REPS_SAN_FELIPE": (
                    '[{"odoo_id": 21, "phone": "+526142417711", "name": "Francisco"}]'
                ),
                "REP_NOTIFY_ENABLED": "true",
            },
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_handoff_updates_stage_assigns_and_notifies(self):
        odoo = MagicMock()
        odoo._resolve_crm_stage_id.return_value = 77
        odoo.assign_lead_advisor.return_value = True
        odoo.post_quote_to_chatter.return_value = 1
        wa = MagicMock()
        wa.send_text_message.return_value = {"ok": True}
        intent = detect_appointment_intent("cita mañana a las 11")

        result = handoff_appointment_to_rep(
            lead_id=1937,
            client_phone="+52614XXXXXXX",
            branch="san_felipe",
            vehicle_interest="Toyota Corolla 2020 trade-in / camioneta",
            appointment=intent,
            client_name="Marco Gastelum",
            odoo=odoo,
            whatsapp_client=wa,
        )

        self.assertEqual(result.stage_name, STAGE_CITA)
        self.assertTrue(result.stage_updated)
        self.assertTrue(result.advisor_assigned)
        odoo.execute_kw.assert_called()
        write_args = odoo.execute_kw.call_args.args
        self.assertEqual(write_args[0], "crm.lead")
        self.assertEqual(write_args[1], "write")
        self.assertEqual(write_args[2][0], [1937])
        self.assertEqual(write_args[2][1]["stage_id"], 77)
        odoo.assign_lead_advisor.assert_called_once_with(1937, 21)
        self.assertTrue(result.rep_notification["sent"])
        sent_text = wa.send_text_message.call_args.args[1]
        self.assertIn("Cita solicitada", sent_text)
        self.assertIn("mañana", sent_text.lower())

    def test_explicit_assignment_used(self):
        wa = MagicMock()
        wa.send_text_message.return_value = {"ok": True}
        pick = RepAssignment(branch="san_felipe", phone="+526149999999", odoo_id=99)

        result = handoff_appointment_to_rep(
            lead_id=None,
            client_phone="6141111111",
            branch="san_felipe",
            whatsapp_client=wa,
            assigner_assignment=pick,
        )

        self.assertEqual(result.assignment.odoo_id, 99)
        self.assertTrue(result.rep_notification["sent"])


if __name__ == "__main__":
    unittest.main()
