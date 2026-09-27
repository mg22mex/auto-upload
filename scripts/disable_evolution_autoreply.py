#!/usr/bin/env python3
"""Disable Evolution built-in bots and point webhooks at Autosell FastAPI.

Clears Typebot / OpenAI / EvolutionBot integrations per instance and sets
``MESSAGES_UPSERT`` → WhatsApp qualification webhook (Beatriz / Vapi).

Examples:
  PYTHONPATH=. python scripts/disable_evolution_autoreply.py
  PYTHONPATH=. python scripts/disable_evolution_autoreply.py \\
      --instances autosell_main,autosell_periferico \\
      --webhook-url http://host.docker.internal:8080/webhook/whatsapp
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

DEFAULT_INSTANCES = (
    "autosell_main",
    "autosell_periferico",
    "autosell_san_felipe",
)
# Docker Evolution → host FastAPI (voice_gateway.webhook on :8080).
DEFAULT_WEBHOOK = "http://host.docker.internal:8080/webhook/whatsapp"
BOT_FIND_PATHS = (
    ("typebot", "/typebot/find/{instance}"),
    ("openai", "/openai/find/{instance}"),
    ("evolutionBot", "/evolutionBot/find/{instance}"),
    ("chatbot", "/chatbot/find/{instance}"),
)
BOT_DELETE_TEMPLATES = (
    "/typebot/delete/{instance}/{bot_id}",
    "/openai/delete/{instance}/{bot_id}",
    "/evolutionBot/delete/{instance}/{bot_id}",
    "/chatbot/delete/{instance}/{bot_id}",
)


def _base_url() -> str:
    return (
        os.getenv("EVOLUTION_SERVER_URL")
        or os.getenv("WHATSAPP_API_URL")
        or "http://127.0.0.1:8082"
    ).rstrip("/")


def _api_key() -> str:
    return (os.getenv("WHATSAPP_API_KEY") or "").strip()


def _request(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> tuple[int, Any]:
    url = f"{_base_url()}{path}"
    data = None
    headers = {"apikey": _api_key(), "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                parsed: Any = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                parsed = raw
            return int(resp.status), parsed
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw else {"error": str(exc)}
        except json.JSONDecodeError:
            parsed = raw or str(exc)
        return int(exc.code), parsed
    except URLError as exc:
        return 0, {"error": str(exc.reason if hasattr(exc, "reason") else exc)}


def _list_instances() -> list[str]:
    code, payload = _request("GET", "/instance/fetchInstances")
    names: list[str] = []
    if code != 200:
        return names
    rows = payload if isinstance(payload, list) else []
    if isinstance(payload, dict):
        rows = payload.get("instance") or payload.get("instances") or []
        if isinstance(rows, dict):
            rows = [rows]
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        inst = row.get("instance") if isinstance(row.get("instance"), dict) else row
        name = (
            (inst or {}).get("instanceName")
            or (inst or {}).get("name")
            or row.get("instanceName")
            or row.get("name")
        )
        if name:
            names.append(str(name))
    return names


def _extract_bot_ids(payload: Any) -> list[str]:
    ids: list[str] = []
    rows: list[Any]
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        for key in ("bots", "typebots", "openaiBots", "evolutionBot", "records"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
        else:
            rows = [payload] if payload.get("id") or payload.get("typebotId") else []
    else:
        rows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        bot_id = row.get("id") or row.get("typebotId") or row.get("openaiBotId")
        if bot_id:
            ids.append(str(bot_id))
    return ids


def _disable_bots(instance: str) -> dict[str, Any]:
    result: dict[str, Any] = {"find": {}, "deleted": []}
    for label, template in BOT_FIND_PATHS:
        code, payload = _request("GET", template.format(instance=instance))
        result["find"][label] = {"status": code, "body": payload}
        if code != 200:
            continue
        for bot_id in _extract_bot_ids(payload):
            for del_tmpl in BOT_DELETE_TEMPLATES:
                if label not in del_tmpl:
                    continue
                dcode, dbody = _request(
                    "DELETE", del_tmpl.format(instance=instance, bot_id=bot_id)
                )
                result["deleted"].append(
                    {"kind": label, "id": bot_id, "status": dcode, "body": dbody}
                )
    # Soft-disable typebot settings if endpoint exists.
    _request(
        "POST",
        f"/typebot/settings/{instance}",
        body={
            "expire": 0,
            "keywordFinish": "#SORRY_DISABLED",
            "delayMessage": 0,
            "unknownMessage": "",
            "listeningFromMe": False,
            "stopBotFromMe": True,
            "keepOpen": False,
            "debounceTime": 0,
            "ignoreJids": ["@g.us"],
            "typebotIdFallback": "",
        },
    )
    return result


def _set_webhook(instance: str, webhook_url: str) -> dict[str, Any]:
    # Evolution v2 accepts either flat or nested ``webhook`` payloads.
    payloads = [
        {
            "enabled": True,
            "url": webhook_url,
            "webhookByEvents": False,
            "webhookBase64": False,
            "events": ["MESSAGES_UPSERT"],
        },
        {
            "webhook": {
                "enabled": True,
                "url": webhook_url,
                "webhookByEvents": False,
                "webhookBase64": False,
                "events": ["MESSAGES_UPSERT"],
            }
        },
    ]
    last: dict[str, Any] = {}
    for body in payloads:
        code, resp = _request("POST", f"/webhook/set/{instance}", body=body)
        last = {"status": code, "body": resp, "request": body}
        if code in {200, 201}:
            break
    find_code, find_body = _request("GET", f"/webhook/find/{instance}")
    last["find"] = {"status": find_code, "body": find_body}
    return last


def _set_instance_settings(instance: str) -> dict[str, Any]:
    """Clear reject-call / msg templates that can look like auto-replies."""
    body = {
        "rejectCall": False,
        "msgCall": "",
        "groupsIgnore": True,
        "alwaysOnline": False,
        "readMessages": False,
        "readStatus": False,
        "syncFullHistory": False,
    }
    code, resp = _request("POST", f"/settings/set/{instance}", body=body)
    return {"status": code, "body": resp, "request": body}


def configure_instance(instance: str, webhook_url: str) -> dict[str, Any]:
    return {
        "instance": instance,
        "settings": _set_instance_settings(instance),
        "bots": _disable_bots(instance),
        "webhook": _set_webhook(instance, webhook_url),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--instances",
        default="",
        help="Comma-separated instance names (default: discover + known Autosell names)",
    )
    parser.add_argument(
        "--webhook-url",
        default=os.getenv("WHATSAPP_WEBHOOK_URL") or DEFAULT_WEBHOOK,
        help=f"Webhook URL (default: {DEFAULT_WEBHOOK})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List targets only; do not PATCH Evolution",
    )
    args = parser.parse_args()

    if not _api_key():
        print("ERROR: WHATSAPP_API_KEY missing", file=sys.stderr)
        return 2

    discovered = _list_instances()
    requested = [x.strip() for x in args.instances.split(",") if x.strip()]
    targets = requested or sorted(set(discovered) | set(DEFAULT_INSTANCES))
    if args.dry_run:
        print(
            json.dumps(
                {
                    "base": _base_url(),
                    "discovered": discovered,
                    "targets": targets,
                    "webhook_url": args.webhook_url,
                },
                indent=2,
            )
        )
        return 0

    report = {
        "base": _base_url(),
        "discovered": discovered,
        "webhook_url": args.webhook_url,
        "results": [configure_instance(name, args.webhook_url) for name in targets],
    }
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    # Non-zero only when every target hard-failed webhook set (instance missing is OK).
    ok_any = any(
        (r.get("webhook") or {}).get("status") in {200, 201}
        for r in report["results"]
    )
    if discovered and not ok_any:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
