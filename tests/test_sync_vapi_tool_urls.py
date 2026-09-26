"""Unit tests — sync_vapi_tool_urls helpers (mocked HTTP)."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sync_vapi_tool_urls.py"


def _load():
    spec = importlib.util.spec_from_file_location("sync_vapi_tool_urls", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sync_vapi_tool_urls"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestSyncVapiUrls(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load()

    def test_extract_tunnel_url_last_match(self):
        text = (
            "old https://respectively-stock-navigation-ari.trycloudflare.com\n"
            "new https://respectively-stock-navigation-mrs.trycloudflare.com\n"
        )
        self.assertEqual(
            self.mod.extract_tunnel_url(text),
            "https://respectively-stock-navigation-mrs.trycloudflare.com",
        )

    def test_resolve_tool_ids_from_env(self):
        with patch.dict(
            "os.environ",
            {
                "VAPI_TOOL_INVENTORY_ID": "inv",
                "VAPI_TOOL_CRM_LEAD_ID": "crm",
                "VAPI_TOOL_FINANCING_ID": "fin",
                "VAPI_TOOL_TRADEIN_ID": "ti",
            },
            clear=False,
        ):
            ids = self.mod.resolve_tool_ids("k", "https://api.vapi.ai", tools=[])
        self.assertEqual(
            ids,
            {
                "/vapi/inventory": "inv",
                "/vapi/crm-lead": "crm",
                "/vapi/financing": "fin",
                "/vapi/tradein": "ti",
            },
        )

    def test_resolve_tool_ids_from_list(self):
        tools = [
            {
                "id": "a",
                "function": {"name": "query_inventory"},
                "server": {"url": "https://old.trycloudflare.com/vapi/inventory"},
            },
            {
                "id": "b",
                "function": {"name": "create_crm_lead"},
                "server": {"url": "https://old.trycloudflare.com/vapi/crm-lead"},
            },
            {
                "id": "c",
                "function": {"name": "calculate_financing"},
                "server": {"url": "https://old.trycloudflare.com/vapi/financing"},
            },
            {
                "id": "d",
                "function": {"name": "estimate_tradein"},
                "server": {"url": "https://old.trycloudflare.com/vapi/tradein"},
            },
        ]
        with patch.dict("os.environ", {}, clear=False):
            for key in (
                "VAPI_TOOL_INVENTORY_ID",
                "VAPI_TOOL_CRM_LEAD_ID",
                "VAPI_TOOL_FINANCING_ID",
                "VAPI_TOOL_TRADEIN_ID",
            ):
                # Ensure env overrides do not leak from developer shell.
                pass
        env_clear = {
            k: ""
            for k in (
                "VAPI_TOOL_INVENTORY_ID",
                "VAPI_TOOL_CRM_LEAD_ID",
                "VAPI_TOOL_FINANCING_ID",
                "VAPI_TOOL_TRADEIN_ID",
            )
        }
        with patch.dict("os.environ", env_clear, clear=False):
            ids = self.mod.resolve_tool_ids("k", "https://api.vapi.ai", tools=tools)
        self.assertEqual(ids["/vapi/inventory"], "a")
        self.assertEqual(ids["/vapi/crm-lead"], "b")

    def test_sync_tools_dry_run_patches(self):
        tools = [
            {
                "id": "inv",
                "function": {"name": "query_inventory"},
                "server": {"url": "https://old.trycloudflare.com/vapi/inventory"},
            },
            {
                "id": "crm",
                "function": {"name": "create_crm_lead"},
                "server": {"url": "https://old.trycloudflare.com/vapi/crm-lead"},
            },
            {
                "id": "fin",
                "function": {"name": "calculate_financing"},
                "server": {"url": "https://old.trycloudflare.com/vapi/financing"},
            },
            {
                "id": "ti",
                "function": {"name": "estimate_tradein"},
                "server": {"url": "https://old.trycloudflare.com/vapi/tradein"},
            },
        ]

        def fake_http(method, url, *, api_key, body=None, timeout=30.0):
            if method == "GET" and url.endswith("/tool"):
                return tools
            if method == "GET" and "/tool/" in url:
                tid = url.rsplit("/", 1)[-1]
                return next(t for t in tools if t["id"] == tid)
            raise AssertionError(f"unexpected {method} {url}")

        with patch.object(self.mod, "_http_json", side_effect=fake_http):
            with patch.dict(
                "os.environ",
                {
                    "VAPI_API_KEY": "secret",
                    "VAPI_TOOL_INVENTORY_ID": "",
                    "VAPI_TOOL_CRM_LEAD_ID": "",
                    "VAPI_TOOL_FINANCING_ID": "",
                    "VAPI_TOOL_TRADEIN_ID": "",
                },
                clear=False,
            ):
                results = self.mod.sync_tools(
                    "https://new.trycloudflare.com",
                    dry_run=True,
                    api_key="secret",
                )
        self.assertEqual(len(results), 4)
        self.assertTrue(all(r["changed"] and r["dry_run"] for r in results))
        self.assertTrue(
            all(r["url"].startswith("https://new.trycloudflare.com/vapi/") for r in results)
        )


if __name__ == "__main__":
    unittest.main()
