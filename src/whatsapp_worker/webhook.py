"""Standalone inbound WhatsApp receiver (Evolution API / open-wa).

Runs as its own process, separate from the Facebook Playwright workers:

    uvicorn src.whatsapp_worker.webhook:app --host 0.0.0.0 --port 8081

The voice gateway exposes an equivalent ``/webhook/whatsapp`` route for
single-process deployments; both share the parsing, qualification, Odoo and
rep-notification code in ``src.whatsapp_worker.inbound``.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*_a: Any, **_k: Any) -> bool:
        return False

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env")

from src.whatsapp_worker.client import WhatsAppWorkerClient
from src.whatsapp_worker.inbound import (
    QualificationStore,
    WhatsAppInboundEvent,
    apply_qualification_to_odoo,
    notify_rep_on_handoff,
    parse_evolution_inbound,
    process_qualification_turn,
)
from src.whatsapp_worker.routing import branch_context_for_instance

logger = logging.getLogger(__name__)


def handle_inbound_event(
    event: WhatsAppInboundEvent,
    *,
    store: QualificationStore,
    odoo: Any,
    whatsapp: Any,
) -> dict[str, Any]:
    """Qualify one inbound message: reply, upsert CRM, alert rep on handoff."""
    branch_ctx = branch_context_for_instance(event.instance)
    turn = process_qualification_turn(
        event,
        store.get(event.phone, event.instance),
        branch=branch_ctx["branch"],
        branch_id=branch_ctx.get("branch_id"),
        physical_location=branch_ctx["physical_location"],
    )

    lead_id = apply_qualification_to_odoo(odoo, event, turn)
    rep_notice = notify_rep_on_handoff(turn, whatsapp_client=whatsapp)

    reply_sent = False
    reply_error: str | None = None
    try:
        whatsapp.send_text_message(
            event.phone,
            turn.reply_text,
            instance=event.instance or None,
            branch=turn.session.branch,
        )
        reply_sent = True
    except Exception as exc:
        reply_error = str(exc)

    store.save(turn.session)
    return {
        "status": "ok",
        "phone": event.phone,
        "instance": event.instance,
        "branch": turn.session.branch,
        "branch_id": turn.session.branch_id,
        "lead_id": lead_id,
        "qualification_state": turn.session.state,
        "auto_reply_sent": reply_sent,
        "auto_reply_error": reply_error,
        "rep_notification": rep_notice,
        "error": None,
    }


def handle_inbound_payload(
    payload: dict[str, Any],
    *,
    store: QualificationStore,
    odoo: Any,
    whatsapp: Any,
) -> dict[str, Any]:
    """Process a full Evolution webhook body (may carry several messages)."""
    events = parse_evolution_inbound(payload)
    results: list[dict[str, Any]] = []
    for event in events:
        try:
            results.append(
                handle_inbound_event(event, store=store, odoo=odoo, whatsapp=whatsapp)
            )
        except Exception as exc:
            logger.exception("WhatsApp inbound failed for %s", event.phone)
            results.append({"status": "error", "phone": event.phone, "error": str(exc)})
    return {
        "status": "event_received",
        "processed": len(events),
        "ignored": 0 if events else 1,
        "results": results,
    }


def create_app(
    *,
    qualification_store: QualificationStore | None = None,
    odoo_client: Any | None = None,
    whatsapp_client: Any | None = None,
    odoo_factory: Callable[[], Any] | None = None,
) -> FastAPI:
    """Build the worker app. Inject collaborators for tests."""
    app = FastAPI(title="Autosell WhatsApp Worker", version="0.1.0")
    state: dict[str, Any] = {
        "qualification_store": qualification_store,
        "odoo_client": odoo_client,
        "whatsapp_client": whatsapp_client,
        "odoo_factory": odoo_factory,
    }

    def _store() -> QualificationStore:
        if state["qualification_store"] is None:
            state["qualification_store"] = QualificationStore()
        return state["qualification_store"]

    def _odoo() -> Any:
        if state["odoo_client"] is None:
            factory = state["odoo_factory"]
            if factory is None:
                from src.odoo_sync.client import OdooCRMClient

                factory = OdooCRMClient
            state["odoo_client"] = factory()
        return state["odoo_client"]

    def _whatsapp() -> Any:
        if state["whatsapp_client"] is None:
            state["whatsapp_client"] = WhatsAppWorkerClient()
        return state["whatsapp_client"]

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "worker": "whatsapp"}

    @app.post("/webhook/whatsapp")
    async def whatsapp_webhook(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="payload must be a JSON object")
        body = handle_inbound_payload(
            payload, store=_store(), odoo=_odoo(), whatsapp=_whatsapp()
        )
        return JSONResponse(status_code=200, content=body)

    app.state.worker = state  # type: ignore[attr-defined]
    return app


app = create_app()

__all__ = ["app", "create_app", "handle_inbound_event", "handle_inbound_payload"]
