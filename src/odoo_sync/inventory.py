"""Fast Odoo vehicle inventory lookups for Vapi / CRM tools.

Prefer indexed scalar fields only (no ``categ_id.name`` relational browse).
Hard-cap result rows at 3 for voice TTS latency.
Includes a small in-process TTL cache for repeated brand queries.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

# Voice tools must stay under Vapi timeouts — never fetch large result sets.
RESULT_LIMIT = 3
CACHE_TTL_SEC = 15 * 60
CACHE_MAX_ENTRIES = 128

INVENTORY_FIELDS = ("id", "name", "list_price", "default_code")

# Studio / custom selection labels used by inventory sync (EN + ES).
VEHICLE_STATE_AVAILABLE = ("available", "Available", "Disponible", "disponible")
VEHICLE_STATE_FIELDS = (
    "x_studio_state",
    "x_studio_estatus",
    "x_vehicle_state",
    "state",
)

_cache_lock = threading.Lock()
_cache: dict[tuple[Any, ...], tuple[float, list[dict[str, Any]]]] = {}


def cache_key(
    *,
    brand: str | None = None,
    model: str | None = None,
    max_price: float | None = None,
    year: int | None = None,
    query: str | None = None,
    limit: int = RESULT_LIMIT,
    available_only: bool = True,
) -> tuple[Any, ...]:
    brand_n = (brand or "").strip().lower()
    model_n = (model or "").strip().lower()
    query_n = (query or "").strip().lower()
    price_n = None if max_price is None else round(float(max_price), 2)
    return (brand_n, model_n, price_n, year, query_n, int(limit), bool(available_only))


def cache_get(key: tuple[Any, ...]) -> list[dict[str, Any]] | None:
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if not hit:
            return None
        expires_at, rows = hit
        if expires_at <= now:
            _cache.pop(key, None)
            return None
        return [dict(row) for row in rows]


def cache_set(
    key: tuple[Any, ...],
    rows: list[dict[str, Any]],
    *,
    ttl_sec: float = CACHE_TTL_SEC,
) -> None:
    expires_at = time.monotonic() + max(1.0, float(ttl_sec))
    payload = [dict(row) for row in rows]
    with _cache_lock:
        if len(_cache) >= CACHE_MAX_ENTRIES:
            # Drop oldest expiry first (simple LRU-ish eviction).
            oldest = min(_cache.items(), key=lambda item: item[1][0])[0]
            _cache.pop(oldest, None)
        _cache[key] = (expires_at, payload)


def cache_clear() -> None:
    with _cache_lock:
        _cache.clear()


def _state_field() -> str:
    return (
        os.getenv("ODOO_VEHICLE_STATE_FIELD") or "x_studio_state"
    ).strip() or "x_studio_state"


def build_inventory_domain(
    *,
    brand: str | None = None,
    model: str | None = None,
    max_price: float | None = None,
    year: int | None = None,
    query: str | None = None,
    available_only: bool = True,
    state_field: str | None = None,
) -> list[Any]:
    """Domain for ``product.template`` — active saleable SKUs only.

    Names in Odoo look like ``Corolla XLE * Toyota 2022``, so brand and model
    are separate ``ilike`` terms (AND) rather than a single concatenated phrase.
    """
    domain: list[Any] = [
        ("sale_ok", "=", True),
        ("active", "=", True),
        ("default_code", "!=", False),
    ]
    if available_only:
        field = (state_field or _state_field()).strip() or "x_studio_state"
        domain.append((field, "in", list(VEHICLE_STATE_AVAILABLE)))
    brand_t = (brand or "").strip()
    model_t = (model or "").strip()
    query_t = (query or "").strip()
    if query_t and not brand_t and not model_t:
        domain.append(("name", "ilike", query_t))
    else:
        if brand_t:
            domain.append(("name", "ilike", brand_t))
        if model_t:
            domain.append(("name", "ilike", model_t))
    if max_price is not None:
        domain.append(("list_price", "<=", float(max_price)))
    if year is not None:
        domain.append(("name", "ilike", str(int(year))))
    return domain


def query_inventory(
    execute_kw: Any,
    *,
    brand: str | None = None,
    model: str | None = None,
    max_price: float | None = None,
    year: int | None = None,
    query: str | None = None,
    limit: int = RESULT_LIMIT,
    available_only: bool = True,
    use_cache: bool = True,
) -> list[dict[str, Any]]:
    """``search_read`` with available-state filter and soft field fallback.

    ``execute_kw`` signature::

        execute_kw(model, method, args, kwargs=None) -> Any

    Matching ``OdooClient.execute_kw`` / thin XML-RPC wrappers.
    """
    cap = max(1, min(int(limit), RESULT_LIMIT))
    key = cache_key(
        brand=brand,
        model=model,
        max_price=max_price,
        year=year,
        query=query,
        limit=cap,
        available_only=available_only,
    )
    if use_cache:
        cached = cache_get(key)
        if cached is not None:
            return cached

    opts = {
        "fields": list(INVENTORY_FIELDS),
        "limit": cap,
        "order": "list_price asc, id desc",
    }

    def _run(domain: list[Any]) -> list[dict[str, Any]]:
        rows = execute_kw("product.template", "search_read", [domain], opts)
        return list(rows or [])

    domain = build_inventory_domain(
        brand=brand,
        model=model,
        max_price=max_price,
        year=year,
        query=query,
        available_only=available_only,
    )
    if not available_only:
        rows = _run(domain)
        if use_cache:
            cache_set(key, rows)
        return rows

    # Prefer preferred Studio field; on missing-field RPC error, try siblings,
    # then fall back to active-only (still sale_ok + default_code).
    preferred = _state_field()
    tried: set[str] = set()
    for field in (preferred, *VEHICLE_STATE_FIELDS):
        if field in tried:
            continue
        tried.add(field)
        domain = build_inventory_domain(
            brand=brand,
            model=model,
            max_price=max_price,
            year=year,
            query=query,
            available_only=True,
            state_field=field,
        )
        try:
            rows = _run(domain)
            if use_cache:
                cache_set(key, rows)
            return rows
        except Exception:
            continue

    rows = _run(
        build_inventory_domain(
            brand=brand,
            model=model,
            max_price=max_price,
            year=year,
            query=query,
            available_only=False,
        )
    )
    if use_cache:
        # Cache under available_only=True key still — next hit skips soft retries.
        cache_set(key, rows)
    return rows


__all__ = [
    "CACHE_TTL_SEC",
    "INVENTORY_FIELDS",
    "RESULT_LIMIT",
    "VEHICLE_STATE_AVAILABLE",
    "VEHICLE_STATE_FIELDS",
    "build_inventory_domain",
    "cache_clear",
    "cache_get",
    "cache_key",
    "cache_set",
    "query_inventory",
]
