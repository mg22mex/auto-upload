"""Phase 2 master pipeline — trade-in → quote → Odoo → PDF → WhatsApp."""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.odoo_sync.client import OdooCRMClient
from src.pdf_engine.generator import generate_vehicle_quote_pdf
from src.quote_engine.engine import CalibratedQuoteEngine
from src.quote_engine.trade_in import TradeInEngine, TradeInVehicle
from src.whatsapp_worker.client import WhatsAppWorkerClient

DEFAULT_CHANNEL = "Voice / Phone"


@dataclass
class PipelineResult:
    ok: bool
    lead_id: int | None = None
    advisor_user_id: int | None = None
    net_trade_in_equity: Decimal = Decimal("0.00")
    estimated_monthly_payment: Decimal | None = None
    whatsapp_message: str = ""
    channel: str = DEFAULT_CHANNEL
    pdf_path: str | None = None
    pdf_attachment_id: int | None = None
    vehicle_sku: str | None = None
    calendar_event_id: int | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("net_trade_in_equity", "estimated_monthly_payment"):
            if payload[key] is not None:
                payload[key] = str(payload[key])
        return payload


class AutosellPipeline:
    """Lead → trade-in → Scotiabank quote → Odoo → PDF → WhatsApp."""

    def __init__(
        self,
        *,
        trade_in: TradeInEngine | None = None,
        quote_engine: CalibratedQuoteEngine | None = None,
        odoo: OdooCRMClient | None = None,
        whatsapp: WhatsAppWorkerClient | None = None,
        assign_advisor: bool = True,
        dispatch_whatsapp: bool = True,
        attach_pdf: bool = True,
        pdf_output_dir: str | Path | None = None,
    ) -> None:
        self.trade_in = trade_in or TradeInEngine()
        self.quote_engine = quote_engine or CalibratedQuoteEngine()
        self.odoo = odoo or OdooCRMClient()
        self.whatsapp = whatsapp or WhatsAppWorkerClient()
        self.assign_advisor = assign_advisor
        self.dispatch_whatsapp = dispatch_whatsapp
        self.attach_pdf = attach_pdf
        self.pdf_output_dir = (
            Path(pdf_output_dir)
            if pdf_output_dir is not None
            else Path(os.getenv("PDF_OUTPUT_DIR") or "data/quotes")
        )

    def process_lead(self, lead_data: dict[str, Any]) -> PipelineResult:
        """Run full Phase 2 flow; return structured execution log."""
        log: list[dict[str, Any]] = []
        result = PipelineResult(ok=False, steps=log)

        try:
            name = str(lead_data.get("name") or "").strip()
            phone = str(lead_data.get("phone") or "").strip()
            vehicle_name = str(
                lead_data.get("vehicle_name") or lead_data.get("vehicle") or ""
            ).strip()
            branch_id = int(lead_data.get("branch_id") or os.getenv("VOICE_DEFAULT_BRANCH_ID") or 1)
            term_months = int(lead_data.get("term_months") or 36)
            channel = str(lead_data.get("channel") or DEFAULT_CHANNEL).strip() or DEFAULT_CHANNEL
            result.channel = channel
            sku = str(
                lead_data.get("sku")
                or lead_data.get("autosell_id")
                or vehicle_name
                or "vehicle"
            ).strip()
            result.vehicle_sku = sku

            if not name or not phone or not vehicle_name:
                raise ValueError("lead_data requires name, phone, vehicle_name")

            # Soft CRM capture (degraded STT) — no quote/PDF math.
            if bool(lead_data.get("soft_capture")):
                self.odoo.authenticate()
                note = str(
                    lead_data.get("notes")
                    or "Voice capture with degraded audio / failed STT. "
                    "Advisor follow-up required."
                )
                lead_result = self.odoo.create_or_update_lead(
                    name,
                    phone,
                    vehicle_name,
                    branch_id,
                    quote_summary=note,
                    stage_name=str(lead_data.get("stage_name") or "New"),
                    channel=channel,
                )
                result.lead_id = lead_result.lead_id
                log.append(
                    {
                        "step": "odoo_lead",
                        "status": "ok",
                        "lead_id": lead_result.lead_id,
                        "tag_ids": list(lead_result.tag_ids),
                        "channel": channel,
                        "soft_capture": True,
                    }
                )
                if lead_result.activity_id is not None:
                    log.append(
                        {
                            "step": "odoo_follow_up",
                            "status": "ok",
                            "activity_id": lead_result.activity_id,
                            "lead_id": lead_result.lead_id,
                        }
                    )
                else:
                    log.append({"step": "odoo_follow_up", "status": "skipped"})
                log.append({"step": "quote", "status": "skipped"})
                log.append({"step": "pdf_spec_sheet", "status": "skipped"})
                log.append({"step": "whatsapp", "status": "skipped"})
                result.ok = True
                return result

            # Resolve missing/zero price from live Odoo inventory.
            odoo_authenticated = False
            raw_price = lead_data.get("vehicle_price")
            vehicle_price = Decimal(str(raw_price or 0))
            if vehicle_price <= 0:
                self.odoo.authenticate()
                odoo_authenticated = True
                inventory = self.odoo.search_vehicle_inventory(vehicle_name)
                priced = [
                    vehicle
                    for vehicle in inventory
                    if Decimal(str(vehicle.get("list_price") or 0)) > 0
                ]
                if not priced:
                    raise ValueError(
                        f"no positive Odoo inventory price found for {vehicle_name!r}"
                    )
                selected = priced[0]
                vehicle_price = Decimal(str(selected["list_price"]))
                if not lead_data.get("sku") and selected.get("default_code"):
                    sku = str(selected["default_code"])
                    result.vehicle_sku = sku
                log.append(
                    {
                        "step": "inventory_lookup",
                        "status": "ok",
                        "product_template_id": selected["id"],
                        "matched_name": selected["name"],
                        "vehicle_price": str(vehicle_price),
                        "qty_available": selected["qty_available"],
                    }
                )
            else:
                log.append(
                    {
                        "step": "inventory_lookup",
                        "status": "skipped",
                        "vehicle_price": str(vehicle_price),
                    }
                )

            # 1) Trade-in equity
            equity = Decimal("0.00")
            trade_raw = lead_data.get("trade_in")
            if trade_raw:
                vehicle = TradeInVehicle(
                    year=int(trade_raw["year"]),
                    make=str(trade_raw["make"]),
                    model=str(trade_raw["model"]),
                    version=str(trade_raw.get("version") or ""),
                    mileage_km=int(trade_raw.get("mileage_km") or 0),
                    vin=str(trade_raw.get("vin") or ""),
                )
                valuation = self.trade_in.value(
                    vehicle,
                    outstanding_lien=trade_raw.get("outstanding_lien") or 0,
                    condition_adjustment=trade_raw.get("condition_adjustment") or 0,
                    manual_guide_value=trade_raw.get("manual_guide_value"),
                )
                equity = valuation.net_equity
                log.append(
                    {
                        "step": "trade_in",
                        "status": "ok",
                        "net_equity": str(equity),
                        "guide_value": str(valuation.guide_value),
                        "source": valuation.source.value,
                        "notes": valuation.notes,
                    }
                )
            else:
                log.append({"step": "trade_in", "status": "skipped"})
            result.net_trade_in_equity = equity

            # 2) Scotiabank loan estimate
            quote = self.quote_engine.calculate(
                vehicle_price,
                term_months,
                down_payment=lead_data.get("down_payment"),
                net_trade_in_equity=equity if equity > 0 else None,
                annual_auto_insurance=lead_data.get("annual_auto_insurance") or 0,
                include_additional_coverages=bool(
                    lead_data.get("include_additional_coverages", True)
                ),
                include_certificate_renewal=bool(
                    lead_data.get("include_certificate_renewal", False)
                ),
                enforce_min_down=bool(lead_data.get("enforce_min_down", True)),
            )
            result.estimated_monthly_payment = quote.estimated_monthly_payment
            log.append(
                {
                    "step": "quote",
                    "status": "ok",
                    "profile": quote.profile_name,
                    "term_months": quote.term_months,
                    "down_payment": str(quote.down_payment),
                    "cash_down_payment": str(quote.cash_down_payment),
                    "financed_principal": str(quote.financed_principal),
                    "base_monthly_payment": str(quote.base_monthly_payment),
                    "estimated_monthly_payment": str(quote.estimated_monthly_payment),
                }
            )

            quote_summary = self.whatsapp.format_quote_message(
                name, vehicle_name, quote
            )
            result.whatsapp_message = quote_summary

            # 3) Odoo CRM + chatter + 24h follow-up activity
            if not odoo_authenticated:
                self.odoo.authenticate()
            lead_result = self.odoo.create_or_update_lead(
                name,
                phone,
                vehicle_name,
                branch_id,
                down_payment=quote.down_payment,
                term_months=quote.term_months,
                quote_summary=quote_summary,
                stage_name="Quote Generated",
                channel=channel,
                estimated_monthly_payment=quote.estimated_monthly_payment,
                vehicle_price=quote.vehicle_price,
            )
            lead_id = lead_result.lead_id
            result.lead_id = lead_id
            log.append(
                {
                    "step": "odoo_lead",
                    "status": "ok",
                    "lead_id": lead_id,
                    "tag_ids": list(lead_result.tag_ids),
                    "channel": channel,
                }
            )
            if lead_result.activity_id is not None:
                log.append(
                    {
                        "step": "odoo_follow_up",
                        "status": "ok",
                        "activity_id": lead_result.activity_id,
                        "lead_id": lead_id,
                    }
                )
            else:
                log.append({"step": "odoo_follow_up", "status": "skipped"})

            advisor_id: int | None = None
            if self.assign_advisor:
                advisor_id = self.odoo.round_robin_assign_advisor(branch_id)
                self.odoo.assign_lead_advisor(lead_id, advisor_id)
                result.advisor_user_id = advisor_id
                log.append(
                    {
                        "step": "odoo_assign_advisor",
                        "status": "ok",
                        "user_id": advisor_id,
                    }
                )
            else:
                log.append({"step": "odoo_assign_advisor", "status": "skipped"})

            chatter_id = self.odoo.post_quote_to_chatter(lead_id, quote_summary)
            log.append(
                {
                    "step": "odoo_chatter",
                    "status": "ok",
                    "message_id": chatter_id,
                }
            )

            # 3b) Optional test-drive calendar booking (non-fatal)
            test_drive = lead_data.get("test_drive")
            if isinstance(test_drive, dict) and test_drive.get("start"):
                try:
                    td = self.odoo.create_test_drive_event(
                        lead_id=lead_id,
                        vehicle_model=str(
                            test_drive.get("vehicle_model")
                            or test_drive.get("vehicle")
                            or vehicle_name
                        ),
                        customer_name=name,
                        start=test_drive.get("start"),
                        stop=test_drive.get("stop"),
                        user_id=advisor_id or result.advisor_user_id,
                        phone=phone,
                        duration_hours=float(
                            test_drive.get("duration_hours") or 1.0
                        ),
                        branch_id=branch_id,
                        dry_run=bool(lead_data.get("odoo_dry_run", False)),
                    )
                    result.calendar_event_id = td.event_id
                    log.append(
                        {
                            "step": "odoo_test_drive",
                            "status": "ok" if td.event_id is not None and not td.error else "error",
                            "event_id": td.event_id,
                            "stage_updated": td.stage_updated,
                            "activity_id": td.activity_id,
                            "dry_run": td.dry_run,
                            "error": td.error,
                        }
                    )
                except Exception as exc:
                    log.append(
                        {
                            "step": "odoo_test_drive",
                            "status": "error",
                            "error": str(exc),
                        }
                    )

            # 3c) Fleet VIN / plate → lead (non-fatal; independent of Meta WA)
            vin = str(
                lead_data.get("vin") or lead_data.get("vin_sn") or ""
            ).strip()
            plate = str(
                lead_data.get("plate") or lead_data.get("license_plate") or ""
            ).strip()
            fleet_vehicle_id = lead_data.get("fleet_vehicle_id")
            want_fleet = bool(lead_data.get("link_fleet", True)) and bool(
                vin or plate or fleet_vehicle_id is not None
            )
            if want_fleet:
                try:
                    fleet_result = self.odoo.link_fleet_vehicle_to_lead(
                        lead_id,
                        vin=vin or None,
                        plate=plate or None,
                        vehicle_id=(
                            int(fleet_vehicle_id)
                            if fleet_vehicle_id is not None
                            else None
                        ),
                        dry_run=bool(lead_data.get("odoo_dry_run", False)),
                    )
                    log.append(
                        {
                            "step": "odoo_fleet_vin",
                            "status": (
                                "ok"
                                if getattr(fleet_result, "ok", False)
                                else "error"
                            ),
                            "lead_id": lead_id,
                            "vehicle_id": getattr(fleet_result, "vehicle_id", None),
                            "vin": getattr(fleet_result, "vin", vin),
                            "linked_via": getattr(fleet_result, "linked_via", ""),
                            "dry_run": getattr(fleet_result, "dry_run", False),
                            "error": getattr(fleet_result, "error", None),
                        }
                    )
                except Exception as exc:
                    log.append(
                        {
                            "step": "odoo_fleet_vin",
                            "status": "error",
                            "error": str(exc),
                        }
                    )
            else:
                log.append({"step": "odoo_fleet_vin", "status": "skipped"})

            # 4) PDF spec sheet → disk + ir.attachment on crm.lead
            want_pdf = self.attach_pdf and bool(lead_data.get("generate_pdf", True))
            if want_pdf:
                quote_data = {
                    "vehicle_price": quote.vehicle_price,
                    "down_payment": quote.down_payment,
                    "cash_down_payment": quote.cash_down_payment,
                    "net_trade_in_equity": quote.net_trade_in_equity,
                    "financed_principal": quote.financed_principal,
                    "origination_fee": quote.origination_fee,
                    "term_months": quote.term_months,
                    "estimated_monthly_payment": quote.estimated_monthly_payment,
                    "monthly_admin_fee": quote.monthly_admin_fee,
                }
                vehicle_data = {
                    "name": vehicle_name,
                    "sku": sku,
                    "autosell_id": sku,
                    "year": lead_data.get("year"),
                    "make": lead_data.get("make"),
                    "model": lead_data.get("model"),
                    "vin": lead_data.get("vin"),
                    "mileage_km": lead_data.get("mileage_km"),
                    "transmission": lead_data.get("transmission"),
                    "features": lead_data.get("features") or [],
                }
                pdf_meta: dict[str, Any] = {}
                pdf_result = generate_vehicle_quote_pdf(
                    quote_data,
                    vehicle_data,
                    output_dir=self.pdf_output_dir,
                    lead_id=lead_id,
                    attach_to_odoo=True,
                    odoo_client=self.odoo,
                    odoo_model="crm.lead",
                    odoo_res_id=lead_id,
                    result_meta=pdf_meta,
                )
                if isinstance(pdf_result, Path):
                    result.pdf_path = str(pdf_result)
                elif pdf_meta.get("path"):
                    result.pdf_path = str(pdf_meta["path"])
                att = pdf_meta.get("attachment_id")
                result.pdf_attachment_id = int(att) if att is not None else None
                log.append(
                    {
                        "step": "pdf_spec_sheet",
                        "status": "ok",
                        "path": result.pdf_path,
                        "attachment_id": result.pdf_attachment_id,
                        "lead_id": lead_id,
                        "sku": sku,
                    }
                )
            else:
                log.append({"step": "pdf_spec_sheet", "status": "skipped"})

            # 5) WhatsApp dispatch
            do_wa = self.dispatch_whatsapp and bool(
                lead_data.get("dispatch_whatsapp", True)
            )
            if do_wa:
                wa_resp = self.whatsapp.send_text_message(phone, quote_summary)
                log.append(
                    {
                        "step": "whatsapp",
                        "status": "ok",
                        "response": wa_resp,
                    }
                )
            else:
                log.append({"step": "whatsapp", "status": "skipped"})

            result.ok = True
            return result
        except Exception as exc:
            result.ok = False
            result.error = str(exc)
            log.append({"step": "error", "status": "failed", "error": str(exc)})
            return result
