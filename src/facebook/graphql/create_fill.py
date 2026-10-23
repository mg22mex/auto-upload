"""Deep-fill captured ``useCometMarketplaceListingCreateMutation`` variables."""
from __future__ import annotations

import copy
import json
import re
import time
import uuid
from typing import Any

from src.config import BRANCH_LABELS, BRANCH_TAG_MAP, PRIMARY_BRANCH
from src.facebook.categorize import categorize_vehicle, fb_make_name, fb_model_name
from src.facebook.graphql.errors import FacebookGraphqlError
from src.facebook.graphql.session_ctx import FbSessionContext
from src.facebook.util import mileage_for_listing, parse_mxn_price
from src.models import Vehicle

# Catalog branch markers (``*`` Periférico, ``+`` San Felipe, ``-`` consignment).
# Never strip these from Vehicle / catalog — only from GraphQL title/model fields.
_BRANCH_MARKERS = frozenset(BRANCH_TAG_MAP.keys())

# Comet ``serialized_verticals_data.vehicleType`` (NOT vehicleCategory).
_BODY_TO_VEHICLE_TYPE = {
    "SUV": "suv",
    "Truck": "truck",
    "Sedan": "car",
    "Hatchback": "car",
    "Coupe": "car",
    "Convertible": "car",
    "Wagon": "car",
    "Minivan": "minivan",
    "Small Car": "car",
    "Other": "other",
}

_COLOR_TO_VERTICAL = {
    "White": "white",
    "Black": "black",
    "Silver": "silver",
    "Gray": "gray",
    "Grey": "gray",
    "Red": "red",
    "Blue": "blue",
    "Green": "green",
    "Yellow": "yellow",
    "Orange": "orange",
    "Brown": "brown",
    "Beige": "beige",
    "Gold": "gold",
    "Other": "other",
}

# Live Comet create uses UPPER fuel + lower condition (see captured_create_live.json).
_FUEL_TO_VERTICAL = {
    "Gasoline": "GASOLINE",
    "Diesel": "DIESEL",
    "Electric": "ELECTRIC",
    "Hybrid": "HYBRID",
    "Plug-in hybrid": "HYBRID",
    "Flex": "FLEX",
    "Other": "OTHER",
}

_CONDITION_TO_VERTICAL = {
    "Excellent": "excellent",
    "Good": "good",
    "Fair": "fair",
    "Poor": "poor",
}

# Brand aliases → graphql_make_ids keys.
_MAKE_KEY_ALIASES = {
    "chevy": "chevrolet",
    "vw": "volkswagen",
    "mercedes": "mercedes-benz",
    "mercedes benz": "mercedes-benz",
    "mercedesbenz": "mercedes-benz",
}

# Exact key order from live ``create_vehicle.json`` capture (Comet).
_VERTICALS_KEY_ORDER = (
    "conversionPostID",
    "conversionSource",
    "engineSize",
    "exteriorColor",
    "fuelType",
    "horsepower",
    "interiorColor",
    "isKM",
    "isManualTransmission",
    "make",
    "mileage",
    "model",
    "numberOfOwners",
    "titleStatus",
    "trim",
    "type",
    "vehicleCategory",
    "vehicleCondition",
    "vehicleFeatures",
    "vehicleType",
    "vin",
    "year",
)


