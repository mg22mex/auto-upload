"""Sales-rep WhatsApp handoff notification (message payload + hooks)."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import (  # noqa: E402
    ENV_DEFAULT_REP_PHONE,
    ENV_REPS_PERIFERICO,
    ENV_REPS_SAN_FELIPE,
)
from src.notifications.whatsapp_rep import (  # noqa: E402
    ENV_ENABLED,
    format_rep_notification,
    notify_rep,
    odoo_lead_url,
    payment_label,
    rep_notifications_enabled,
)
from src.odoo_sync.crm import RepAssignment, reset_round_robin  # noqa: E402
from src.whatsapp_worker.inbound import (  # noqa: E402
    QualificationSession,
    QualificationTurnResult,
    notify_rep_on_handoff,
)

_REP_ENV = (
    ENV_REPS_PERIFERICO,
    ENV_REPS_SAN_FELIPE,
    ENV_DEFAULT_REP_PHONE,
    ENV_ENABLED,
    "ODOO_URL",
)


class FakeWhatsApp:
    def __init__(self, fail: Exception | None = None) -> None:
        self.sent: list[dict] = []
        self.fail = fail

    def send_text_message(self, phone, text, *, branch=None, instance=None):
        if self.fail:
            raise self.fail
        self.sent.append({"phone": phone, "text": text, "branch": branch})
        return {"ok": True}


class RepNotifyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in _REP_ENV:
            os.environ.pop(key, None)
        reset_round_robin()
        self.addCleanup(reset_round_robin)
        self.client = FakeWhatsApp()


class TestMessageFormat(RepNotifyTestCase):
    def test_message_contains_every_required_field(self):
        text = format_rep_notification(
            client_phone="+526145550000",
            vehicle_interest="2021 Mazda CX-30",
            payment_method="financing",
            branch_name="San Felipe",
            lead_url="https://odoo.example/web#id=42&model=crm.lead&view_type=form",
        )

        self.assertIn("¡Nuevo Lead Asignado!", text)
        self.assertIn("*Cliente:* +526145550000", text)
        self.assertIn("*Auto:* 2021 Mazda CX-30", text)
        self.assertIn("*Modalidad:* Financiamiento", text)
        self.assertIn("*Sucursal:* San Felipe", text)
        self.assertIn("id=42&model=crm.lead", text)

    def test_message_uses_placeholders_when_data_missing(self):
        text = format_rep_notification(client_phone="")

        self.assertIn("*Cliente:* n/d", text)
        self.assertIn("*Auto:* Por confirmar", text)
        self.assertIn("*Modalidad:* Por definir", text)
        self.assertIn("*Sucursal:* Periférico", text)

    def test_payment_labels(self):
        self.assertEqual(payment_label("cash"), "Contado")
        self.assertEqual(payment_label("trade_in"), "Auto a cambio")
        self.assertEqual(
            payment_label("financing_trade_in"), "Financiamiento + Auto a cambio"
        )
        self.assertEqual(payment_label(None), "Por definir")

    def test_odoo_lead_url_requires_base_and_id(self):
        self.assertEqual(odoo_lead_url(7), "")

        os.environ["ODOO_URL"] = "https://autosell.odoo.com/"

        self.assertEqual(
            odoo_lead_url(7),
            "https://autosell.odoo.com/web#id=7&model=crm.lead&view_type=form",
        )
        self.assertEqual(odoo_lead_url(None), "")


class TestNotifyRep(RepNotifyTestCase):
    def test_sends_to_rotated_rep(self):
        os.environ[ENV_REPS_PERIFERICO] = (
            '[{"odoo_id": 1, "phone": "+526141111111"},'
            ' {"odoo_id": 2, "phone": "+526142222222"}]'
        )

        first = notify_rep(
            client_phone="+526145550000", branch="periferico", whatsapp_client=self.client
        )
        second = notify_rep(
            client_phone="+526145550001", branch="periferico", whatsapp_client=self.client
        )

        self.assertTrue(first.sent)
        self.assertTrue(second.sent)
        self.assertEqual(
            [call["phone"] for call in self.client.sent],
            ["+526141111111", "+526142222222"],
        )
        self.assertEqual(first.odoo_id, 1)
        self.assertEqual(second.odoo_id, 2)

    def test_branch_tag_routes_to_san_felipe(self):
        os.environ[ENV_REPS_SAN_FELIPE] = '[{"odoo_id": 3, "phone": "+526143333333"}]'

        result = notify_rep(
            client_phone="+526145550000",
            tag="2018 Mercedes +",
            whatsapp_client=self.client,
        )

        self.assertEqual(result.branch, "san_felipe")
        self.assertEqual(self.client.sent[0]["phone"], "+526143333333")
        self.assertIn("San Felipe", self.client.sent[0]["text"])

    def test_falls_back_to_default_phone(self):
        os.environ[ENV_DEFAULT_REP_PHONE] = "+526149999999"

        result = notify_rep(
            client_phone="+526145550000", branch="san_felipe", whatsapp_client=self.client
        )

        self.assertTrue(result.sent)
        self.assertIsNone(result.odoo_id)
        self.assertEqual(self.client.sent[0]["phone"], "+526149999999")

    def test_skips_when_nothing_configured(self):
        result = notify_rep(client_phone="+526145550000", whatsapp_client=self.client)

        self.assertFalse(result.sent)
        self.assertEqual(result.skipped_reason, "no rep phone configured for branch")
        self.assertEqual(self.client.sent, [])

    def test_disabled_by_env(self):
        os.environ.update(
            {ENV_ENABLED: "false", ENV_DEFAULT_REP_PHONE: "+526149999999"}
        )

        result = notify_rep(client_phone="+526145550000", whatsapp_client=self.client)

        self.assertFalse(result.sent)
        self.assertEqual(result.skipped_reason, f"{ENV_ENABLED}=false")
        self.assertEqual(self.client.sent, [])

    def test_swallows_transport_errors(self):
        os.environ[ENV_DEFAULT_REP_PHONE] = "+526149999999"
        client = FakeWhatsApp(fail=RuntimeError("evolution 502"))

        result = notify_rep(client_phone="+526145550000", whatsapp_client=client)

        self.assertFalse(result.sent)
        self.assertEqual(result.error, "evolution 502")

    def test_explicit_assignment_bypasses_rotation(self):
        pick = RepAssignment(branch="san_felipe", phone="+526144444444", odoo_id=8)

        result = notify_rep(
            client_phone="+526145550000",
            assignment=pick,
            whatsapp_client=self.client,
            lead_url="https://odoo.example/lead/8",
        )

        self.assertTrue(result.sent)
        self.assertEqual(self.client.sent[0]["phone"], "+526144444444")
        self.assertIn("https://odoo.example/lead/8", self.client.sent[0]["text"])

    def test_builds_default_client_when_none_injected(self):
        os.environ[ENV_DEFAULT_REP_PHONE] = "+526149999999"
        created: list[str] = []

        class _Client:
            def __init__(self) -> None:
                created.append("built")

            def send_text_message(self, *args, **kwargs):
                return {"ok": True}

        with patch("src.whatsapp_worker.client.WhatsAppWorkerClient", _Client):
            result = notify_rep(client_phone="+526145550000")

        self.assertTrue(result.sent)
        self.assertEqual(created, ["built"])
        self.assertTrue(rep_notifications_enabled())


class TestHandoffHook(RepNotifyTestCase):
    @staticmethod
    def _turn(*, handoff: bool) -> QualificationTurnResult:
        session = QualificationSession(
            phone="+526145550000",
            instance="autosell_periferico",
            state="HANDOFF_TO_HUMAN" if handoff else "AWAITING_PAYMENT_METHOD",
            branch="periferico",
            initial_message="Me interesa el Mazda CX-30",
            payment_method="cash" if handoff else "",
            lead_id=55 if handoff else None,
        )
        return QualificationTurnResult(
            session=session, reply_text="ok", odoo_handoff=handoff
        )

    def test_handoff_turn_notifies_rep(self):
        os.environ[ENV_REPS_PERIFERICO] = '[{"odoo_id": 1, "phone": "+526141111111"}]'

        notice = notify_rep_on_handoff(
            self._turn(handoff=True), whatsapp_client=self.client
        )

        self.assertIsNotNone(notice)
        self.assertTrue(notice["sent"])
        self.assertIn("Mazda CX-30", self.client.sent[0]["text"])
        self.assertIn("*Modalidad:* Contado", self.client.sent[0]["text"])

    def test_non_handoff_turn_does_not_notify(self):
        os.environ[ENV_REPS_PERIFERICO] = '[{"odoo_id": 1, "phone": "+526141111111"}]'

        notice = notify_rep_on_handoff(
            self._turn(handoff=False), whatsapp_client=self.client
        )

        self.assertIsNone(notice)
        self.assertEqual(self.client.sent, [])


if __name__ == "__main__":
    unittest.main()
