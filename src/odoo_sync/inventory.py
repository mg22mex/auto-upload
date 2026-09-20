"""Fast Odoo vehicle inventory lookups for Vapi / CRM tools.

Prefer indexed scalar fields only (no ``categ_id.name`` relational browse).
Hard-cap result rows at 3 for voice TTS latency.

Published stock (Autosell):
  * Always: ``sale_ok``, ``active``, non-empty ``default_code``, ``list_price`` ≥ min
  * If a Studio/state field exists: also require Disponible / available labels
  * Never return mock rows; never widen the domain when state filtering fails

In-process TTL cache defaults to **off** (``ODOO_INVENTORY_CACHE_TTL_SEC=0``).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# Voice tools must stay under Vapi timeouts — never fetch large result sets.
RESULT_LIMIT = 3

# Placeholder / junk templates sometimes sit at $1 with sale_ok=True.
_MIN_PRICE_DEFAULT = 10_000.0


def _cache_ttl_sec() -> float:
    raw = (os.getenv("ODOO_INVENTORY_CACHE_TTL_SEC") or "0").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


def _min_list_price() -> float:
    raw = (os.getenv("ODOO_INVENTORY_MIN_PRICE") or str(int(_MIN_PRICE_DEFAULT))).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _MIN_PRICE_DEFAULT


# Default 0 = no cache (live Odoo). Env override for optional short TTL.
CACHE_TTL_SEC = _cache_ttl_sec()
CACHE_MAX_ENTRIES = 128

INVENTORY_FIELDS = ("id", "name", "list_price", "default_code")

# Studio / custom selection labels used by inventory sync (EN + ES).
# Only offer units still marked available — never sold / reserved / pending.
VEHICLE_STATE_AVAILABLE = ("Disponible", "disponible", "available", "Available")
VEHICLE_STATE_EXCLUDED = (
    "sold",
    "Sold",
    "Vendido",
    "vendido",
    "reserved",
    "Reserved",
    "Reservado",
    "reservado",
    "pending",
    "Pending",
    "Pendiente",
    "pendiente",
)
VEHICLE_STATE_FIELDS = (
    "x_studio_state",
    "x_studio_estatus",
    "x_vehicle_state",
    "state",
)

_UNSET: object = object()
_cache_lock = threading.Lock()
_cache: dict[tuple[Any, ...], tuple[float, list[dict[str, Any]]]] = {}
_state_field_lock = threading.Lock()
# Cached: str field name, None = confirmed absent, _UNSET = not probed yet.
_resolved_state_field: Any = _UNSET


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
    if _cache_ttl_sec() <= 0:
        return None
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
    ttl_sec: float | None = None,
) -> None:
    ttl = _cache_ttl_sec() if ttl_sec is None else float(ttl_sec)
    if ttl <= 0:
        return
    expires_at = time.monotonic() + ttl
    payload = [dict(row) for row in rows]
    with _cache_lock:
        if len(_cache) >= CACHE_MAX_ENTRIES:
            oldest = min(_cache.items(), key=lambda item: item[1][0])[0]
            _cache.pop(oldest, None)
        _cache[key] = (expires_at, payload)


def cache_clear() -> None:
    with _cache_lock:
        _cache.clear()


def reset_state_field_cache() -> None:
    """Test helper — clear resolved Studio/state field probe."""
    global _resolved_state_field
    with _state_field_lock:
        _resolved_state_field = _UNSET


def _state_field() -> str:
    return (
        os.getenv("ODOO_VEHICLE_STATE_FIELD") or "x_studio_state"
    ).strip() or "x_studio_state"


def _state_field_candidates() -> list[str]:
    preferred = _state_field()
    out: list[str] = []
    for name in (preferred, *VEHICLE_STATE_FIELDS):
        if name and name not in out:
            out.append(name)
    return out


def resolve_state_field(execute_kw: Any) -> str | None:
    """Return an existing product.template state field, or None if absent.

    Autosell Odoo 19 currently has **no** Studio state field — sold units are
    archived (``active=False``). Probe once via ``fields_get`` and cache.
    """
    global _resolved_state_field
    with _state_field_lock:
        if _resolved_state_field is not _UNSET:
            return _resolved_state_field  # type: ignore[return-value]

    candidates = _state_field_candidates()
    found: str | None = None
    try:
        meta = execute_kw(
            "product.template",
            "fields_get",
            [],
            {"attributes": ["type"]},
        )
        if isinstance(meta, dict):
            for name in candidates:
                if name in meta:
                    found = name
                    break
    except Exception as exc:
        logger.warning("inventory fields_get failed: %s — probing candidates", exc)
        for name in candidates:
            try:
                execute_kw(
                    "product.template",
                    "search_read",
                    [[(name, "!=", False)]],
                    {"fields": ["id"], "limit": 1},
                )
                found = name
                break
            except Exception:
                continue

    with _state_field_lock:
        _resolved_state_field = found
    if found:
        logger.info("inventory state field resolved: %s", found)
    else:
        logger.info(
            "inventory: no state field on product.template — "
            "published stock = sale_ok + active + SKU + min price"
        )
    return found


def build_inventory_domain(
    *,
    brand: str | None = None,
    model: str | None = None,
    max_price: float | None = None,
    year: int | None = None,
    query: str | None = None,
    available_only: bool = True,
    state_field: str | None | object = _UNSET,
) -> list[Any]:
    """Domain for ``product.template`` — active saleable published SKUs only.

    Names in Odoo look like ``Corolla XLE * Toyota 2022``, so brand and model
    are separate ``ilike`` terms (AND) rather than a single concatenated phrase.

    When ``available_only`` and ``state_field`` is a string, require that field
    in Disponible/available labels. When ``state_field`` is ``None``, rely on
    archive-as-sold (``active``) + SKU + min price — never invent mock rows.
    Pass ``state_field=_UNSET`` (default) to include the preferred env field name
    (caller should resolve existence first for live queries).
    """
    domain: list[Any] = [
        ("sale_ok", "=", True),
        ("active", "=", True),
        ("default_code", "!=", False),
        ("list_price", ">=", _min_list_price()),
    ]
    if available_only:
        if state_field is _UNSET:
            field: str | None = _state_field()
        else:
            field = state_field  # type: ignore[assignment]
        if field:
            # Prefer Disponible first in the tuple for operator readability.
            domain.append((field, "in", list(VEHICLE_STATE_AVAILABLE)))
            domain.append((field, "not in", list(VEHICLE_STATE_EXCLUDED)))
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
    """``search_read`` for published stock only — empty list when none match.

    Never widens the domain to include sold/archived units. Never injects
    mock vehicles. Missing Studio state field → archive-as-sold filters only.
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

    state_field: str | None = None
    if available_only:
        state_field = resolve_state_field(execute_kw)

    domain = build_inventory_domain(
        brand=brand,
        model=model,
        max_price=max_price,
        year=year,
        query=query,
        available_only=available_only,
        state_field=state_field if available_only else None,
    )
    rows = list(
        execute_kw("product.template", "search_read", [domain], opts) or []
    )
    if use_cache:
        cache_set(key, rows)
    return rows


__all__ = [
    "CACHE_TTL_SEC",
    "INVENTORY_FIELDS",
    "RESULT_LIMIT",
    "VEHICLE_STATE_AVAILABLE",
    "VEHICLE_STATE_EXCLUDED",
    "VEHICLE_STATE_FIELDS",
    "build_inventory_domain",
    "cache_clear",
    "cache_get",
    "cache_key",
    "cache_set",
    "query_inventory",
    "reset_state_field_cache",
    "resolve_state_field",
]