def fill_create_vehicle_variables(
    template_variables: dict[str, Any],
    *,
    session: FbSessionContext,
    vehicle: Vehicle,
    fb_config: dict,
    photo_ids: list[str],
) -> dict[str, Any]:
    """Rewrite a captured create mutation payload for ``vehicle`` + ``photo_ids``.

    Starts from a deep copy of the capture and only replaces fields the composer
    changes per listing. Does **not** invent keys absent from the capture
    (e.g. ``item_price.currency`` — not in live Comet payloads).
    """
    data = copy.deepcopy(template_variables)
    attrs = categorize_vehicle(vehicle)
    price = parse_mxn_price(vehicle.price)
    mileage = mileage_for_listing(vehicle.mileage)
    # Catalog keeps +/* branch markers; GraphQL title/model map them away.
    title, branch_label = _graphql_title_and_branch(vehicle)
    description = _graphql_plain_description(
        vehicle, title=title, mileage=mileage, branch_label=branch_label
    )
    lat = fb_config.get("location_latitude")
    lon = fb_config.get("location_longitude")
    make_ids = fb_config.get("graphql_make_ids") or {}
    if not isinstance(make_ids, dict):
        make_ids = {}

    input_obj = data.setdefault("input", {})
    if not isinstance(input_obj, dict):
        raise ValueError("create_vehicle variables.input must be an object")

    input_obj["actor_id"] = str(session.c_user)
    # Capture uses small integer strings ("1", "2"), not UUIDs.
    input_obj["client_mutation_id"] = str(uuid.uuid4().int % 10_000_000)
    if input_obj.get("attribution_id_v2"):
        input_obj["attribution_id_v2"] = _refresh_attribution(
            str(input_obj["attribution_id_v2"])
        )

    audience = input_obj.get("audience")
    if not isinstance(audience, dict):
        raise FacebookGraphqlError("create_vehicle.json missing input.audience")
    marketplace = audience.get("marketplace")
    if not isinstance(marketplace, dict) or not marketplace.get("marketplace_id"):
        raise FacebookGraphqlError(
            "create_vehicle.json missing audience.marketplace.marketplace_id"
        )

    payload = input_obj.get("data")
    if not isinstance(payload, dict):
        raise ValueError("create_vehicle variables.input.data must be an object")

    # Capture always has these nulls — preserve exactly, do not invent siblings.
    if "client_application" not in payload:
        payload["client_application"] = None
    if "vin_autofill_summary" not in input_obj:
        input_obj["vin_autofill_summary"] = None

    common = payload.get("common")
    if not isinstance(common, dict):
        raise ValueError("create_vehicle variables.input.data.common must be an object")

    if not common.get("category_id"):
        raise FacebookGraphqlError(
            "create_vehicle.json missing data.common.category_id"
        )

    common["title"] = title
    common["description"] = {"text": description}

    # Match capture shape exactly: {"price":"<digits>"} only — no currency.
    price_obj = common.get("item_price")
    if isinstance(price_obj, dict):
        price_obj = dict(price_obj)
        price_obj["price"] = str(price)
        price_obj.pop("currency", None)
        common["item_price"] = price_obj
    else:
        common["item_price"] = {"price": str(price)}

    common["photo_ids"] = [str(pid) for pid in photo_ids]
    if not common["photo_ids"]:
        raise FacebookGraphqlError("create mutation requires at least one photo_id")

    # Lists: keep capture type (list), replace contents.
    if not isinstance(common.get("video_ids"), list):
        common["video_ids"] = []
    else:
        common["video_ids"] = list(common["video_ids"])
    if not isinstance(common.get("xpost_target_ids"), list):
        common["xpost_target_ids"] = []
    else:
        common["xpost_target_ids"] = list(common["xpost_target_ids"])

    if lat not in (None, ""):
        try:
            common["latitude"] = float(lat)
        except (TypeError, ValueError):
            pass
    if lon not in (None, ""):
        try:
            common["longitude"] = float(lon)
        except (TypeError, ValueError):
            pass
    if common.get("latitude") is None or common.get("longitude") is None:
        raise FacebookGraphqlError(
            "create requires location_latitude / location_longitude in config.yaml"
        )

    # Leave capture flags untouched when present:
    # comments_disabled, draft_type, listing_email_id, source_type,
    # is_photo_order_set_by_seller.

    # Live UI create sends source_type=null (not composer_listing_type_selector).
    if "source_type" in common:
        common["source_type"] = None

    common["serialized_verticals_data"] = _fill_serialized_verticals(
        common.get("serialized_verticals_data"),
        vehicle=vehicle,
        attrs_make=attrs.make,
        attrs_model=attrs.model,
        mileage=mileage,
        body_style=attrs.body_style,
        exterior=attrs.exterior_color,
        interior=attrs.interior_color,
        fuel=attrs.fuel_type,
        condition=attrs.condition,
        transmission=attrs.transmission,
        make_ids=make_ids,
    )
    return data


def _split_branch_marker(text: str) -> tuple[str, str | None]:
    """Return ``(text_without_marker, marker)`` for trailing ``*`` / ``+`` / ``-``.

    Does not mutate catalog objects — pure string map for GraphQL fields.
    """
    cleaned = (text or "").strip()
    if cleaned and cleaned[-1] in _BRANCH_MARKERS:
        return cleaned[:-1].rstrip(), cleaned[-1]
    return cleaned, None


