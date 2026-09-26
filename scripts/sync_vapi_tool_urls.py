#!/usr/bin/env python3
"""Sync Vapi custom-tool ``server.url`` bases to the live Cloudflare quick tunnel.

Reads the current ``*.trycloudflare.com`` URL from a cloudflared log (or
``--base-url``), then PATCHes each tool so paths stay::

    {base}/vapi/inventory
    {base}/vapi/crm-lead
    {base}/vapi/financing
    {base}/vapi/tradein

Auth: ``VAPI_API_KEY`` or ``VAPI_TOKEN`` (Bearer).

Tool IDs (optional — auto-discovered via GET /tool when unset)::

    VAPI_TOOL_INVENTORY_ID
    VAPI_TOOL_CRM_LEAD_ID
    VAPI_TOOL_FINANCING_ID
    VAPI_TOOL_TRADEIN_ID

Usage::

    .venv/bin/python scripts/sync_vapi_tool_urls.py --from-log /tmp/oracle_quick_tunnel.log
    .venv/bin/python scripts/sync_vapi_tool_urls.py --base-url https://….trycloudflare.com
    .venv/bin/python scripts/sync_vapi_tool_urls.py --dry-run --from-log …
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover

    def load_dotenv(*_a: Any, **_k: Any) -> bool:
        return False

DEFAULT_VAPI_BASE = "https://api.vapi.ai"
URL_RE = re.compile(r"https://[a-zA-Z0-9.-]+\.trycloudflare\.com")

# path suffix → env var for explicit tool id
TOOL_PATHS: tuple[tuple[str, str], ...] = (
    ("/vapi/inventory", "VAPI_TOOL_INVENTORY_ID"),
    ("/vapi/crm-lead", "VAPI_TOOL_CRM_LEAD_ID"),
    ("/vapi/financing", "VAPI_TOOL_FINANCING_ID"),
    ("/vapi/tradein", "VAPI_TOOL_TRADEIN_ID"),
)


def _api_key() -> str:
    return (os.getenv("VAPI_API_KEY") or os.getenv("VAPI_TOKEN") or "").strip()


def _vapi_base() -> str:
    return (os.getenv("VAPI_BASE_URL") or DEFAULT_VAPI_BASE).strip().rstrip("/")


def extract_tunnel_url(text: str) -> str | None:
    matches = URL_RE.findall(text or "")
    return matches[-1] if matches else None


def wait_url_from_log(log_path: Path, *, timeout_sec: float = 60.0) -> str:
    import time

    deadline = time.monotonic() + max(1.0, timeout_sec)
    last_size = -1
    while time.monotonic() < deadline:
        if log_path.is_file():
            raw = log_path.read_text(encoding="utf-8", errors="replace")
            url = extract_tunnel_url(raw)
            if url:
                return url
            size = log_path.stat().st_size
            if size != last_size:
                last_size = size
        time.sleep(0.5)
    raise TimeoutError(f"no trycloudflare.com URL in {log_path} within {timeout_sec}s")


def _http_json(
    method: str,
    url: str,
    *,
    api_key: str,
    body: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method.upper(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            # Cloudflare on api.vapi.ai bans the default Python-urllib UA (1010).
            "User-Agent": "Autosell-vapi-sync/1.0 (+https://autosell.mx)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        raise RuntimeError(f"{method} {url} → HTTP {exc.code}: {detail}") from exc


def list_tools(api_key: str, base: str) -> list[dict[str, Any]]:
    payload = _http_json("GET", f"{base}/tool", api_key=api_key)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("results", "data", "tools"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return rows
    raise RuntimeError(f"unexpected GET /tool payload type: {type(payload)!r}")


def resolve_tool_ids(
    api_key: str,
    base: str,
    *,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Map path → tool id using env overrides, else URL/path or function name."""
    out: dict[str, str] = {}
    for path, env_name in TOOL_PATHS:
        explicit = (os.getenv(env_name) or "").strip()
        if explicit:
            out[path] = explicit

    missing = [p for p, _ in TOOL_PATHS if p not in out]
    if not missing:
        return out

    rows = tools if tools is not None else list_tools(api_key, base)
    by_suffix: dict[str, str] = {}
    by_name: dict[str, str] = {}
    name_aliases = {
        "/vapi/inventory": ("query_inventory", "get_inventory"),
        "/vapi/crm-lead": ("create_crm_lead", "create_lead", "crm_lead"),
        "/vapi/financing": ("calculate_financing", "get_financing"),
        "/vapi/tradein": ("estimate_tradein", "get_tradein", "tradein"),
    }
    for row in rows:
        tid = str(row.get("id") or "").strip()
        if not tid:
            continue
        url = str((row.get("server") or {}).get("url") or "")
        for path, _ in TOOL_PATHS:
            if url.rstrip("/").endswith(path):
                by_suffix[path] = tid
        fn = str((row.get("function") or {}).get("name") or "").strip().lower()
        if fn:
            by_name[fn] = tid

    for path in missing:
        if path in by_suffix:
            out[path] = by_suffix[path]
            continue
        for alias in name_aliases.get(path, ()):
            if alias.lower() in by_name:
                out[path] = by_name[alias.lower()]
                break

    still = [p for p, _ in TOOL_PATHS if p not in out]
    if still:
        raise RuntimeError(
            "could not resolve Vapi tool ids for: "
            + ", ".join(still)
            + " — set VAPI_TOOL_*_ID in .env or ensure tools exist in the org"
        )
    return out


