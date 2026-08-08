"""Unit test — AutosellPipeline end-to-end with mocks."""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.odoo_sync.client import QuoteLeadResult
from src.odoo_sync.fleet import FleetLinkResult
from src.pipeline import AutosellPipeline
from src.quote_engine.calculator import QuoteResult
from src.quote_engine.engine import CalibratedQuoteEngine
from src.quote_engine.scotiabank_profile import SCOTIABANK_PROFILE
from src.quote_engine.trade_in import TradeInEngine, TradeInValuation, ValuationSource

try:
    from src.pdf_engine.generator import _HAS_REPORTLAB
except ImportError:
    _HAS_REPORTLAB = False


def _sample_quote(**overrides) -> QuoteResult:
    base = dict(
        vehicle_price=Decimal("300000.00"),
        term_months=36,
        annual_rate=SCOTIABANK_PROFILE.annual_interest_rate,
        down_payment=Decimal("50000.00"),
        cash_down_payment=Decimal("0.00"),
        net_trade_in_equity=Decimal("50000.00"),
        down_payment_pct=Decimal("16.67"),
        amount_to_finance=Decimal("250000.00"),
        origination_fee=Decimal("7250.00"),
        financed_principal=Decimal("272110.00"),
        base_monthly_payment=Decimal("9500.00"),
        monthly_auto_insurance=Decimal("1000.00"),
        monthly_life_insurance=Decimal("141.67"),
        average_monthly_iva=Decimal("250.00"),
        estimated_monthly_payment=Decimal("10891.67"),
        schedule=(),
        profile_name=SCOTIABANK_PROFILE.name,
        monthly_admin_fee=Decimal("58.00"),
        opening_fee_iva=Decimal("1000.00"),
    )
    base.update(overrides)
    return QuoteResult(**base)