def _graphql_map_listing_text(text: str) -> str:
    """Map catalog text → FB-safe title/model (branch markers removed)."""
    cleaned, _marker = _split_branch_marker(text)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned


def _graphql_title_and_branch(vehicle: Vehicle) -> tuple[str, str]:
    """GraphQL listing title + human branch label from catalog markers.

    Source ``vehicle.title`` / ``marketplace_title`` are left unchanged; only the
    returned strings are used in the mutation payload.
    """
    raw_title = vehicle.marketplace_title
    mapped, marker = _split_branch_marker(raw_title)
    if marker is None:
        # Marker often lives on model title only (year/brand prefix has no tag).
        _model_mapped, marker = _split_branch_marker(vehicle.title or "")
    mapped = re.sub(r"\s{2,}", " ", mapped).strip()
    branch_key = BRANCH_TAG_MAP.get(marker or "", PRIMARY_BRANCH)
    branch_label = BRANCH_LABELS.get(branch_key, BRANCH_LABELS[PRIMARY_BRANCH])
    return mapped, branch_label


def graphql_listing_title(vehicle: Vehicle) -> str:
    """FB-safe Marketplace title (branch ``+``/``*``/``-`` removed)."""
    return _graphql_title_and_branch(vehicle)[0]


def graphql_model_name(vehicle: Vehicle) -> str:
    """FB-safe model string for ``serialized_verticals_data.model``."""
    raw = fb_model_name(vehicle.title) or str(vehicle.title or "")
    return _graphql_map_listing_text(raw)


def graphql_plain_description(vehicle: Vehicle) -> str:
    """Plain GraphQL description with Sucursal line; catalog markers mapped out."""
    title, branch_label = _graphql_title_and_branch(vehicle)
    return _graphql_plain_description(
        vehicle,
        title=title,
        mileage=mileage_for_listing(vehicle.mileage),
        branch_label=branch_label,
    )


def _graphql_plain_description(
    vehicle: Vehicle,
    *,
    title: str,
    mileage: str,
    branch_label: str,
) -> str:
    """Plain-text description for GraphQL (no markdown/emoji CTA).

    Branch ``+``/``*`` are omitted from title/model but recorded as
    ``Sucursal: …`` so San Felipe vs Periférico is preserved for buyers.
    """
    lines = [title, f"Kilometraje: {mileage} km"]
    version = _graphql_map_listing_text(vehicle.version or "")
    if version:
        lines.append(f"Versión: {version}")
    year = (vehicle.year or "").strip()
    if year:
        lines.append(f"Año: {year}")
    if branch_label:
        lines.append(f"Sucursal: {branch_label}")
    if vehicle.url:
        lines.append(f"Más información: {vehicle.url}")
    return "\n".join(lines)


def _normalize_make_key(brand: str) -> str:
    key = (brand or "").strip().lower()
    return _MAKE_KEY_ALIASES.get(key, key)


def _lookup_make_id(brand: str, make_ids: dict[str, Any]) -> str | None:
    want = _normalize_make_key(brand)
    if not want:
        return None
    for key, value in make_ids.items():
        if _normalize_make_key(str(key)) == want:
            text = str(value or "").strip()
            if text:
                return text
    return None


def _owner_brand_for_make_id(make_id: str, make_ids: dict[str, Any]) -> str | None:
    want = str(make_id or "").strip()
    if not want:
        return None
    for key, value in make_ids.items():
        if str(value or "").strip() == want:
            return _normalize_make_key(str(key))
    return None


def _dump_verticals(verticals: dict[str, Any]) -> str:
    """Serialize with capture key order (stable Comet shape)."""
    ordered: dict[str, Any] = {}
    for key in _VERTICALS_KEY_ORDER:
        if key in verticals:
            ordered[key] = verticals[key]
    for key, value in verticals.items():
        if key not in ordered:
            ordered[key] = value
    return json.dumps(ordered, separators=(",", ":"), ensure_ascii=False)


