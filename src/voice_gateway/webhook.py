"""Voice AI webhook — quote pipeline + Meta Messenger routes."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Callable

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*_a: Any, **_k: Any) -> bool:
        return False

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

# Load repo .env before Odoo / Meta clients read os.environ.
_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env")

from src.odoo_sync.client import OdooCRMClient
from src.odoo_sync.crm import CRMLeadManager
from src.meta_gateway.gateway import MetaWebhookGateway, parse_messenger_events
from src.whatsapp_worker.client import WhatsAppWorkerClient
from src.voice_gateway.intent import (
    VOICE_CHANNEL,
    VoiceIntent,
    format_tts_quote,
    parse_voice_intent,
)
from src.whatsapp_worker.routing import (
    apply_whatsapp_branch_context,
    branch_context_for_instance,
)
from src.whatsapp_worker.inbound import (
    WA_CHANNEL,
    QualificationStore,
    QualificationTurnResult,
    inbound_to_voice_payload,
    parse_evolution_inbound,
    process_qualification_turn,
    qualification_enabled,
)
from src.pipeline import AutosellPipeline, PipelineResult
from src.voice_gateway.inbound_call import (
    branch_context_for_inbound_call,
    build_inbound_call_response,
    parse_inbound_call_payload,
    parse_inbound_call_request,
)

logger = logging.getLogger(__name__)


def parse_voice_lead_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Map Voice AI JSON → AutosellPipeline lead_data.

    Raises ``ValueError`` when the payload cannot produce a usable lead.
    """
    intent = parse_voice_intent(payload)
    if intent.mode == "transfer" and not intent.ok:
        raise ValueError(
            "; ".join(intent.errors) if intent.errors else "invalid voice payload"
        )

    lead = intent.to_lead_data()
    branch = intent.branch_id
    if branch is None:
        branch = int(os.getenv("VOICE_DEFAULT_BRANCH_ID") or "1")
    lead["branch_id"] = branch
    channel = str(payload.get("channel") or "").strip()
    lead["channel"] = channel if channel else VOICE_CHANNEL

    # Preserve strict price requirement for legacy structured posts without
    # Odoo resolve: missing price only OK when inventory lookup is intended.
    if (
        intent.mode == "quote"
        and intent.vehicle_price is None
        and not payload.get("resolve_price_from_odoo", True)
    ):
        raise ValueError("vehicle_price is required (or vehicle_interest.price)")

    return lead


def _tts_from_result(intent: VoiceIntent, result: PipelineResult) -> str:
    if intent.tts_fallback:
        return intent.tts_fallback
    if result.ok and result.estimated_monthly_payment is not None:
        down = None
        for step in result.steps:
            if step.get("step") == "quote" and step.get("down_payment"):
                down = step["down_payment"]
                break
        return format_tts_quote(
            name=intent.caller_name,
            vehicle_name=intent.vehicle_name,
            monthly=result.estimated_monthly_payment,
            down_payment=down,
            term_months=intent.term_months,
        )
    if result.error:
        return (
            "Hubo un problema al calcular su cotización. "
            "Lo transfiero con un asesor de Autosell MX."
        )
    return intent.tts_fallback or (
        "Gracias por su llamada. Un asesor de Autosell MX le contactará pronto."
    )


def _response_body(
    *,
    intent: VoiceIntent,
    result: PipelineResult | None,
    confirmation: str,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "status": "error",
        "confirmation": confirmation,
        "channel": VOICE_CHANNEL,
        "mode": intent.mode,
        "audio_degraded": intent.audio_degraded,
        "tts_text": intent.tts_fallback or "",
        "lead_id": result.lead_id if result else None,
        "advisor_user_id": result.advisor_user_id if result else None,
        "estimated_monthly_payment": (
            str(result.estimated_monthly_payment)
            if result and result.estimated_monthly_payment is not None
            else None
        ),
        "net_trade_in_equity": (
            str(result.net_trade_in_equity) if result else "0.00"
        ),
        "pdf_path": result.pdf_path if result else None,
        "pdf_attachment_id": result.pdf_attachment_id if result else None,
        "vehicle_sku": result.vehicle_sku if result else (intent.sku or None),
        "steps": result.steps if result else [],
        "error": None,
    }
    if result is not None:
        body["status"] = "ok" if result.ok else "error"
        body["tts_text"] = _tts_from_result(intent, result)
        body["error"] = result.error
    elif intent.mode == "transfer":
        body["status"] = "fallback"
        body["error"] = "; ".join(intent.errors) if intent.errors else None
    else:
        body["error"] = "; ".join(intent.errors) if intent.errors else None
    return body


