from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, Page

from src.facebook.errors import FacebookPostingError
from src.facebook.graphql.client import GraphQLClient
from src.facebook.graphql.create_fill import (
    fill_create_vehicle_variables,
    graphql_listing_title,
    graphql_model_name,
    graphql_plain_description,
)
from src.facebook.graphql.errors import (
    FacebookGraphqlError,
    FacebookGraphqlFallback,
    extract_listing_id_from_payload,
    is_duplicate_graphql_payload,
    raise_for_graphql_payload,
)
from src.facebook.session import get_page
from src.facebook.graphql.photos import upload_photos_for_listing
from src.facebook.graphql.session_ctx import (
    FbSessionContext,
    extract_session_context,
    harvest_comet_scaffold,
)
from src.facebook.graphql.templates import load_template
from src.facebook.photos import download_vehicle_photos
from src.models import Vehicle


def create_vehicle_listing_graphql(
    context: BrowserContext,
    vehicle: Vehicle,
    *,
    fb_config: dict,
    max_photos: int,
    log_dir: Path,
    photo_paths: list[Path] | None = None,
    skip_shelf_gate: bool = False,
    delete_verified: bool = False,
    page: Page | None = None,
    root: Path | None = None,
    account_id: str = "",
    store: Any = None,
) -> str:
    """Create a Marketplace vehicle listing via GraphQL templates + httpx.

    Requires ``data/graphql/create_vehicle.json``. Photos upload via
    ``data/graphql/photo_upload.json`` or the default Marketplace ajax endpoint
    when that file is a mis-captured GraphQL query.

    Does **not** drive the Playwright composer (avoids duplicate-listing spam).
    """
    if skip_shelf_gate and not delete_verified:
        raise FacebookPostingError(
            f"SKIP_CREATE: skip_shelf_gate requires delete_verified=True for "
            f"{vehicle.autosell_id}"
        )

    # GraphQL create skips UI shelf gate — preflight GraphQL purge already ran.
    if not skip_shelf_gate:
        print(
            f"  {vehicle.autosell_id}: GraphQL create — skipping Playwright "
            f"shelf gate (use preflight GraphQL purge)",
            flush=True,
        )

    template = load_template("create_vehicle", root=root, required=True)
    assert template is not None

    work_page = page or get_page(context)
    session = extract_session_context(
        context, page=work_page, account_id=account_id, fetch_tokens=True
    )
    if not session.comet_scaffold:
        session.comet_scaffold = harvest_comet_scaffold(work_page)
    if not session.comet_scaffold:
        raise FacebookGraphqlError(
            f"{vehicle.autosell_id}: could not harvest Comet GraphQL scaffold "
            f"(__dyn/__spin_*) — open Marketplace composer and retry"
        )
    print(
        f"  {vehicle.autosell_id}: GraphQL create session {session.redacted_summary()}",
        flush=True,
    )

    own_photos = photo_paths is None
    if own_photos:
        photo_paths = download_vehicle_photos(vehicle, max_photos=max_photos)
    assert photo_paths is not None
    if not photo_paths:
        raise FacebookPostingError(
            f"No photos ready for {vehicle.autosell_id} — cannot publish"
        )

    try:
        photo_ids = upload_photos_for_listing(
            session, photo_paths, root=root, autosell_id=vehicle.autosell_id
        )
        listing_url = publish_vehicle_graphql(
            session,
            vehicle,
            fb_config=fb_config,
            photo_ids=photo_ids,
            root=root,
            log_dir=log_dir,
            template=template,
        )
        if store is not None and account_id:
            try:
                store.upsert_fb_listing(
                    vehicle.autosell_id,
                    account_id,
                    fb_listing_url=listing_url,
                    content_hash=vehicle.content_hash(),
                    status="live",
                )
            except Exception:
                pass
        return listing_url
    except FacebookPostingError as exc:
        if store is not None and account_id and "duplicate" in str(exc).lower():
            try:
                store.mark_duplicate_blocked(
                    vehicle.autosell_id,
                    account_id,
                    content_hash=vehicle.content_hash(),
                    reason="fb_duplicate_graphql",
                )
                print(
                    f"  sync.db: marked {vehicle.autosell_id} on {account_id} "
                    f"as duplicate_blocked (GraphQL)",
                    flush=True,
                )
            except Exception:
                pass
        raise
    finally:
        if own_photos:
            for path in photo_paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            if photo_paths:
                try:
                    photo_paths[0].parent.rmdir()
                except OSError:
                    pass


