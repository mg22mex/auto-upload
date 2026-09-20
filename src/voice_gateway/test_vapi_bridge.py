from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from src.voice_gateway.vapi_bridge import (
    FinancingArgs,
    InventoryArgs,
    TradeInArgs,
    build_domain,
    extract_tool_calls,
    format_financing_speech,
    format_inventory_speech,
    format_price_voice_es,
    format_tradein_speech,
    handle_financing_payload,
    handle_inventory_payload,
    handle_tradein_payload,
    number_to_words_es,
)


class TestNumberWords(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(number_to_words_es(0), "cero")
        self.assertEqual(number_to_words_es(16), "dieciséis")
        self.assertEqual(number_to_words_es(21), "veintiuno")
        self.assertEqual(number_to_words_es(100), "cien")
        self.assertEqual(number_to_words_es(200_000), "doscientos mil")
        self.assertEqual(number_to_words_es(1_000_000), "un millón")

    def test_price_words(self):
        self.assertEqual(format_price_voice_es(200_000), "doscientos mil pesos")
        self.assertNotIn("$", format_price_voice_es(369_000))


class TestFormatters(unittest.TestCase):
    def test_empty_speech(self):
        text = format_inventory_speech([], InventoryArgs(brand="Mazda"))
        self.assertIn("No encontré", text)
        self.assertIn("Mazda", text)

    def test_rows_speech(self):
        text = format_inventory_speech(
            [{"name": "CX5 * Mazda 2020", "list_price": 369000, "default_code": "obj705"}],
            InventoryArgs(brand="Mazda", max_price=400000),
        )
        self.assertIn("Tengo", text)
        self.assertIn("pesos", text)
        self.assertIn("Mazda", text)
        self.assertIn("Ubicación: Lote Periférico", text)
        self.assertIn("No digas Sucursal Autosell", text)
        self.assertNotIn("Consignación", text)
        self.assertNotIn("$", text)

    def test_location_markers(self):
        from src.voice_gateway.vapi_bridge import (
            branch_from_vehicle_title,
            branch_marker_from_title,
            location_speech_for_title,
        )

        self.assertEqual(branch_marker_from_title("Cx5 * Mazda 2020"), "*")
        self.assertEqual(branch_marker_from_title("+ RAV4 Toyota 2021"), "+")
        self.assertEqual(branch_marker_from_title("Sport - Mazda 2022"), "-")
        self.assertEqual(branch_marker_from_title("Cx 30 IGT *"), "*")
        self.assertIsNone(branch_marker_from_title("Corolla Toyota 2022"))
        self.assertEqual(
            location_speech_for_title("+ RAV4 Toyota 2021"),
            "Ubicación: Lote San Felipe",
        )
        dash = location_speech_for_title("Sport - Mazda 2022")
        self.assertEqual(
            dash,
            "Disponible para entrega en la sucursal de tu preferencia "
            "(Periférico o San Felipe)",
        )
        self.assertNotIn("Consignación", dash)
        self.assertNotIn("consignación", dash)
        self.assertIn(
            "consultar disponibilidad",
            location_speech_for_title("Corolla Toyota 2022"),
        )
        speech = format_inventory_speech(
            [{"name": "MX 5 I Sport - Mazda 2022", "list_price": 345000}],
            InventoryArgs(brand="Mazda"),
        )
        self.assertIn("disponible para entrega", speech.lower())
        self.assertIn("Lote Periférico o Lote San Felipe", speech)
        self.assertNotIn("Consignación", speech)
        self.assertEqual(
            branch_from_vehicle_title("Cx5 * Mazda 2020"),
            ("periferico", "Periférico"),
        )
        self.assertEqual(
            branch_from_vehicle_title("+ RAV4 Toyota 2021")[0],
            "san_felipe",
        )
        self.assertEqual(
            branch_from_vehicle_title("Sport - Mazda 2022")[0],
            "periferico",
        )


class TestParse(unittest.TestCase):
    def test_vapi_envelope(self):
        payload = {
            "message": {
                "toolCalls": [
                    {
                        "id": "call_abc",
                        "function": {
                            "name": "get_inventory",
                            "arguments": '{"brand":"Mazda","max_price":500000,"year":2020}',
                        },
                    }
                ]
            }
        }
        pairs = extract_tool_calls(payload)
        self.assertEqual(len(pairs), 1)
        call_id, args = pairs[0]
        self.assertEqual(call_id, "call_abc")
        self.assertEqual(args.brand, "Mazda")
        self.assertEqual(args.max_price, 500000)
        self.assertEqual(args.year, 2020)

    def test_flat_payload(self):
        pairs = extract_tool_calls({"brand": "Ford", "max_price": 800000})
        self.assertEqual(pairs[0][0], "call_direct")
        self.assertEqual(pairs[0][1].brand, "Ford")

    def test_soft_empty_and_null_fields(self):
        pairs = extract_tool_calls(
            {
                "brand": "",
                "max_price": "null",
                "year": "undefined",
            }
        )
        args = pairs[0][1]
        self.assertIsNone(args.brand)
        self.assertIsNone(args.max_price)
        self.assertIsNone(args.year)

    def test_soft_mistyped_strings(self):
        pairs = extract_tool_calls(
            {
                "brand": "  Mazda ",
                "max_price": "$450,000",
                "year": "2020",
            }
        )
        args = pairs[0][1]
        self.assertEqual(args.brand, "Mazda")
        self.assertEqual(args.max_price, 450000.0)
        self.assertEqual(args.year, 2020)

    def test_soft_invalid_year_ignored(self):
        pairs = extract_tool_calls({"brand": "Ford", "year": "dos mil", "max_price": "abc"})
        args = pairs[0][1]
        self.assertEqual(args.brand, "Ford")
        self.assertIsNone(args.year)
        self.assertIsNone(args.max_price)

    def test_domain(self):
        domain = build_domain(
            InventoryArgs(brand="audi", model="a3", max_price=900000, year=2018)
        )
        self.assertIn(("name", "ilike", "audi"), domain)
        self.assertIn(("name", "ilike", "a3"), domain)
        self.assertIn(("list_price", "<=", 900000.0), domain)
        self.assertIn(("name", "ilike", "2018"), domain)
        self.assertIn(("sale_ok", "=", True), domain)
        self.assertIn(("active", "=", True), domain)
        self.assertTrue(
            any(
                isinstance(term, (list, tuple))
                and len(term) == 3
                and term[1] == "in"
                and "available" in term[2]
                for term in domain
            )
        )

    def test_model_in_vapi_payload(self):
        pairs = extract_tool_calls(
            {
                "message": {
                    "toolCalls": [
                        {
                            "id": "call_test",
                            "function": {
                                "arguments": {"brand": "Toyota", "model": "Corolla"},
                            },
                        }
                    ]
                }
            }
        )
        args = pairs[0][1]
        self.assertEqual(args.brand, "Toyota")
        self.assertEqual(args.model, "Corolla")


class TestHandle(unittest.TestCase):
    def test_handle_with_mock_odoo(self):
        import asyncio

        models = MagicMock()
        models.execute_kw.return_value = [
            {
                "id": 1,
                "name": "CX5 Mazda 2020",
                "list_price": 369000.0,
                "default_code": "obj705",
            }
        ]
        with patch(
            "src.voice_gateway.vapi_bridge.connect_odoo",
            return_value=("db", 2, models, "key"),
        ):
            resp = asyncio.run(
                handle_inventory_payload(
                    {
                        "message": {
                            "toolCalls": [
                                {
                                    "id": "tc1",
                                    "function": {
                                        "arguments": '{"brand":"Mazda","max_price":400000}'
                                    },
                                }
                            ]
                        }
                    }
                )
            )
        self.assertEqual(len(resp.results), 1)
        self.assertEqual(resp.results[0].toolCallId, "tc1")
        self.assertIn("CX5 Mazda 2020", resp.results[0].result)
        models.execute_kw.assert_called()
        call_kw = models.execute_kw.call_args
        # execute_kw(db, uid, password, model, method, [domain], opts)
        opts = call_kw.args[6] if len(call_kw.args) > 6 else call_kw.kwargs
        self.assertEqual(opts["limit"], 3)
        self.assertNotIn("qty_available", opts["fields"])
        self.assertNotIn("categ_id", opts["fields"])

    def test_inventory_timeout_fallback(self):
        import asyncio

        from src.voice_gateway import vapi_bridge as vb

        def _slow(_args):
            import time

            time.sleep(0.2)
            return []

        with (
            patch.object(vb, "INVENTORY_TIMEOUT_SEC", 0.05),
            patch.object(vb, "_search_inventory_blocking", side_effect=_slow),
        ):
            resp = asyncio.run(
                handle_inventory_payload({"brand": "Mazda", "max_price": 400000})
            )
        text = resp.results[0].result.lower()
        self.assertIn("tardó un momento", text)
        self.assertIn("whatsapp", text)
        self.assertIn("prueba de manejo", text)

    def test_inventory_cache_hit_skips_odoo(self):
        import asyncio
        import time

        from src.odoo_sync import inventory as inv
        from src.voice_gateway import vapi_bridge as vb

        inv.cache_clear()
        rows = [
            {
                "id": 1,
                "name": "Toyota RAV4 2021",
                "list_price": 389000.0,
                "default_code": "rav1",
            }
        ]
        key = inv.cache_key(brand="RAV4", max_price=None, year=None, limit=3)
        inv.cache_set(key, rows)

        with patch.object(vb, "_search_inventory_blocking") as blocking:
            t0 = time.perf_counter()
            resp = asyncio.run(handle_inventory_payload({"brand": "RAV4"}))
            elapsed_ms = (time.perf_counter() - t0) * 1000
        blocking.assert_not_called()
        self.assertIn("RAV4", resp.results[0].result)
        self.assertLess(elapsed_ms, 50)


class TestFinancing(unittest.TestCase):
    def test_flat_financing(self):
        resp = handle_financing_payload(
            {"vehicle_price": 450000, "term_months": 48, "down_payment": 90000}
        )
        self.assertEqual(resp.results[0].toolCallId, "call_direct")
        text = resp.results[0].result
        self.assertIn("enganche", text.lower())
        self.assertIn("Scotiabank", text)
        self.assertIn("WhatsApp", text)
        self.assertNotIn("$", text)
        self.assertRegex(text, r"pesos")

    def test_financing_speech_template(self):
        quote = MagicMock(
            down_payment=Decimal("90000"),
            term_months=48,
            estimated_monthly_payment=Decimal("12345.67"),
        )
        text = format_financing_speech(
            FinancingArgs(vehicle_price=450000, term_months=48, down_payment=90000),
            quote,
        )
        self.assertIn("noventa mil pesos", text)
        self.assertIn("cuarenta y ocho meses", text)


class TestTradeIn(unittest.TestCase):
    def test_flat_tradein(self):
        resp = handle_tradein_payload(
            {"brand": "Toyota", "model": "Corolla", "year": 2020, "mileage": 80000}
        )
        text = resp.results[0].result
        self.assertIn("Autométrica", text)
        self.assertIn("Toyota", text)
        self.assertIn("Corolla", text)
        self.assertIn("2020", text)
        self.assertIn("inspección física", text)
        self.assertNotIn("$", text)

    def test_tradein_speech(self):
        val = MagicMock(net_equity=Decimal("185000"), raw={"matched": True})
        text = format_tradein_speech(
            TradeInArgs(brand="Toyota", model="Corolla", year=2020, mileage=80000),
            val,
        )
        self.assertIn("ciento ochenta y cinco mil pesos", text)


class TestLead(unittest.TestCase):
    def test_lead_speech(self):
        from src.voice_gateway.vapi_bridge import LeadArgs, format_lead_speech

        text = format_lead_speech(
            LeadArgs(
                name="María López",
                phone="6141234567",
                appointment_date="el 18 a las 15 horas",
            )
        )
        self.assertIn("María López", text)
        self.assertIn("quince", text)  # 15 → words
        self.assertIn("Autosell", text)
        self.assertNotIn("15", text)

    def test_lead_speech_updated(self):
        from src.voice_gateway.vapi_bridge import LeadArgs, format_lead_speech

        text = format_lead_speech(
            LeadArgs(name="Ana", phone="6141112233"),
            status="updated",
        )
        self.assertIn("Actualicé tu expediente", text)
        self.assertIn("Ana", text)

    def test_create_lead_mocked(self):
        from src.voice_gateway.vapi_bridge import handle_lead_payload

        manager = MagicMock()
        manager.create_or_update_lead.return_value = {
            "status": "created",
            "lead_id": 42,
            "branch": "periferico",
            "dry_run": False,
        }
        wa = MagicMock()
        wa.send_text_message.return_value = {"ok": True}
        resp = handle_lead_payload(
            {
                "name": "Juan Pérez",
                "phone": "6149876543",
                "interested_vehicle": "Mazda CX-5 2020",
                "financing_summary": "mensualidad estimada cuarenta mil",
                "tradein_summary": "Corolla 2018",
                "appointment_date": "mañana a las 11",
            },
            manager=manager,
            whatsapp_client=wa,
        )
        self.assertEqual(resp.results[0].toolCallId, "call_direct")
        self.assertIn("Juan Pérez", resp.results[0].result)
        self.assertIn("Cita registrada", resp.results[0].result)
        manager.create_or_update_lead.assert_called_once()
        payload = manager.create_or_update_lead.call_args.args[0]
        branch_arg = manager.create_or_update_lead.call_args.kwargs.get("branch")
        if branch_arg is None and len(manager.create_or_update_lead.call_args.args) > 1:
            branch_arg = manager.create_or_update_lead.call_args.args[1]
        self.assertEqual(branch_arg, "periferico")
        self.assertIn("Financiamiento:", payload["description"])
        self.assertIn("Cita preferida:", payload["description"])
        self.assertTrue(payload["opportunity_name"].startswith("Llamada Paulina - "))
        self.assertEqual(payload["stage_name"], "Cita/Prueba de manejo")
        self.assertTrue(payload["assign_round_robin"])
        self.assertTrue(payload["preserve_salesperson"])
        manager.odoo.execute_kw.assert_not_called()
        wa.send_text_message.assert_called_once()
        wa_args = wa.send_text_message.call_args
        self.assertEqual(wa_args.args[0], "526149876543")
        self.assertIn("Juan Pérez", wa_args.args[1])
        self.assertIn("Mazda CX-5 2020", wa_args.args[1])

    def test_create_lead_san_felipe_branch_from_marker(self):
        from src.voice_gateway.vapi_bridge import handle_lead_payload

        manager = MagicMock()
        manager.create_or_update_lead.return_value = {
            "status": "created",
            "lead_id": 77,
            "branch": "san_felipe",
            "team_id": 5,
            "dry_run": False,
        }
        wa = MagicMock()
        handle_lead_payload(
            {
                "name": "Ana",
                "phone": "6141112222",
                "interested_vehicle": "+ RAV4 Toyota 2021",
                "appointment_date": "hoy 17",
                "financing_summary": None,
                "tradein_summary": None,
            },
            manager=manager,
            whatsapp_client=wa,
        )
        payload = manager.create_or_update_lead.call_args.args[0]
        branch_arg = manager.create_or_update_lead.call_args.kwargs.get(
            "branch",
            manager.create_or_update_lead.call_args.args[1]
            if len(manager.create_or_update_lead.call_args.args) > 1
            else None,
        )
        self.assertEqual(branch_arg, "san_felipe")
        self.assertEqual(payload.get("physical_location"), "San Felipe")
        wa.send_text_message.assert_called_once()

    def test_update_existing_lead_by_id(self):
        from src.voice_gateway.vapi_bridge import handle_lead_payload

        manager = MagicMock()
        manager.create_or_update_lead.return_value = {
            "status": "updated",
            "lead_id": 1937,
            "deduplicated": True,
            "branch": "periferico",
            "dry_run": False,
        }
        wa = MagicMock()
        resp = handle_lead_payload(
            {
                "name": "Marco",
                "phone": "6145551212",
                "lead_id": 1937,
                "appointment_date": "viernes a las 16",
            },
            manager=manager,
            whatsapp_client=wa,
        )
        self.assertIn("Actualicé tu expediente", resp.results[0].result)
        payload = manager.create_or_update_lead.call_args.args[0]
        self.assertEqual(payload["lead_id"], 1937)
        self.assertTrue(payload["preserve_salesperson"])
        manager.odoo.execute_kw.assert_not_called()
        wa.send_text_message.assert_called_once()

    def test_background_tasks_queues_whatsapp(self):
        from fastapi import BackgroundTasks

        from src.voice_gateway.vapi_bridge import handle_lead_payload

        manager = MagicMock()
        manager.create_or_update_lead.return_value = {
            "status": "created",
            "lead_id": 9,
            "branch": "periferico",
            "dry_run": False,
        }
        wa = MagicMock()
        tasks = BackgroundTasks()
        handle_lead_payload(
            {"name": "Sofía", "phone": "6140009999", "appointment_date": "hoy 17"},
            manager=manager,
            background_tasks=tasks,
            whatsapp_client=wa,
        )
        wa.send_text_message.assert_not_called()
        self.assertEqual(len(tasks.tasks), 1)
        tasks.tasks[0].func(*tasks.tasks[0].args, **tasks.tasks[0].kwargs)
        wa.send_text_message.assert_called_once()

    def test_crm_lead_alias_queues_whatsapp(self):
        """``/vapi/crm-lead`` shares ``handle_lead_payload`` → same WA queue."""
        from unittest.mock import patch

        from fastapi import BackgroundTasks

        from src.voice_gateway.vapi_bridge import handle_lead_payload

        manager = MagicMock()
        manager.create_or_update_lead.return_value = {
            "status": "created",
            "lead_id": 42,
            "branch": "periferico",
            "dry_run": False,
        }
        wa = MagicMock()
        tasks = BackgroundTasks()
        with patch("src.voice_gateway.vapi_bridge.logger") as log:
            handle_lead_payload(
                {
                    "name": "Ana",
                    "phone": "6141112222",
                    "appointment_date": "mañana 10",
                    # financing / trade-in / vehicle intentionally omitted
                },
                manager=manager,
                background_tasks=tasks,
                whatsapp_client=wa,
            )
            tasks.tasks[0].func(*tasks.tasks[0].args, **tasks.tasks[0].kwargs)
        wa.send_text_message.assert_called_once()
        warned = [
            c
            for c in log.warning.call_args_list
            if c.args and "dispatch_lead_whatsapp missing optional fields" in str(c.args[0])
        ]
        self.assertTrue(warned)
        blob = " ".join(str(a) for a in warned[0].args)
        self.assertIn("interested_vehicle", blob)
        self.assertIn("financing_summary", blob)
        self.assertIn("tradein_summary", blob)

    def test_context_lead_id_from_variable_values(self):
        from src.voice_gateway.vapi_bridge import handle_lead_payload

        manager = MagicMock()
        manager.create_or_update_lead.return_value = {
            "status": "updated",
            "lead_id": 88,
            "branch": "periferico",
            "dry_run": False,
        }
        wa = MagicMock()
        handle_lead_payload(
            {
                "message": {
                    "call": {
                        "assistantOverrides": {
                            "variableValues": {"lead_id": "88"},
                        }
                    },
                    "toolCalls": [
                        {
                            "id": "tc1",
                            "function": {
                                "name": "create_lead",
                                "arguments": {
                                    "name": "Sofía",
                                    "phone": "6140009999",
                                },
                            },
                        }
                    ],
                }
            },
            manager=manager,
            whatsapp_client=wa,
        )
        payload = manager.create_or_update_lead.call_args.args[0]
        self.assertEqual(payload["lead_id"], 88)


if __name__ == "__main__":
    unittest.main()
