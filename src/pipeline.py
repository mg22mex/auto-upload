"""Phase 2 master pipeline — trade-in → quote → Odoo → WhatsApp."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from src.odoo_sync.client import OdooCRMClient
from src.quote_engine.engine import CalibratedQuoteEngine
from src.quote_engine.trade_in import TradeInEngine, TradeInVehicle
from src.whatsapp_worker.client import WhatsAppWorkerClient


@dataclass
class PipelineResult:
    ok: bool
    lead_id: int | None = None
    advisor_user_id: int | None = None
    net_trade_in_equity: Decimal = Decimal("0.00")
    estimated_monthly_payment: Decimal | None = None
    whatsapp_message: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("net_trade_in_equity", "estimated_monthly_payment"):
            if payload[key] is not None:
                payload[key] = str(payload[key])
        return payload


class AutosellPipeline:
    """Lead → trade-in → Scotiabank quote → Odoo chatter → WhatsApp dispatch."""

    def __init__(
        self,
        *,
        trade_in: TradeInEngine | None = None,
        quote_engine: CalibratedQuoteEngine | None = None,
        odoo: OdooCRMClient | None = None,
        whatsapp: WhatsAppWorkerClient | None = None,
        assign_advisor: bool = True,
        dispatch_whatsapp: bool = True,
    ) -> None:
        self.trade_in = trade_in or TradeInEngine()
        self.quote_engine = quote_engine or CalibratedQuoteEngine()
        self.odoo = odoo or OdooCRMClient()
        self.whatsapp = whatsapp or WhatsAppWorkerClient()
        self.assign_advisor = assign_advisor
        self.dispatch_whatsapp = dispatch_whatsapp

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
            branch_id = int(lead_data["branch_id"])
            term_months = int(lead_data.get("term_months") or 36)
            if not name or not phone or not vehicle_name:
                raise ValueError("lead_data requires name, phone, vehicle_name")

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

            # 3) Odoo CRM + chatter
            if not odoo_authenticated:
                self.odoo.authenticate()
            lead_id = self.odoo.create_or_update_lead(
                name, phone, vehicle_name, branch_id
            )
            result.lead_id = lead_id
            log.append(
                {"step": "odoo_lead", "status": "ok", "lead_id": lead_id}
            )

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

            # 4) WhatsApp dispatch
            if self.dispatch_whatsapp:
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