def publish_vehicle_graphql(
    session: FbSessionContext,
    vehicle: Vehicle,
    *,
    fb_config: dict,
    photo_ids: list[str],
    root: Path | None = None,
    log_dir: Path | None = None,
    template: dict[str, Any] | None = None,
) -> str:
    template = template or load_template("create_vehicle", root=root, required=True)
    assert template is not None

    raw_variables = template.get("variables")
    if not isinstance(raw_variables, dict):
        raise FacebookGraphqlFallback("create_vehicle.json missing variables object")

    # Prefer deep-fill of captured Comet mutation shape; also honor {{placeholders}}.
    blob = json.dumps(raw_variables)
    if "{{" in blob and "{{photo_ids}}" in blob:
        from src.facebook.graphql.templates import base_mapping, build_variables
        from src.facebook.util import mileage_for_listing, parse_mxn_price

        mapping = base_mapping(
            c_user=session.c_user,
            extra={
                # Always map +/* branch markers out of GraphQL title/model.
                "title": graphql_listing_title(vehicle),
                "description": graphql_plain_description(vehicle),
                "price": parse_mxn_price(vehicle.price),
                "year": str(vehicle.year or ""),
                "make": str(vehicle.brand or ""),
                "model": graphql_model_name(vehicle),
                "mileage": mileage_for_listing(vehicle.mileage),
                "photo_ids": photo_ids,
                "photos": photo_ids,
            },
        )
        variables = build_variables(template, mapping)
    else:
        variables = fill_create_vehicle_variables(
            raw_variables,
            session=session,
            vehicle=vehicle,
            fb_config=fb_config,
            photo_ids=photo_ids,
        )

    # Defense in depth: final payload title/model must not carry branch markers.
    _ensure_graphql_title_model_clean(variables, vehicle)

    doc_id = str(template["doc_id"])
    friendly = str(
        template.get("friendly_name") or "useCometMarketplaceListingCreateMutation"
    )
    sent_title = _payload_title(variables) or graphql_listing_title(vehicle)

    with GraphQLClient(session) as gql:
        print(
            f"  {vehicle.autosell_id}: GraphQL create doc_id={doc_id} "
            f"photos={len(photo_ids)} title={sent_title!r}",
            flush=True,
        )
        try:
            payload = gql.post(
                doc_id,
                variables,
                friendly_name=friendly,
                check_errors=True,
                require_scaffold=True,
            )
        except FacebookPostingError as exc:
            if log_dir is not None:
                dump: dict[str, Any] = {
                    "error": str(exc),
                    "doc_id": doc_id,
                    "friendly_name": friendly,
                    "variables": variables,
                    "analysis": _analyze_create_failure(variables, template),
                }
                if isinstance(exc, FacebookGraphqlError) and exc.payload is not None:
                    dump["response"] = exc.payload
                    dump["analysis"]["fb_error"] = _summarize_fb_error(exc.payload)
                _dump_payload(log_dir, vehicle.autosell_id, "create_error", dump)
            raise

    if is_duplicate_graphql_payload(payload):
        raise FacebookPostingError(
            f"SKIP_CREATE: Facebook duplicate listing detected via GraphQL "
            f"for {vehicle.autosell_id}"
        )
    raise_for_graphql_payload(payload, action="create_vehicle")

    item_id = extract_listing_id_from_payload(payload)
    if not item_id:
        for path_key in template.get("listing_id_paths") or []:
            item_id = _dig_id(payload, [str(path_key)])
            if item_id:
                break
    if not item_id:
        if log_dir is not None:
            _dump_payload(log_dir, vehicle.autosell_id, "create_no_id", payload)
        raise FacebookGraphqlError(
            f"GraphQL create succeeded but no listing id for {vehicle.autosell_id}",
            payload=payload,
        )

    listing_url = f"https://www.facebook.com/marketplace/item/{item_id}/"
    print(f"  {vehicle.autosell_id}: GraphQL create OK → {listing_url}", flush=True)
    return listing_url


def _dig_id(payload: Any, paths: list[str]) -> str | None:
    for path in paths:
        node: Any = payload
        ok = True
        for part in str(path).split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                ok = False
                break
        if ok and node is not None and str(node).strip():
            text = str(node).strip()
            if text.isdigit() or len(text) >= 6:
                return text
    return extract_listing_id_from_payload(payload)


def _payload_common(variables: dict[str, Any]) -> dict[str, Any]:
    input_obj = variables.get("input")
    if not isinstance(input_obj, dict):
        return {}
    data = input_obj.get("data")
    if not isinstance(data, dict):
        return {}
    common = data.get("common")
    return common if isinstance(common, dict) else {}