class TestAutosellPipeline(unittest.TestCase):
    def test_full_lead_flow(self):
        trade = MagicMock(spec=TradeInEngine)
        trade.value.return_value = TradeInValuation(
            source=ValuationSource.MANUAL,
            guide_value=Decimal("80000.00"),
            outstanding_lien=Decimal("30000.00"),
            adjustments=Decimal("0.00"),
            net_equity=Decimal("50000.00"),
            notes="manual",
            raw={"mode": "manual"},
        )

        quote_engine = MagicMock(spec=CalibratedQuoteEngine)
        quote_engine.calculate.return_value = _sample_quote()

        odoo = MagicMock()
        odoo.authenticate.return_value = 1
        odoo.create_or_update_lead.return_value = QuoteLeadResult(
            lead_id=501, activity_id=777, tag_ids=(9,)
        )
        odoo.round_robin_assign_advisor.return_value = 42
        odoo.assign_lead_advisor.return_value = True
        odoo.post_quote_to_chatter.return_value = 9001

        wa = MagicMock()
        wa.format_quote_message.return_value = (
            "Hola Ana\nCotización Autosell — Mazda CX-5 2020\n"
            "Total estimado: $10,891.67"
        )
        wa.send_text_message.return_value = {"key": {"id": "wamid.1"}}

        pipeline = AutosellPipeline(
            trade_in=trade,
            quote_engine=quote_engine,
            odoo=odoo,
            whatsapp=wa,
            attach_pdf=False,
        )

        lead = {
            "name": "Ana Pérez",
            "phone": "6141234567",
            "vehicle_name": "Mazda CX-5 2020",
            "vehicle_price": 300000,
            "term_months": 36,
            "branch_id": 3,
            "annual_auto_insurance": 12000,
            "trade_in": {
                "year": 2018,
                "make": "Nissan",
                "model": "Sentra",
                "mileage_km": 72000,
                "outstanding_lien": 30000,
                "manual_guide_value": 80000,
            },
        }

        result = pipeline.process_lead(lead)

        self.assertTrue(result.ok)
        self.assertIsNone(result.error)
        self.assertEqual(result.lead_id, 501)
        self.assertEqual(result.advisor_user_id, 42)
        self.assertEqual(result.net_trade_in_equity, Decimal("50000.00"))
        self.assertEqual(result.estimated_monthly_payment, Decimal("10891.67"))
        self.assertIn("10,891.67", result.whatsapp_message)

        steps = {s["step"]: s for s in result.steps}
        self.assertEqual(steps["trade_in"]["status"], "ok")
        self.assertEqual(steps["quote"]["status"], "ok")
        self.assertEqual(steps["odoo_lead"]["lead_id"], 501)
        self.assertEqual(steps["odoo_assign_advisor"]["user_id"], 42)
        self.assertEqual(steps["odoo_chatter"]["message_id"], 9001)
        self.assertEqual(steps["odoo_fleet_vin"]["status"], "skipped")
        self.assertEqual(steps["whatsapp"]["status"], "ok")

        trade.value.assert_called_once()
        quote_engine.calculate.assert_called_once()
        kwargs = quote_engine.calculate.call_args.kwargs
        self.assertEqual(kwargs["net_trade_in_equity"], Decimal("50000.00"))
        odoo.authenticate.assert_called_once()
        odoo.create_or_update_lead.assert_called_once()
        lead_call = odoo.create_or_update_lead.call_args
        self.assertEqual(
            lead_call.args[:4],
            ("Ana Pérez", "6141234567", "Mazda CX-5 2020", 3),
        )
        self.assertEqual(lead_call.kwargs.get("channel"), "Voice / Phone")
        self.assertEqual(lead_call.kwargs.get("stage_name"), "Quote Generated")
        self.assertEqual(lead_call.kwargs.get("term_months"), 36)
        odoo.post_quote_to_chatter.assert_called_once()
        wa.send_text_message.assert_called_once()
        self.assertEqual(wa.send_text_message.call_args.args[0], "6141234567")

    @unittest.skipUnless(_HAS_REPORTLAB, "reportlab not installed")
    def test_pdf_attached_to_lead(self):
        quote_engine = MagicMock(spec=CalibratedQuoteEngine)
        quote_engine.calculate.return_value = _sample_quote(
            net_trade_in_equity=Decimal("0.00"),
            cash_down_payment=Decimal("30000.00"),
            down_payment=Decimal("30000.00"),
        )
        odoo = MagicMock()
        odoo.authenticate.return_value = 1
        odoo.create_or_update_lead.return_value = QuoteLeadResult(
            lead_id=501, activity_id=777, tag_ids=(9,)
        )
        odoo.round_robin_assign_advisor.return_value = 42
        odoo.post_quote_to_chatter.return_value = 1
        odoo.attach_file.return_value = 9001
        wa = MagicMock()
        wa.format_quote_message.return_value = "msg"
        wa.send_text_message.return_value = {"ok": True}

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            pipeline = AutosellPipeline(
                quote_engine=quote_engine,
                odoo=odoo,
                whatsapp=wa,
                assign_advisor=False,
                dispatch_whatsapp=False,
                attach_pdf=True,
                pdf_output_dir=tmp,
            )
            result = pipeline.process_lead(
                {
                    "name": "Ana",
                    "phone": "6141234567",
                    "vehicle_name": "Mazda CX-5 2020",
                    "vehicle_price": 300000,
                    "term_months": 36,
                    "branch_id": 1,
                    "sku": "obj969",
                    "channel": "Voice / Phone",
                }
            )

            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.pdf_attachment_id, 9001)
            self.assertIsNotNone(result.pdf_path)
            self.assertTrue(Path(result.pdf_path).exists())

        odoo.attach_file.assert_called_once()
        att_kwargs = odoo.attach_file.call_args.kwargs
        self.assertEqual(att_kwargs["model"], "crm.lead")
        self.assertEqual(att_kwargs["res_id"], 501)
        self.assertEqual(att_kwargs["mimetype"], "application/pdf")
        steps = {s["step"]: s for s in result.steps}
        self.assertEqual(steps["pdf_spec_sheet"]["status"], "ok")
        self.assertEqual(steps["odoo_follow_up"]["activity_id"], 777)

    def test_fleet_vin_linked_to_lead(self):
        """Next pipeline step after Meta-blocked WA: attach fleet VIN on create."""
        quote_engine = MagicMock(spec=CalibratedQuoteEngine)
        quote_engine.calculate.return_value = _sample_quote(
            net_trade_in_equity=Decimal("0.00"),
            cash_down_payment=Decimal("30000.00"),
            down_payment=Decimal("30000.00"),
        )
        odoo = MagicMock()
        odoo.authenticate.return_value = 1
        odoo.create_or_update_lead.return_value = QuoteLeadResult(
            lead_id=501, activity_id=1, tag_ids=()
        )
        odoo.post_quote_to_chatter.return_value = 1
        odoo.link_fleet_vehicle_to_lead.return_value = FleetLinkResult(
            ok=True,
            lead_id=501,
            vehicle_id=9,
            vin="JM3KFBCM5L0123456",
            linked_via="x_vin",
            dry_run=False,
        )
        wa = MagicMock()
        wa.format_quote_message.return_value = "msg"
        pipeline = AutosellPipeline(
            quote_engine=quote_engine,
            odoo=odoo,
            whatsapp=wa,
            assign_advisor=False,
            dispatch_whatsapp=False,
            attach_pdf=False,
        )
        result = pipeline.process_lead(
            {
                "name": "Ana",
                "phone": "6141234567",
                "vehicle_name": "Mazda CX-5 2020",
                "vehicle_price": 300000,
                "term_months": 36,
                "branch_id": 1,
                "sku": "obj969",
                "vin": "JM3KFBCM5L0123456",
                "channel": "Voice / Phone",
            }
        )
        self.assertTrue(result.ok, result.error)
        odoo.link_fleet_vehicle_to_lead.assert_called_once()
        call_kw = odoo.link_fleet_vehicle_to_lead.call_args
        self.assertEqual(call_kw.args[0], 501)
        self.assertEqual(call_kw.kwargs.get("vin"), "JM3KFBCM5L0123456")
        steps = {s["step"]: s for s in result.steps}
        self.assertEqual(steps["odoo_fleet_vin"]["status"], "ok")
        self.assertEqual(steps["odoo_fleet_vin"]["linked_via"], "x_vin")
        self.assertEqual(steps["odoo_fleet_vin"]["vin"], "JM3KFBCM5L0123456")

    def test_fleet_vin_dry_run_flag(self):
        quote_engine = MagicMock(spec=CalibratedQuoteEngine)
        quote_engine.calculate.return_value = _sample_quote(
            net_trade_in_equity=Decimal("0.00"),
            cash_down_payment=Decimal("30000.00"),
            down_payment=Decimal("30000.00"),
        )
        odoo = MagicMock()
        odoo.authenticate.return_value = 1
        odoo.create_or_update_lead.return_value = QuoteLeadResult(
            lead_id=55, activity_id=1, tag_ids=()
        )
        odoo.post_quote_to_chatter.return_value = 1
        odoo.link_fleet_vehicle_to_lead.return_value = FleetLinkResult(
            ok=True,
            lead_id=55,
            vin="ABC123VIN",
            linked_via="dry_run",
            dry_run=True,
        )
        pipeline = AutosellPipeline(
            quote_engine=quote_engine,
            odoo=odoo,
            whatsapp=MagicMock(),
            assign_advisor=False,
            dispatch_whatsapp=False,
            attach_pdf=False,
        )
        result = pipeline.process_lead(
            {
                "name": "Luis",
                "phone": "6149998877",
                "vehicle_name": "CX-5",
                "vehicle_price": 300000,
                "term_months": 36,
                "branch_id": 1,
                "vin": "ABC123VIN",
                "odoo_dry_run": True,
            }
        )
        self.assertTrue(result.ok, result.error)
        self.assertTrue(
            odoo.link_fleet_vehicle_to_lead.call_args.kwargs.get("dry_run")
        )
        steps = {s["step"]: s for s in result.steps}
        self.assertEqual(steps["odoo_fleet_vin"]["status"], "ok")
        self.assertTrue(steps["odoo_fleet_vin"]["dry_run"])

    def test_soft_capture_skips_quote_pdf(self):
        odoo = MagicMock()
        odoo.authenticate.return_value = 1
        odoo.create_or_update_lead.return_value = QuoteLeadResult(
            lead_id=88, activity_id=9, tag_ids=()
        )
        quote_engine = MagicMock(spec=CalibratedQuoteEngine)
        pipeline = AutosellPipeline(
            quote_engine=quote_engine,
            odoo=odoo,
            whatsapp=MagicMock(),
            attach_pdf=True,
            dispatch_whatsapp=False,
            assign_advisor=False,
        )
        result = pipeline.process_lead(
            {
                "name": "Pedro",
                "phone": "6145556677",
                "vehicle_name": "Consulta telefónica",
                "branch_id": 1,
                "channel": "Voice / Phone",
                "soft_capture": True,
            }
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.lead_id, 88)
        quote_engine.calculate.assert_not_called()
        odoo.attach_file.assert_not_called()
        steps = {s["step"]: s for s in result.steps}
        self.assertTrue(steps["odoo_lead"].get("soft_capture"))
        self.assertEqual(steps["quote"]["status"], "skipped")
        self.assertEqual(steps["pdf_spec_sheet"]["status"], "skipped")

    def test_test_drive_booking_step(self):
        from src.odoo_sync.client import TestDriveEventResult

        quote_engine = MagicMock(spec=CalibratedQuoteEngine)
        quote_engine.calculate.return_value = _sample_quote(
            net_trade_in_equity=Decimal("0.00"),
            cash_down_payment=Decimal("30000.00"),
            down_payment=Decimal("30000.00"),
        )
        odoo = MagicMock()
        odoo.authenticate.return_value = 1
        odoo.create_or_update_lead.return_value = QuoteLeadResult(
            lead_id=501, activity_id=1, tag_ids=()
        )
        odoo.round_robin_assign_advisor.return_value = 42
        odoo.post_quote_to_chatter.return_value = 1
        odoo.create_test_drive_event.return_value = TestDriveEventResult(
            event_id=9001,
            lead_id=501,
            stage_updated=True,
            activity_id=777,
        )
        wa = MagicMock()
        wa.format_quote_message.return_value = "msg"
        pipeline = AutosellPipeline(
            quote_engine=quote_engine,
            odoo=odoo,
            whatsapp=wa,
            assign_advisor=True,
            dispatch_whatsapp=False,
            attach_pdf=False,
        )
        result = pipeline.process_lead(
            {
                "name": "Ana",
                "phone": "6141234567",
                "vehicle_name": "Mazda CX-5",
                "vehicle_price": 300000,
                "term_months": 36,
                "branch_id": 1,
                "test_drive": {
                    "start": "2026-08-10T16:00:00-06:00",
                    "vehicle_model": "Mazda CX-5",
                },
            }
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.calendar_event_id, 9001)
        odoo.create_test_drive_event.assert_called_once()
        steps = {s["step"]: s for s in result.steps}
        self.assertEqual(steps["odoo_test_drive"]["event_id"], 9001)

    def test_no_trade_in_skips_valuation(self):
        trade = MagicMock(spec=TradeInEngine)
        quote_engine = MagicMock(spec=CalibratedQuoteEngine)
        quote_engine.calculate.return_value = _sample_quote(
            net_trade_in_equity=Decimal("0.00"),
            cash_down_payment=Decimal("30000.00"),
            down_payment=Decimal("30000.00"),
        )
        odoo = MagicMock()
        odoo.authenticate.return_value = 1
        odoo.create_or_update_lead.return_value = QuoteLeadResult(lead_id=7)
        odoo.round_robin_assign_advisor.return_value = 1
        odoo.post_quote_to_chatter.return_value = 1
        wa = MagicMock()
        wa.format_quote_message.return_value = "msg"
        wa.send_text_message.return_value = {"ok": True}

        pipeline = AutosellPipeline(
            trade_in=trade,
            quote_engine=quote_engine,
            odoo=odoo,
            whatsapp=wa,
            attach_pdf=False,
        )
        result = pipeline.process_lead(
            {
                "name": "Luis",
                "phone": "6140001111",
                "vehicle_name": "Vento 2018",
                "vehicle_price": 150000,
                "term_months": 12,
                "branch_id": 1,
            }
        )
        self.assertTrue(result.ok)
        trade.value.assert_not_called()
        self.assertEqual(
            next(s for s in result.steps if s["step"] == "trade_in")["status"],
            "skipped",
        )
        self.assertIsNone(quote_engine.calculate.call_args.kwargs.get("net_trade_in_equity"))

    def test_missing_price_resolves_from_odoo_inventory(self):
        quote_engine = MagicMock(spec=CalibratedQuoteEngine)
        quote_engine.calculate.return_value = _sample_quote(
            vehicle_price=Decimal("289000.00")
        )
        odoo = MagicMock()
        odoo.authenticate.return_value = 2
        odoo.search_vehicle_inventory.return_value = [
            {
                "id": 638,
                "name": "MAZDA CX3 2020",
                "list_price": 289000.0,
                "qty_available": 1.0,
                "categ_id": 8,
                "category_name": "vehiculos",
            }
        ]
        odoo.create_or_update_lead.return_value = QuoteLeadResult(lead_id=1851)
        odoo.post_quote_to_chatter.return_value = 13540
        wa = MagicMock()
        wa.format_quote_message.return_value = "Scotiabank quote"

        pipeline = AutosellPipeline(
            quote_engine=quote_engine,
            odoo=odoo,
            whatsapp=wa,
            assign_advisor=False,
            dispatch_whatsapp=False,
            attach_pdf=False,
        )
        result = pipeline.process_lead(
            {
                "name": "Prueba Inventario AI",
                "phone": "6140000001",
                "vehicle_name": "Mazda CX3",
                "term_months": 36,
                "branch_id": 1,
            }
        )

        self.assertTrue(result.ok, result.error)
        odoo.search_vehicle_inventory.assert_called_once_with("Mazda CX3")
        self.assertEqual(
            quote_engine.calculate.call_args.args[0], Decimal("289000.0")
        )
        inventory_step = next(
            step for step in result.steps if step["step"] == "inventory_lookup"
        )
        self.assertEqual(inventory_step["product_template_id"], 638)
        self.assertEqual(inventory_step["vehicle_price"], "289000.0")


if __name__ == "__main__":
    unittest.main()