async def _run_pipeline(
    pipeline: AutosellPipeline,
    lead_data: dict[str, Any],
) -> PipelineResult:
    """Run sync pipeline off the event loop so FastAPI stays responsive."""
    return await asyncio.to_thread(pipeline.process_lead, lead_data)


def _apply_qualification_odoo(
    odoo: OdooCRMClient,
    event: Any,
    turn: QualificationTurnResult,
) -> int | None:
    """Create or update Odoo CRM lead for a qualification turn."""
    session = turn.session
    branch_id = int(session.branch_id or os.getenv("VOICE_DEFAULT_BRANCH_ID") or 1)
    odoo.authenticate()
    if turn.odoo_create:
        note = f"WhatsApp inbound: {session.initial_message or event.text}"
        lead_result = odoo.create_or_update_lead(
            session.contact_name or event.name,
            event.phone,
            session.initial_message or event.text,
            branch_id,
            quote_summary=note,
            stage_name="New",
            channel=WA_CHANNEL,
            schedule_follow_up=True,
        )
        session.lead_id = lead_result.lead_id
        return lead_result.lead_id
    if turn.odoo_handoff:
        lead_result = odoo.create_or_update_lead(
            session.contact_name or event.name,
            event.phone,
            session.initial_message or event.text,
            branch_id,
            quote_summary=turn.odoo_notes,
            stage_name="New",
            channel=WA_CHANNEL,
            schedule_follow_up=True,
        )
        session.lead_id = lead_result.lead_id
        return lead_result.lead_id
    return session.lead_id


async def _handle_whatsapp_qualification(
    event: Any,
    *,
    store: QualificationStore,
    odoo: OdooCRMClient,
    whatsapp: WhatsAppWorkerClient,
) -> dict[str, Any]:
    branch_ctx = branch_context_for_instance(event.instance)
    session = store.get(event.phone, event.instance)
    turn = process_qualification_turn(
        event,
        session,
        branch=branch_ctx["branch"],
        branch_id=branch_ctx.get("branch_id"),
        physical_location=branch_ctx["physical_location"],
    )
    lead_id = await asyncio.to_thread(_apply_qualification_odoo, odoo, event, turn)
    reply_error: str | None = None
    reply_sent = False
    try:
        await asyncio.to_thread(
            whatsapp.send_text_message,
            event.phone,
            turn.reply_text,
            instance=event.instance or None,
            branch=turn.session.branch,
        )
        reply_sent = True
    except Exception as exc:
        reply_error = str(exc)
    store.save(turn.session)
    logger.info(
        "WhatsApp qualification %s instance=%s state=%s branch=%s team=%s "
        "lead=%s reply=%s",
        event.phone,
        event.instance,
        turn.session.state,
        turn.session.branch,
        turn.session.branch_id,
        lead_id,
        reply_sent,
    )
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
        "error": None,
    }


