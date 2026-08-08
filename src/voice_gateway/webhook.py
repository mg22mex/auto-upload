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
from fastapi.responses import JSONResponse, PlainTextResponse

# Load repo .env before Odoo / Meta clients read os.environ.
_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env")

from src.meta_gateway.gateway import MetaWebhookGateway, parse_messenger_events
from src.pipeline import AutosellPipeline, PipelineResult
from src.voice_gateway.intent import (
    VOICE_CHANNEL,
    VoiceIntent,
    format_tts_quote,
    parse_voice_intent,
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
    lead["channel"] = VOICE_CHANNEL

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


def create_app(
    pipeline: AutosellPipeline | None = None,
    pipeline_factory: Callable[[], AutosellPipeline] | None = None,
    meta_gateway: MetaWebhookGateway | None = None,
    meta_gateway_factory: Callable[[], MetaWebhookGateway] | None = None,
) -> FastAPI:
    """Build FastAPI app. Inject pipeline for tests."""
    app = FastAPI(title="Autosell Voice Gateway", version="0.2.0")
    state: dict[str, Any] = {
        "pipeline": pipeline,
        "pipeline_factory": pipeline_factory or AutosellPipeline,
        "meta_gateway": meta_gateway,
        "meta_gateway_factory": meta_gateway_factory or MetaWebhookGateway,
    }

    def _get_pipeline() -> AutosellPipeline:
        if state["pipeline"] is not None:
            return state["pipeline"]
        return state["pipeline_factory"]()

    def _get_meta_gateway() -> MetaWebhookGateway:
        if state["meta_gateway"] is not None:
            return state["meta_gateway"]
        return state["meta_gateway_factory"]()

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

    app.state.gateway = state  # type: ignore[attr-defined]
    return app


app = create_app()
