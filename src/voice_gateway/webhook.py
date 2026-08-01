"""Voice AI webhook — POST /webhook/voice-lead → AutosellPipeline."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

# Load repo .env before Odoo / Meta clients read os.environ.
_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env")

from src.meta_gateway.gateway import MetaWebhookGateway, parse_messenger_events
from src.pipeline import AutosellPipeline, PipelineResult


def parse_voice_lead_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Map Voice AI JSON → AutosellPipeline lead_data."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")

    phone = str(
        payload.get("caller_phone")
        or payload.get("phone")
        or ""
    ).strip()
    name = str(
        payload.get("caller_name")
        or payload.get("name")
        or ""
    ).strip()
    if not phone:
        raise ValueError("caller_phone is required")
    if not name:
        raise ValueError("caller_name is required")

    interest = payload.get("vehicle_interest")
    vehicle_name = ""
    vehicle_price: Any = payload.get("vehicle_price")
    if isinstance(interest, dict):
        vehicle_name = str(
            interest.get("name")
            or interest.get("vehicle_name")
            or interest.get("title")
            or ""
        ).strip()
        if vehicle_price is None:
            vehicle_price = interest.get("price") or interest.get("vehicle_price")
    else:
        vehicle_name = str(interest or payload.get("vehicle_name") or "").strip()

    if not vehicle_name:
        raise ValueError("vehicle_interest is required")
    if vehicle_price is None:
        raise ValueError("vehicle_price is required (or vehicle_interest.price)")

    term = payload.get("term") or payload.get("term_months") or 36
    branch_raw = payload.get("branch_id") or os.getenv("VOICE_DEFAULT_BRANCH_ID") or "1"
    down = payload.get("down_payment")

    trade_raw = payload.get("trade_in_info") or payload.get("trade_in")
    trade_in: dict[str, Any] | None = None
    if isinstance(trade_raw, dict) and trade_raw:
        trade_in = {
            "year": int(trade_raw["year"]),
            "make": str(trade_raw["make"]),
            "model": str(trade_raw["model"]),
            "version": str(trade_raw.get("version") or ""),
            "mileage_km": int(trade_raw.get("mileage_km") or 0),
            "vin": str(trade_raw.get("vin") or ""),
            "outstanding_lien": trade_raw.get("outstanding_lien") or 0,
            "condition_adjustment": trade_raw.get("condition_adjustment") or 0,
        }
        if trade_raw.get("manual_guide_value") is not None:
            trade_in["manual_guide_value"] = trade_raw["manual_guide_value"]

    lead: dict[str, Any] = {
        "name": name,
        "phone": phone,
        "vehicle_name": vehicle_name,
        "vehicle_price": vehicle_price,
        "term_months": int(term),
        "branch_id": int(branch_raw),
    }
    if down is not None and down != "":
        lead["down_payment"] = down
    if trade_in is not None:
        lead["trade_in"] = trade_in
    if payload.get("annual_auto_insurance") is not None:
        lead["annual_auto_insurance"] = payload["annual_auto_insurance"]
    if "include_certificate_renewal" in payload:
        lead["include_certificate_renewal"] = bool(payload["include_certificate_renewal"])
    if "enforce_min_down" in payload:
        lead["enforce_min_down"] = bool(payload["enforce_min_down"])
    return lead


def create_app(
    pipeline: AutosellPipeline | None = None,
    pipeline_factory: Callable[[], AutosellPipeline] | None = None,
    meta_gateway: MetaWebhookGateway | None = None,
    meta_gateway_factory: Callable[[], MetaWebhookGateway] | None = None,
) -> FastAPI:
    """Build FastAPI app. Inject pipeline for tests."""
    app = FastAPI(title="Autosell Voice Gateway", version="0.1.0")
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

    @app.post("/webhook/voice-lead")
    async def voice_lead(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc

        try:
            lead_data = parse_voice_lead_payload(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        result: PipelineResult = _get_pipeline().process_lead(lead_data)
        body = {
            "status": "ok" if result.ok else "error",
            "confirmation": (
                "voice lead processed"
                if result.ok
                else "voice lead failed"
            ),
            "lead_id": result.lead_id,
            "advisor_user_id": result.advisor_user_id,
            "estimated_monthly_payment": (
                str(result.estimated_monthly_payment)
                if result.estimated_monthly_payment is not None
                else None
            ),
            "net_trade_in_equity": str(result.net_trade_in_equity),
            "steps": result.steps,
            "error": result.error,
        }
        # Always HTTP 200 JSON confirmation per contract (errors in body).
        return JSONResponse(status_code=200, content=body)

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
