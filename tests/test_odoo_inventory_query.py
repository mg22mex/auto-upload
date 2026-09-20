"""Unit tests — fast Odoo inventory domain / query_inventory."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.odoo_sync.inventory import (
    RESULT_LIMIT,
    VEHICLE_STATE_AVAILABLE,
    VEHICLE_STATE_EXCLUDED,
    build_inventory_domain,
    cache_clear,
    cache_get,
    cache_key,
    query_inventory,
    reset_state_field_cache,
)


class TestBuildDomain(unittest.TestCase):
    def setUp(self):
        cache_clear()
        reset_state_field_cache()

    def test_available_filter_and_cap_fields(self):
        domain = build_inventory_domain(brand="Ford", max_price=500_000, year=2020)
        self.assertIn(("sale_ok", "=", True), domain)
        self.assertIn(("active", "=", True), domain)
        self.assertIn(("default_code", "!=", False), domain)
        self.assertIn(("list_price", ">=", 10_000.0), domain)
        self.assertIn(("name", "ilike", "Ford"), domain)
        self.assertIn(("x_studio_state", "in", list(VEHICLE_STATE_AVAILABLE)), domain)
        self.assertIn(("x_studio_state", "not in", list(VEHICLE_STATE_EXCLUDED)), domain)
        self.assertEqual(RESULT_LIMIT, 3)

    def test_available_only_false_skips_state(self):
        domain = build_inventory_domain(brand="Toyota", available_only=False)
        self.assertIn(("sale_ok", "=", True), domain)
        self.assertTrue(
            all(
                term[0] != "x_studio_state"
                for term in domain
                if isinstance(term, tuple)
            )
        )

    def test_no_state_field_published_stock_domain(self):
        domain = build_inventory_domain(
            brand="Nissan", available_only=True, state_field=None
        )
        self.assertIn(("sale_ok", "=", True), domain)
        self.assertIn(("active", "=", True), domain)
        self.assertIn(("default_code", "!=", False), domain)
        self.assertTrue(
            all(
                not (isinstance(t, tuple) and t[0] == "x_studio_state")
                for t in domain
            )
        )

    def test_query_inventory_limit_three(self):
        execute_kw = MagicMock(
            side_effect=[
                {"x_studio_state": {"type": "selection"}},  # fields_get
                [{"id": 1, "name": "A", "list_price": 1}],
            ]
        )
        rows = query_inventory(execute_kw, brand="Mazda", limit=99)
        self.assertEqual(len(rows), 1)
        search_call = execute_kw.call_args_list[-1]
        opts = search_call.args[3]
        self.assertEqual(opts["limit"], 3)
        self.assertEqual(opts["fields"], ["id", "name", "list_price", "default_code"])

    def test_missing_state_field_does_not_widen_domain(self):
        """No Studio state → published filters only; never drop active/SKU."""
        execute_kw = MagicMock(
            side_effect=[
                {},  # fields_get — no state fields
                [
                    {
                        "id": 577,
                        "name": "Versa Sense - Nissan 2019",
                        "list_price": 219000.0,
                        "default_code": "obj818",
                    }
                ],
            ]
        )
        rows = query_inventory(execute_kw, brand="Nissan", use_cache=False)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["list_price"], 219000.0)
        domain = execute_kw.call_args_list[-1].args[2][0]
        self.assertIn(("sale_ok", "=", True), domain)
        self.assertIn(("active", "=", True), domain)
        self.assertIn(("default_code", "!=", False), domain)
        self.assertFalse(
            any(
                isinstance(t, tuple) and t[0] in ("x_studio_state", "state")
                for t in domain
            )
        )
        # Exactly one search_read after fields_get — no soft unfiltered retries.
        search_reads = [
            c
            for c in execute_kw.call_args_list
            if c.args[1] == "search_read"
        ]
        self.assertEqual(len(search_reads), 1)

    def test_ttl_cache_serves_second_call(self):
        execute_kw = MagicMock(
            side_effect=[
                {"x_studio_state": {"type": "selection"}},
                [{"id": 9, "name": "RAV4", "list_price": 1, "default_code": "r"}],
            ]
        )
        with patch.dict("os.environ", {"ODOO_INVENTORY_CACHE_TTL_SEC": "900"}):
            first = query_inventory(execute_kw, brand="RAV4", use_cache=True)
            second = query_inventory(execute_kw, brand="RAV4", use_cache=True)
            key = cache_key(brand="RAV4", limit=3)
            self.assertIsNotNone(cache_get(key))
            self.assertEqual(cache_get(key)[0]["name"], "RAV4")
        self.assertEqual(first, second)
        # fields_get + one search_read only
        self.assertEqual(execute_kw.call_count, 2)

    def test_default_ttl_zero_skips_cache(self):
        execute_kw = MagicMock(
            side_effect=[
                {},
                [{"id": 1, "name": "A", "list_price": 1, "default_code": "a"}],
                [{"id": 1, "name": "A", "list_price": 1, "default_code": "a"}],
            ]
        )
        with patch.dict("os.environ", {"ODOO_INVENTORY_CACHE_TTL_SEC": "0"}):
            query_inventory(execute_kw, brand="A", use_cache=True)
            query_inventory(execute_kw, brand="A", use_cache=True)
        search_reads = [
            c for c in execute_kw.call_args_list if c.args[1] == "search_read"
        ]
        self.assertEqual(len(search_reads), 2)


if __name__ == "__main__":
    unittest.main()