def _payload_title(variables: dict[str, Any]) -> str:
    return str(_payload_common(variables).get("title") or "").strip()


def _ensure_graphql_title_model_clean(
    variables: dict[str, Any], vehicle: Vehicle
) -> None:
    """Force FB-safe title/model on the mutation payload (catalog unchanged)."""
    common = _payload_common(variables)
    if not common:
        return
    common["title"] = graphql_listing_title(vehicle)
    raw_verticals = common.get("serialized_verticals_data")
    try:
        verticals = (
            json.loads(raw_verticals)
            if isinstance(raw_verticals, str) and raw_verticals.strip()
            else {}
        )
    except json.JSONDecodeError:
        verticals = {}
    if isinstance(verticals, dict):
        verticals["model"] = graphql_model_name(vehicle)
        common["serialized_verticals_data"] = json.dumps(
            verticals, separators=(",", ":"), ensure_ascii=False
        )


def _dump_payload(log_dir: Path, autosell_id: str, tag: str, payload: Any) -> None:
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"{autosell_id}_gql_{tag}.json"
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False)[:200_000],
            encoding="utf-8",
        )
    except OSError:
        pass


def _summarize_fb_error(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"raw_type": type(payload).__name__}
    errors = payload.get("errors") or []
    first = errors[0] if errors and isinstance(errors[0], dict) else {}
    return {
        "path": first.get("path"),
        "code": first.get("code"),
        "severity": first.get("severity"),
        "message": first.get("message"),
        "debug_info": first.get("debug_info"),
        "mids": first.get("mids"),
        "marketplace_listing_create": (payload.get("data") or {}).get(
            "marketplace_listing_create"
        ),
        "note": (
            "FB field_exception does not name the bad input key; "
            "compare analysis.verticals_diff / suspects vs create_vehicle.json"
        ),
    }


def _analyze_create_failure(
    variables: dict[str, Any], template: dict[str, Any]
) -> dict[str, Any]:
    """Diff sent variables against the capture template for likely validators."""
    analysis: dict[str, Any] = {"suspects": []}
    try:
        sent_common = variables["input"]["data"]["common"]
        cap_vars = template.get("variables") or {}
        cap_common = cap_vars["input"]["data"]["common"]
        sent_vert = json.loads(sent_common.get("serialized_verticals_data") or "{}")
        cap_vert = json.loads(cap_common.get("serialized_verticals_data") or "{}")
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        analysis["parse_error"] = str(exc)
        return analysis

    vert_diff = {
        key: {"sent": sent_vert.get(key), "capture": cap_vert.get(key)}
        for key in sorted(set(sent_vert) | set(cap_vert))
        if sent_vert.get(key) != cap_vert.get(key)
    }
    analysis["verticals_diff"] = vert_diff
    analysis["item_price_sent"] = sent_common.get("item_price")
    analysis["item_price_capture"] = cap_common.get("item_price")
    analysis["photo_count_sent"] = len(sent_common.get("photo_ids") or [])
    analysis["photo_count_capture"] = len(cap_common.get("photo_ids") or [])

    make = str(sent_vert.get("make") or "")
    if not make.isdigit():
        analysis["suspects"].append(
            f"serialized_verticals_data.make={make!r} is not a catalog id"
        )
    if "+" in str(sent_vert.get("model") or "") or "*" in str(sent_vert.get("model") or ""):
        analysis["suspects"].append(
            "serialized_verticals_data.model still contains +/* markers"
        )
    if sent_vert.get("vehicleType") not in {"suv", "truck", "car", "minivan", "other"}:
        analysis["suspects"].append(
            f"serialized_verticals_data.vehicleType={sent_vert.get('vehicleType')!r} "
            f"outside known Comet enum"
        )
    if isinstance(sent_common.get("item_price"), dict) and "currency" in (
        sent_common.get("item_price") or {}
    ):
        analysis["suspects"].append(
            "item_price.currency present (not in live capture shape)"
        )
    desc = str((sent_common.get("description") or {}).get("text") or "")
    if "**" in desc or any(ord(ch) > 0x1F300 for ch in desc):
        analysis["suspects"].append(
            "description contains markdown/emoji (capture used plain text)"
        )
    if not analysis["suspects"]:
        analysis["suspects"].append(
            "No obvious shape/suspect markers — remaining diffs are value-level "
            f"({', '.join(vert_diff.keys()) or 'none'}). FB path is only "
            "marketplace_listing_create."
        )
    return analysis