def create_app(
    pipeline: AutosellPipeline | None = None,
    pipeline_factory: Callable[[], AutosellPipeline] | None = None,
    meta_gateway: MetaWebhookGateway | None = None,
    meta_gateway_factory: Callable[[], MetaWebhookGateway] | None = None,
    qualification_store: QualificationStore | None = None,
    odoo_client: OdooCRMClient | None = None,
    crm_manager: CRMLeadManager | None = None,
    whatsapp_client: WhatsAppWorkerClient | None = None,
) -> FastAPI:
    """Build FastAPI app. Inject pipeline for tests."""
    app = FastAPI(title="Autosell Voice Gateway", version="0.2.0")
    state: dict[str, Any] = {
        "pipeline": pipeline,
        "pipeline_factory": pipeline_factory or AutosellPipeline,
        "meta_gateway": meta_gateway,
        "meta_gateway_factory": meta_gateway_factory or MetaWebhookGateway,
        "qualification_store": qualification_store,
        "odoo_client": odoo_client,
        "crm_manager": crm_manager,
        "whatsapp_client": whatsapp_client,
    }

    def _get_pipeline() -> AutosellPipeline:
        if state["pipeline"] is not None:
            return state["pipeline"]
        return state["pipeline_factory"]()

    def _get_meta_gateway() -> MetaWebhookGateway:
        if state["meta_gateway"] is not None:
            return state["meta_gateway"]
        return state["meta_gateway_factory"]()

    def _get_qualification_store() -> QualificationStore:
        if state["qualification_store"] is not None:
            return state["qualification_store"]
        return QualificationStore()

    def _get_odoo_client() -> OdooCRMClient:
        if state["odoo_client"] is not None:
            return state["odoo_client"]
        return OdooCRMClient()

    def _get_crm_manager() -> CRMLeadManager:
        if state["crm_manager"] is not None:
            return state["crm_manager"]
        return CRMLeadManager(client=_get_odoo_client())

    def _get_whatsapp_client() -> WhatsAppWorkerClient:
        if state["whatsapp_client"] is not None:
            return state["whatsapp_client"]
        return WhatsAppWorkerClient()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    async def _handle_voice(payload: dict[str, Any]) -> JSONResponse:
        intent = parse_voice_intent(payload)

        if intent.mode == "transfer" and not intent.ok:
            body = _response_body(
                intent=intent,
                result=None,
                confirmation="voice transfer prompted",
            )
            # Hard validation failures (no phone/name) → 422 for structured clients.
            if not intent.caller_phone or not intent.caller_name:
                raise HTTPException(
                    status_code=422,
                    detail="; ".join(intent.errors) or "invalid voice payload",
                )
            return JSONResponse(status_code=200, content=body)

        try:
            lead_data = parse_voice_lead_payload(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        # Soft capture after degraded STT still upserts a CRM lead (no PDF/quote).
        if intent.mode == "generic_capture":
            lead_data["soft_capture"] = True
            lead_data["generate_pdf"] = False
            lead_data["dispatch_whatsapp"] = False

        result = await _run_pipeline(_get_pipeline(), lead_data)

        sku = result.vehicle_sku or intent.sku or lead_data.get("vehicle_name") or "n/a"
        phone = intent.caller_phone or lead_data.get("phone") or "n/a"
        lead_id = result.lead_id if result.lead_id is not None else "n/a"
        logger.info(
            "Processed voice quote for caller %s / vehicle %s -> Lead ID %s",
            phone,
            sku,
            lead_id,
        )
        # Also print for operator consoles that don't wire logging.
        print(
            f"Processed voice quote for caller {phone} / vehicle {sku} "
            f"-> Lead ID {lead_id}"
        )

        confirmation = (
            "voice lead processed"
            if result.ok
            else (
                "voice lead fallback"
                if intent.audio_degraded
                else "voice lead failed"
            )
        )
        if intent.mode == "generic_capture" and result.ok:
            confirmation = "voice lead captured (degraded audio)"

        body = _response_body(
            intent=intent,
            result=result,
            confirmation=confirmation,
        )
        # Always HTTP 200 JSON confirmation per contract (errors in body).
        return JSONResponse(status_code=200, content=body)

    @app.post("/webhook/voice-lead")
    async def voice_lead(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="payload must be a JSON object")
        return await _handle_voice(payload)

    @app.post("/voice/webhook")
    async def voice_webhook_alias(request: Request) -> JSONResponse:
        """Alias for Voice AI platforms expecting /voice/webhook."""
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="payload must be a JSON object")
        return await _handle_voice(payload)

    @app.post("/voice/stream")
    async def voice_stream(request: Request) -> JSONResponse:
        """STT / partial utterance endpoint — same pipeline, TTS-first response."""
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="payload must be a JSON object")
        # Prefer transcript field names used by streaming STT gateways.
        if "utterance" in payload and "transcript" not in payload:
            payload = {**payload, "transcript": payload["utterance"]}
        return await _handle_voice(payload)

    @app.post("/voice/inbound")
    async def voice_inbound(request: Request) -> Response:
        """Inbound VoIP/SIP call webhook — CRM log + TwiML/JSON forward."""
        raw = await parse_inbound_call_request(request)
        try:
            event = parse_inbound_call_payload(raw)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        ctx = branch_context_for_inbound_call(event)
        crm = _get_crm_manager()
        result = await asyncio.to_thread(
            crm.log_inbound_call,
            caller_phone=event.caller_phone,
            branch=str(ctx["branch"]),
            caller_name=event.caller_name,
            called_number=event.called_number or None,
            call_sid=event.call_sid or None,
            call_status=event.call_status or None,
            duration_sec=event.duration_sec,
        )
        logger.info(
            "Inbound call %s → %s branch=%s team=%s lead=%s activity=%s",
            event.caller_phone,
            event.called_number or "n/a",
            ctx.get("branch"),
            ctx.get("branch_id"),
            result.get("lead_id"),
            result.get("activity_id"),
        )
        return build_inbound_call_response(
            request,
            ctx=ctx,
            crm_result=result,
            raw=raw,
        )

    @app.get("/webhook/facebook", response_class=PlainTextResponse)
    def verify_facebook_webhook(request: Request) -> PlainTextResponse:
        mode = request.query_params.get("hub.mode", "")
        token = request.query_params.get("hub.verify_token", "")
        challenge = request.query_params.get("hub.challenge", "")
        if not _get_meta_gateway().verify(mode, token):
            raise HTTPException(status_code=403, detail="invalid Meta verify token")
        return PlainTextResponse(content=challenge, status_code=200)

    @app.post("/webhook/facebook")
    async def facebook_webhook(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
            events = parse_messenger_events(payload)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"invalid Meta payload: {exc}") from exc

        gateway = _get_meta_gateway()
        results: list[dict[str, Any]] = []
        for event in events:
            try:
                results.append(gateway.process_event(event))
            except Exception as exc:
                # Acknowledge Meta to avoid a retry storm; retain per-event failure.
                results.append(
                    {
                        "status": "error",
                        "sender_id": event.sender_id,
                        "error": str(exc),
                    }
                )
        return JSONResponse(
            status_code=200,
            content={"status": "event_received", "processed": len(events), "results": results},
        )

    @app.post("/webhook/whatsapp")
    async def whatsapp_webhook(request: Request) -> JSONResponse:
        """Inbound Evolution API events (messages.upsert → quote pipeline)."""
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="payload must be a JSON object")

        inbound = parse_evolution_inbound(payload)
        results: list[dict[str, Any]] = []
        for event in inbound:
            try:
                if qualification_enabled():
                    result_body = await _handle_whatsapp_qualification(
                        event,
                        store=_get_qualification_store(),
                        odoo=_get_odoo_client(),
                        whatsapp=_get_whatsapp_client(),
                    )
                    results.append(result_body)
                    continue

                voice_payload = inbound_to_voice_payload(event)
                intent = parse_voice_intent(voice_payload)
                try:
                    lead_data = parse_voice_lead_payload(voice_payload)
                except ValueError:
                    lead_data = {
                        "name": event.name,
                        "phone": event.phone,
                        "vehicle_name": event.text,
                        "channel": WA_CHANNEL,
                        "soft_capture": True,
                        "generate_pdf": False,
                    }
                lead_data["channel"] = WA_CHANNEL
                lead_data["auto_reply"] = True
                apply_whatsapp_branch_context(lead_data, event.instance)
                # Inbound WhatsApp → CRM upsert + greeting (not full quote pipeline).
                lead_data["soft_capture"] = True
                lead_data["generate_pdf"] = False
                lead_data["dispatch_whatsapp"] = True
                result = await _run_pipeline(_get_pipeline(), lead_data)
                wa_step = next(
                    (s for s in (result.steps or []) if s.get("step") == "whatsapp"),
                    None,
                )
                auto_reply_sent = (
                    wa_step is not None
                    and wa_step.get("status") == "ok"
                    and wa_step.get("kind") == "auto_reply"
                )
                auto_reply_error = (
                    wa_step.get("error") if wa_step and wa_step.get("status") == "failed" else None
                )
                logger.info(
                    "WhatsApp inbound %s / %s instance=%s branch=%s team=%s "
                    "-> lead %s ok=%s auto_reply=%s",
                    event.phone,
                    event.text[:80],
                    event.instance,
                    lead_data.get("branch"),
                    lead_data.get("branch_id"),
                    result.lead_id,
                    result.ok,
                    auto_reply_sent,
                )
                results.append(
                    {
                        "status": "ok" if result.ok else "error",
                        "phone": event.phone,
                        "instance": event.instance,
                        "branch": lead_data.get("branch"),
                        "branch_id": lead_data.get("branch_id"),
                        "lead_id": result.lead_id,
                        "auto_reply_sent": auto_reply_sent,
                        "auto_reply_error": auto_reply_error,
                        "error": result.error,
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "status": "error",
                        "phone": event.phone,
                        "error": str(exc),
                    }
                )
        return JSONResponse(
            status_code=200,
            content={
                "status": "event_received",
                "processed": len(inbound),
                "ignored": 0 if inbound else 1,
                "results": results,
            },
        )

    app.state.gateway = state  # type: ignore[attr-defined]
    return app


app = create_app()
