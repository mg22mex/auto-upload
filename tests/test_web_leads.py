"""Unit tests — webform email parse + Beatriz outreach copy."""
from __future__ import annotations

import email
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.web_leads.imap_poll import already_processed, mark_processed
from src.web_leads.models import WebLead
from src.web_leads.parser import parse_email_message, parse_web_lead_email
from src.web_leads.pipeline import (
    format_beatriz_web_lead_message,
    ingest_web_lead,
)


SAMPLE_BODY = """
Nuevo contacto desde autosell.mx

Nombre: Juan Pérez
Teléfono: 614 123 4567
Email: juan.perez@gmail.com
Vehículo: Mazda CX-5 2020
Sucursal: San Felipe
Mensaje: Me interesa financiamiento
"""


class TestParseWebLead(unittest.TestCase):
    def test_labeled_spanish_body(self):
        lead = parse_web_lead_email(
            subject="Solicitud web Mazda CX-5",
            body=SAMPLE_BODY,
            message_id="<abc@autosell.mx>",
        )
        self.assertEqual(lead.name, "Juan Pérez")
        self.assertTrue(lead.phone.endswith("6141234567"))
        self.assertEqual(lead.email, "juan.perez@gmail.com")
        self.assertIn("CX-5", lead.vehicle)
        self.assertEqual(lead.branch, "san_felipe")
        self.assertTrue(lead.ok)

    def test_html_table_body(self):
        html = """
        <html><body>
        <p>Nombre: Ana Lopez</p>
        <p>Telefono: +52 614-987-6543</p>
        <p>Vehiculo: Toyota Corolla</p>
        </body></html>
        """
        lead = parse_web_lead_email(subject="Lead", body=html)
        self.assertEqual(lead.name, "Ana Lopez")
        self.assertIn("6149876543", lead.phone)
        self.assertIn("Corolla", lead.vehicle)
        self.assertEqual(lead.branch, "periferico")

    def test_rfc822_roundtrip(self):
        msg = email.message_from_string(
            "From: forms@autosell.mx\n"
            "Subject: Interés en RAV4\n"
            "Message-ID: <web-1@test>\n"
            "Content-Type: text/plain; charset=utf-8\n"
            "\n"
            "Nombre: Carla\n"
            "Teléfono: 6145551212\n"
            "Sucursal: Periférico\n"
        )
        lead = parse_email_message(msg)
        self.assertEqual(lead.name, "Carla")
        self.assertEqual(lead.message_id, "<web-1@test>")
        self.assertEqual(lead.branch, "periferico")

    def test_beatriz_copy(self):
        text = format_beatriz_web_lead_message(
            WebLead(name="Marco Gastelum", phone="5216140000000", vehicle="Corolla 2020")
        )
        self.assertIn("Hola Marco", text)
        self.assertIn("Autosell.mx", text)
        self.assertIn("Corolla 2020", text)
        self.assertIn("Beatriz", text)
        self.assertIn("financiamiento", text)


class TestIngestDryRun(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        os.environ["WEB_LEADS_SEEN_DB"] = str(
            Path(self._tmpdir.name) / "seen.db"
        )
        self.addCleanup(self._tmpdir.cleanup)
        self.addCleanup(lambda: os.environ.pop("WEB_LEADS_SEEN_DB", None))

    def test_dry_run_does_not_call_crm(self):
        lead = parse_web_lead_email(subject="x", body=SAMPLE_BODY, message_id="<d1>")
        with patch("src.web_leads.pipeline.CRMLeadManager") as mgr_cls:
            result = ingest_web_lead(lead, dry_run=True)
            mgr_cls.assert_not_called()
        self.assertEqual(result.status, "dry_run")
        self.assertIn("Hola Juan", (result.customer_wa or {}).get("message", ""))

    def test_dedupe_message_id(self):
        mark_processed("<dup@test>", lead_id=1, phone="5216141234567")
        self.assertTrue(already_processed("<dup@test>"))
        lead = WebLead(
            name="X", phone="5216141234567", message_id="<dup@test>"
        )
        result = ingest_web_lead(lead, dry_run=True)
        self.assertEqual(result.status, "skipped")

    def test_live_ingest_mocks(self):
        lead = parse_web_lead_email(
            subject="x", body=SAMPLE_BODY, message_id="<live-1>"
        )
        crm = MagicMock()
        crm.create_or_update_lead.return_value = {
            "status": "created",
            "lead_id": 99,
        }
        crm._client = MagicMock()
        crm._client.assign_lead_advisor.return_value = True
        wa = MagicMock()
        with patch(
            "src.notifications.whatsapp.send_whatsapp_message",
            return_value={"ok": True},
        ) as send_cust, patch(
            "src.notifications.whatsapp_rep.notify_rep",
        ) as notify, patch(
            "src.web_leads.pipeline.ensure_customer_partner",
            create=True,
        ), patch(
            "src.odoo_sync.appointment_sync.ensure_customer_partner",
            return_value=55,
        ), patch(
            "src.odoo_sync.structure.setter_user_id_from_mapping",
            return_value=18,
        ):
            notify.return_value = MagicMock(
                as_dict=lambda: {"sent": True, "phone": "+526142417711"}
            )
            result = ingest_web_lead(
                lead, crm=crm, whatsapp_client=wa, dry_run=False
            )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.lead_id, 99)
        self.assertEqual(result.partner_id, 55)
        send_cust.assert_called_once()
        notify.assert_called_once()
        args = crm.create_or_update_lead.call_args
        payload = args.args[0]
        self.assertEqual(args.kwargs.get("branch") or args.args[1], "san_felipe")
        self.assertEqual(payload["channel"], "Website")
        self.assertFalse(payload["assign_round_robin"])
        self.assertIn("Nuevo", payload["stage_name"])
        crm._client.assign_lead_advisor.assert_called()


if __name__ == "__main__":
    unittest.main()