def patch_tool_server_url(
    api_key: str,
    base: str,
    tool_id: str,
    new_url: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    current = _http_json("GET", f"{base}/tool/{tool_id}", api_key=api_key)
    if not isinstance(current, dict):
        raise RuntimeError(f"GET /tool/{tool_id} returned non-object")
    old_server = dict(current.get("server") or {})
    old_url = str(old_server.get("url") or "")
    if old_url == new_url:
        return {"id": tool_id, "url": new_url, "changed": False, "dry_run": dry_run}
    merged = {**old_server, "url": new_url}
    if dry_run:
        return {
            "id": tool_id,
            "url": new_url,
            "previous": old_url,
            "changed": True,
            "dry_run": True,
        }
    updated = _http_json(
        "PATCH",
        f"{base}/tool/{tool_id}",
        api_key=api_key,
        body={"server": merged},
    )
    verify_url = ""
    if isinstance(updated, dict):
        verify_url = str((updated.get("server") or {}).get("url") or "")
    return {
        "id": tool_id,
        "url": verify_url or new_url,
        "previous": old_url,
        "changed": True,
        "dry_run": False,
    }


def sync_tools(
    tunnel_base: str,
    *,
    dry_run: bool = False,
    api_key: str | None = None,
    vapi_base: str | None = None,
) -> list[dict[str, Any]]:
    key = (api_key or _api_key()).strip()
    if not key:
        raise RuntimeError("VAPI_API_KEY or VAPI_TOKEN required")
    api = (vapi_base or _vapi_base()).rstrip("/")
    base = tunnel_base.rstrip("/")
    if not URL_RE.fullmatch(base) and "trycloudflare.com" not in base:
        # Allow named hostnames too (vapi.autosell.mx) when explicitly passed.
        if not base.startswith("https://"):
            raise RuntimeError(f"invalid tunnel base URL: {tunnel_base!r}")

    ids = resolve_tool_ids(key, api)
    results: list[dict[str, Any]] = []
    for path, _env in TOOL_PATHS:
        tool_id = ids[path]
        new_url = f"{base}{path}"
        result = patch_tool_server_url(
            key, api, tool_id, new_url, dry_run=dry_run
        )
        result["path"] = path
        results.append(result)
        status = "DRY" if dry_run and result.get("changed") else (
            "OK" if result.get("changed") else "SKIP"
        )
        print(f"{status} {path} → {result.get('url')}")
    return results


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-log", type=Path, help="cloudflared log with trycloudflare URL")
    src.add_argument("--base-url", help="explicit https://….trycloudflare.com base")
    parser.add_argument(
        "--wait-sec",
        type=float,
        default=60.0,
        help="seconds to wait for URL in --from-log (default 60)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.base_url:
        base = args.base_url.strip().rstrip("/")
    else:
        base = wait_url_from_log(args.from_log, timeout_sec=args.wait_sec)
    print(f"TUNNEL_BASE={base}", flush=True)
    sync_tools(base, dry_run=bool(args.dry_run))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
