#!/usr/bin/env python3
"""Configure Vapi ``query_inventory`` + wire tools onto Beatriz.

Fixes silence-timeouts when:
  1. tool was ``async: true`` with ``request-start`` filler, or
  2. assistant had **no** ``toolIds`` (prompt named tools that never ran).

Always re-sends ``server`` so Vapi does not wipe the webhook URL on PATCH.

Usage::

  .venv/bin/python scripts/configure_vapi_inventory_tool.py
  .venv/bin/python scripts/configure_vapi_inventory_tool.py --dry-run
  .venv/bin/python scripts/configure_vapi_inventory_tool.py --skip-assistant
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

DEFAULT_ASSISTANT_ID = "7b4bc492-b94b-40bc-a79c-754fe48c6f9b"

ANTI_SILENCE_BLOCK = """

## PROHIBIDO HABLAR ANTES DEL TOOL (CRÍTICO)
- Cuando el cliente pida un vehículo, llama INMEDIATAMENTE a query_inventory con brand y model.
- En el mismo turno del tool call, tu mensaje de texto DEBE estar vacío. Cero palabras. Solo el tool call.
- Frases prohibidas (nunca): "un momento", "dame un momento", "déjame consultar", "ahora mismo consulto", "verifico", "consulto el inventario", "espera un segundo".
- Flujo obligatorio: (1) tool call silencioso → (2) recibes JSON → (3) hablas modelo + precio en palabras + sucursal + pregunta de cita.
"""

INVENTORY_FUNCTION: dict[str, Any] = {
    "name": "query_inventory",
    "description": (
        "Busca vehículos disponibles en inventario Autosell (Odoo). "
        "Usa brand y model juntos cuando el cliente pide un auto concreto "
        "(ej. Toyota Corolla). Devuelve JSON compacto con precio y ubicación. "
        "Espera el resultado antes de hablar; no digas frases de espera."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "brand": {
                "type": "string",
                "description": "Marca (ej. Toyota, Mazda, Nissan)",
            },
            "model": {
                "type": "string",
                "description": "Modelo (ej. Corolla, CX5, Versa)",
            },
            "year": {
                "type": "integer",
                "description": "Año deseado (ej. 2020, 2022)",
            },
            "max_price": {
                "type": "number",
                "description": "Presupuesto máximo en pesos mexicanos",
            },
        },
        "required": [],
    },
}


def _load_sync_mod():
    path = ROOT / "scripts" / "sync_vapi_tool_urls.py"
    spec = importlib.util.spec_from_file_location("sync_vapi_tool_urls", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def configure_inventory_tool(*, dry_run: bool = False) -> dict[str, Any]:
    sync = _load_sync_mod()
    key = sync._api_key()
    if not key:
        raise RuntimeError("VAPI_API_KEY or VAPI_TOKEN required")
    api = sync._vapi_base()
    ids = sync.resolve_tool_ids(key, api)
    tool_id = ids["/vapi/inventory"]
    current = sync._http_json("GET", f"{api}/tool/{tool_id}", api_key=key)
    if not isinstance(current, dict):
        raise RuntimeError(f"unexpected tool payload: {type(current)}")

    old_fn = dict(current.get("function") or {})
    new_fn = {
        **old_fn,
        **INVENTORY_FUNCTION,
        "parameters": INVENTORY_FUNCTION["parameters"],
    }
    old_server = dict(current.get("server") or {})
    server_url = (
        (os.getenv("VAPI_INVENTORY_SERVER_URL") or "").strip()
        or str(old_server.get("url") or "").strip()
    )
    if not server_url:
        raise RuntimeError(
            "server.url missing — set VAPI_INVENTORY_SERVER_URL or run "
            "scripts/sync_vapi_tool_urls.py first"
        )
    server = {
        **old_server,
        "url": server_url,
        "timeoutSeconds": int(old_server.get("timeoutSeconds") or 20),
    }

    patch: dict[str, Any] = {
        "type": current.get("type") or "function",
        "async": False,
        "function": new_fn,
        "messages": [],
        "server": server,
    }

    before = {
        "async": current.get("async"),
        "messages": current.get("messages"),
        "function_keys": list(
            (current.get("function") or {})
            .get("parameters", {})
            .get("properties", {})
            .keys()
        ),
        "server_url": (current.get("server") or {}).get("url"),
    }
    print("TOOL BEFORE", json.dumps(before, ensure_ascii=False))
    if dry_run:
        print("DRY tool patch", json.dumps(patch, ensure_ascii=False)[:900])
        return {"id": tool_id, "dry_run": True, "before": before, "patch": patch}

    updated = sync._http_json(
        "PATCH",
        f"{api}/tool/{tool_id}",
        api_key=key,
        body=patch,
    )
    after = {
        "async": (updated or {}).get("async") if isinstance(updated, dict) else None,
        "messages": (updated or {}).get("messages") if isinstance(updated, dict) else None,
        "function_keys": list(
            ((updated or {}).get("function") or {})
            .get("parameters", {})
            .get("properties", {})
            .keys()
        )
        if isinstance(updated, dict)
        else [],
        "server_url": ((updated or {}).get("server") or {}).get("url")
        if isinstance(updated, dict)
        else None,
    }
    print("TOOL AFTER", json.dumps(after, ensure_ascii=False))
    if after.get("async") is not False:
        raise RuntimeError(f"async not cleared: {after.get('async')!r}")
    if after.get("messages"):
        raise RuntimeError(f"messages still set: {after.get('messages')!r}")
    if "model" not in (after.get("function_keys") or []):
        raise RuntimeError("model parameter missing after PATCH")
    if not after.get("server_url"):
        raise RuntimeError("server.url missing after PATCH")
    return {"id": tool_id, "dry_run": False, "before": before, "after": after, "all_tool_ids": list(ids.values())}


def configure_assistant_tools(
    *,
    dry_run: bool = False,
    assistant_id: str | None = None,
    tool_ids: list[str] | None = None,
) -> dict[str, Any]:
    sync = _load_sync_mod()
    key = sync._api_key()
    api = sync._vapi_base()
    aid = (
        (assistant_id or "").strip()
        or (os.getenv("VAPI_ASSISTANT_ID") or "").strip()
        or DEFAULT_ASSISTANT_ID
    )
    if tool_ids is None:
        tool_ids = list(sync.resolve_tool_ids(key, api).values())

    current = sync._http_json("GET", f"{api}/assistant/{aid}", api_key=key)
    if not isinstance(current, dict):
        raise RuntimeError(f"unexpected assistant payload: {type(current)}")
    model = dict(current.get("model") or {})
    msgs = list(model.get("messages") or [])
    sys_idx = next((i for i, m in enumerate(msgs) if m.get("role") == "system"), None)
    if sys_idx is None:
        raise RuntimeError("assistant has no system message")
    sys_content = msgs[sys_idx].get("content") or ""
    if "PROHIBIDO HABLAR ANTES DEL TOOL" not in sys_content:
        sys_content = sys_content.rstrip() + ANTI_SILENCE_BLOCK
    msgs[sys_idx] = {**msgs[sys_idx], "content": sys_content}
    model["messages"] = msgs
    model["toolIds"] = tool_ids

    before = {
        "toolIds": (current.get("model") or {}).get("toolIds"),
        "has_anti_silence": "PROHIBIDO HABLAR ANTES DEL TOOL"
        in (((current.get("model") or {}).get("messages") or [{}])[0].get("content") or ""),
    }
    print("ASSISTANT BEFORE", json.dumps(before, ensure_ascii=False))
    if dry_run:
        return {"id": aid, "dry_run": True, "before": before, "toolIds": tool_ids}

    updated = sync._http_json(
        "PATCH",
        f"{api}/assistant/{aid}",
        api_key=key,
        body={"model": model},
    )
    um = (updated or {}).get("model") or {}
    after = {
        "toolIds": um.get("toolIds"),
        "has_anti_silence": "PROHIBIDO HABLAR ANTES DEL TOOL"
        in ((um.get("messages") or [{}])[0].get("content") or ""),
    }
    print("ASSISTANT AFTER", json.dumps(after, ensure_ascii=False))
    if set(after.get("toolIds") or []) != set(tool_ids):
        raise RuntimeError(f"toolIds mismatch: {after.get('toolIds')!r}")
    if not after.get("has_anti_silence"):
        raise RuntimeError("anti-silence block missing after PATCH")
    return {"id": aid, "dry_run": False, "before": before, "after": after}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-assistant", action="store_true")
    parser.add_argument("--assistant-id", default="")
    args = parser.parse_args(argv)
    tool_result = configure_inventory_tool(dry_run=bool(args.dry_run))
    if not args.skip_assistant:
        configure_assistant_tools(
            dry_run=bool(args.dry_run),
            assistant_id=args.assistant_id or None,
            tool_ids=tool_result.get("all_tool_ids"),
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
