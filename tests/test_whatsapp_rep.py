"""Sales-rep WhatsApp handoff notification (message payload + hooks)."""
from __future__ import annotations

import pytest

from src.config import ENV_DEFAULT_REP_PHONE, ENV_REPS_PERIFERICO, ENV_REPS_SAN_FELIPE
from src.notifications import whatsapp_rep
from src.notifications.whatsapp_rep import (
    ENV_ENABLED,
    format_rep_notification,
    notify_rep,
    odoo_lead_url,
    payment_label,
)
from src.odoo_sync.crm import RepAssignment, reset_round_robin
from src.whatsapp_worker.inbound import (
    QualificationSession,
    QualificationTurnResult,
    notify_rep_on_handoff,
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


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in (
        ENV_REPS_PERIFERICO,
        ENV_REPS_SAN_FELIPE,
        ENV_DEFAULT_REP_PHONE,
        ENV_ENABLED,
        "ODOO_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    reset_round_robin()
    yield
    reset_round_robin()


def test_message_contains_every_required_field():
    text = format_rep_notification(
        client_phone="+526145550000",
        vehicle_interest="2021 Mazda CX-30",
        payment_method="financing",
        branch_name="San Felipe",
        lead_url="https://odoo.example/web#id=42&model=crm.lead&view_type=form",
    )

    assert "¡Nuevo Lead Asignado!" in text
    assert "*Cliente:* +526145550000" in text
    assert "*Auto:* 2021 Mazda CX-30" in text
    assert "*Modalidad:* Financiamiento" in text
    assert "*Sucursal:* San Felipe" in text
    assert "id=42&model=crm.lead" in text


def test_message_uses_placeholders_when_data_missing():
    text = format_rep_notification(client_phone="")

    assert "*Cliente:* n/d" in text
    assert "*Auto:* Por confirmar" in text
    assert "*Modalidad:* Por definir" in text
    assert "*Sucursal:* Periférico" in text


def test_payment_labels():
    assert payment_label("cash") == "Contado"
    assert payment_label("trade_in") == "Auto a cuenta"
    assert payment_label(None) == "Por definir"


def test_odoo_lead_url_requires_base_and_id(monkeypatch):
    assert odoo_lead_url(7) == ""
    monkeypatch.setenv("ODOO_URL", "https://autosell.odoo.com/")
    assert odoo_lead_url(7) == "https://autosell.odoo.com/web#id=7&model=crm.lead&view_type=form"
    assert odoo_lead_url(None) == ""


def test_notify_rep_sends_to_rotated_rep(monkeypatch):
    monkeypatch.setenv(
        ENV_REPS_PERIFERICO,
        '[{"odoo_id": 1, "phone": "+526141111111"},'
        ' {"odoo_id": 2, "phone": "+526142222222"}]',
    )
    client = FakeWhatsApp()

    first = notify_rep(client_phone="+526145550000", branch="periferico", whatsapp_client=client)
    second = notify_rep(client_phone="+526145550001", branch="periferico", whatsapp_client=client)

    assert first.sent and second.sent
    assert [call["phone"] for call in client.sent] == ["+526141111111", "+526142222222"]
    assert first.odoo_id == 1
    assert second.odoo_id == 2


def test_notify_rep_uses_branch_tag(monkeypatch):
    monkeypatch.setenv(ENV_REPS_SAN_FELIPE, '[{"odoo_id": 3, "phone": "+526143333333"}]')
    client = FakeWhatsApp()

    result = notify_rep(client_phone="+526145550000", tag="2018 Mercedes +", whatsapp_client=client)

    assert result.branch == "san_felipe"
    assert client.sent[0]["phone"] == "+526143333333"
    assert "San Felipe" in client.sent[0]["text"]


def test_notify_rep_falls_back_to_default_phone(monkeypatch):
    monkeypatch.setenv(ENV_DEFAULT_REP_PHONE, "+526149999999")
    client = FakeWhatsApp()

    result = notify_rep(client_phone="+526145550000", branch="san_felipe", whatsapp_client=client)

    assert result.sent
    assert result.odoo_id is None
    assert client.sent[0]["phone"] == "+526149999999"


def test_notify_rep_skips_when_nothing_configured():
    client = FakeWhatsApp()

    result = notify_rep(client_phone="+526145550000", whatsapp_client=client)

    assert not result.sent
    assert result.skipped_reason == "no rep phone configured for branch"
    assert client.sent == []


def test_notify_rep_disabled_by_env(monkeypatch):
    monkeypatch.setenv(ENV_ENABLED, "false")
    monkeypatch.setenv(ENV_DEFAULT_REP_PHONE, "+526149999999")
    client = FakeWhatsApp()

    result = notify_rep(client_phone="+526145550000", whatsapp_client=client)

    assert not result.sent
    assert result.skipped_reason == f"{ENV_ENABLED}=false"
    assert client.sent == []


def test_notify_rep_swallows_transport_errors(monkeypatch):
    monkeypatch.setenv(ENV_DEFAULT_REP_PHONE, "+526149999999")
    client = FakeWhatsApp(fail=RuntimeError("evolution 502"))

    result = notify_rep(client_phone="+526145550000", whatsapp_client=client)

    assert not result.sent
    assert result.error == "evolution 502"


def test_explicit_assignment_bypasses_rotation():
    client = FakeWhatsApp()
    pick = RepAssignment(branch="san_felipe", phone="+526144444444", odoo_id=8)

    result = notify_rep(
        client_phone="+526145550000",
        assignment=pick,
        whatsapp_client=client,
        lead_url="https://odoo.example/lead/8",
    )

    assert result.sent
    assert client.sent[0]["phone"] == "+526144444444"
    assert "https://odoo.example/lead/8" in client.sent[0]["text"]


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


def test_handoff_hook_notifies_rep(monkeypatch):
    monkeypatch.setenv(ENV_REPS_PERIFERICO, '[{"odoo_id": 1, "phone": "+526141111111"}]')
    client = FakeWhatsApp()

    notice = notify_rep_on_handoff(_turn(handoff=True), whatsapp_client=client)

    assert notice is not None and notice["sent"]
    assert "Mazda CX-30" in client.sent[0]["text"]
    assert "*Modalidad:* Contado" in client.sent[0]["text"]


def test_non_handoff_turn_does_not_notify(monkeypatch):
    monkeypatch.setenv(ENV_REPS_PERIFERICO, '[{"odoo_id": 1, "phone": "+526141111111"}]')
    client = FakeWhatsApp()

    assert notify_rep_on_handoff(_turn(handoff=False), whatsapp_client=client) is None
    assert client.sent == []


def test_notify_rep_creates_default_client_only_when_needed(monkeypatch):
    monkeypatch.setenv(ENV_DEFAULT_REP_PHONE, "+526149999999")
    created: list[str] = []

    class _Client:
        def __init__(self) -> None:
            created.append("built")

        def send_text_message(self, *a, **k):
            return {"ok": True}

    monkeypatch.setattr(
        "src.whatsapp_worker.client.WhatsAppWorkerClient", _Client, raising=True
    )

    assert notify_rep(client_phone="+526145550000").sent
    assert created == ["built"]
    assert whatsapp_rep.rep_notifications_enabled()
