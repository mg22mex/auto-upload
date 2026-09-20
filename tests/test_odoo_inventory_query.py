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
    build_inventory_domain,
    cache_clear,
    cache_get,
    cache_key,
    cache_set,
    query_inventory,
)


class TestBuildDomain(unittest.TestCase):
    def setUp(self):
        cache_clear()

    def test_available_filter_and_cap_fields(self):
        from src.odoo_sync.inventory import VEHICLE_STATE_AVAILABLE, VEHICLE_STATE_EXCLUDED

        domain = build_inventory_domain(brand="Ford", max_price=500_000, year=2020)
        self.assertIn(("sale_ok", "=", True), domain)
        self.assertIn(("active", "=", True), domain)
        self.assertIn(("default_code", "!=", False), domain)
        self.assertIn(("name", "ilike", "Ford"), domain)
        self.assertIn(("x_studio_state", "in", list(VEHICLE_STATE_AVAILABLE)), domain)
        self.assertIn(("x_studio_state", "not in", list(VEHICLE_STATE_EXCLUDED)), domain)
        self.assertEqual(RESULT_LIMIT, 3)

    def test_available_only_false_skips_state(self):
        domain = build_inventory_domain(brand="Toyota", available_only=False)
        self.assertIn(("sale_ok", "=", True), domain)
        self.assertTrue(all(term[0] != "x_studio_state" for term in domain if isinstance(term, tuple)))

    def test_query_inventory_limit_three(self):
        execute_kw = MagicMock(return_value=[{"id": 1, "name": "A", "list_price": 1}])
        rows = query_inventory(execute_kw, brand="Mazda", limit=99)
        self.assertEqual(len(rows), 1)
        opts = execute_kw.call_args.args[3]
        self.assertEqual(opts["limit"], 3)
        self.assertEqual(opts["fields"], ["id", "name", "list_price", "default_code"])

    def test_ttl_cache_serves_second_call(self):
        execute_kw = MagicMock(
            return_value=[{"id": 9, "name": "RAV4", "list_price": 1, "default_code": "r"}]
        )
        with patch.dict("os.environ", {"ODOO_INVENTORY_CACHE_TTL_SEC": "900"}):
            first = query_inventory(execute_kw, brand="RAV4", use_cache=True)
            second = query_inventory(execute_kw, brand="RAV4", use_cache=True)
            key = cache_key(brand="RAV4", limit=3)
            self.assertIsNotNone(cache_get(key))
            self.assertEqual(cache_get(key)[0]["name"], "RAV4")
        self.assertEqual(first, second)
        self.assertEqual(execute_kw.call_count, 1)

    def test_default_ttl_zero_skips_cache(self):
        execute_kw = MagicMock(
            return_value=[{"id": 1, "name": "A", "list_price": 1, "default_code": "a"}]
        )
        with patch.dict("os.environ", {"ODOO_INVENTORY_CACHE_TTL_SEC": "0"}):
            query_inventory(execute_kw, brand="A", use_cache=True)
            query_inventory(execute_kw, brand="A", use_cache=True)
        self.assertEqual(execute_kw.call_count, 2)


if __name__ == "__main__":
    unittest.main()