def _fill_serialized_verticals(
    raw: Any,
    *,
    vehicle: Vehicle,
    attrs_make: str,
    attrs_model: str,
    mileage: str,
    body_style: str,
    exterior: str,
    interior: str,
    fuel: str,
    condition: str,
    transmission: str,
    make_ids: dict[str, Any],
) -> str:
    try:
        verticals: dict[str, Any] = (
            json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
        )
    except json.JSONDecodeError:
        verticals = {}
    if not isinstance(verticals, dict):
        verticals = {}

    # Seed any missing capture keys with empty defaults (same as live payload).
    for key in _VERTICALS_KEY_ORDER:
        if key == "isKM":
            verticals.setdefault(key, True)
        elif key == "isManualTransmission":
            verticals.setdefault(key, False)
        elif key == "vehicleFeatures":
            verticals.setdefault(key, [])
        elif key == "type":
            verticals.setdefault(key, "autos")
        elif key == "vehicleCategory":
            verticals.setdefault(key, "car&truck/")
        elif key == "year":
            continue
        else:
            verticals.setdefault(key, "" if key != "vehicleFeatures" else [])

    year_raw = str(vehicle.year or "").strip()
    if year_raw.isdigit():
        verticals["year"] = int(year_raw)

    verticals["mileage"] = str(mileage)
    verticals["isKM"] = True
    raw_model = (
        attrs_model or fb_model_name(vehicle.title) or str(verticals.get("model") or "")
    )
    # Payload-only map: catalog title may still end with +/* branch markers.
    verticals["model"] = _graphql_map_listing_text(raw_model)
    verticals["isManualTransmission"] = bool(
        "manual" in (transmission or "").lower()
    )

    # Live UI create fills these; empty strings → field_exception on marketplace_listing_create.
    mapped_vt = _BODY_TO_VEHICLE_TYPE.get(body_style, "car")
    if mapped_vt not in {"suv", "truck", "car", "minivan", "other"}:
        mapped_vt = "car"
    verticals["vehicleType"] = mapped_vt
    verticals["exteriorColor"] = (
        _COLOR_TO_VERTICAL.get(exterior) or "silver"
    ).strip().lower()
    verticals["interiorColor"] = (
        _COLOR_TO_VERTICAL.get(interior) or "black"
    ).strip().lower()
    verticals["fuelType"] = _FUEL_TO_VERTICAL.get(fuel) or "GASOLINE"
    verticals["vehicleCondition"] = (
        _CONDITION_TO_VERTICAL.get(condition) or "excellent"
    )
    verticals["titleStatus"] = "CLEAN"

    verticals["type"] = "autos"
    verticals["vehicleCategory"] = "car&truck/"
    if not isinstance(verticals.get("vehicleFeatures"), list):
        verticals["vehicleFeatures"] = []

    brand_label = attrs_make or fb_make_name(vehicle.brand) or str(vehicle.brand or "")
    make_id = _lookup_make_id(vehicle.brand or brand_label, make_ids)
    captured_make = str(verticals.get("make") or "").strip()

    if make_id:
        verticals["make"] = make_id
    elif captured_make:
        owner = _owner_brand_for_make_id(captured_make, make_ids)
        brand_key = _normalize_make_key(vehicle.brand or brand_label)
        if owner and owner != brand_key:
            print(
                f"  WARN create: discarding captured make id {captured_make} "
                f"(mapped to {owner}, listing brand={brand_key})",
                flush=True,
            )
            verticals["make"] = brand_label
        elif owner == brand_key:
            verticals["make"] = captured_make
        else:
            print(
                f"  WARN create: no graphql_make_ids entry for "
                f"{brand_key!r}; using display name {brand_label!r}",
                flush=True,
            )
            verticals["make"] = brand_label
    else:
        print(
            f"  WARN create: missing make id for {brand_label!r} — "
            f"add facebook.graphql_make_ids in config.yaml",
            flush=True,
        )
        verticals["make"] = brand_label

    if not str(verticals.get("make") or "").strip():
        raise FacebookGraphqlError(
            f"Cannot build create payload: empty make for brand={vehicle.brand!r}. "
            f"Add facebook.graphql_make_ids.{_normalize_make_key(vehicle.brand or '')} "
            f"to config.yaml."
        )

    return _dump_verticals(verticals)


def _refresh_attribution(attr: str) -> str:
    """Swap embedded ms timestamps so attribution looks fresh."""
    now_ms = str(int(time.time() * 1000))
    parts = attr.split(",")
    refreshed: list[str] = []
    i = 0
    while i < len(parts):
        part = parts[i]
        refreshed.append(part)
        if part in {"unexpected", "via_cold_start"} and i + 1 < len(parts):
            if re.fullmatch(r"\d{12,14}", parts[i + 1] or ""):
                refreshed.append(now_ms)
                i += 2
                continue
        i += 1
    return ",".join(refreshed)
