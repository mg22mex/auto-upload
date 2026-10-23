"""Fetch active Marketplace selling-shelf item ids via GraphQL / HTTP (no DOM)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from src.facebook.graphql.client import GraphQLClient
from src.facebook.graphql.errors import FacebookGraphqlFallback
from src.facebook.graphql.session_ctx import (
    FbSessionContext,
    MARKETPLACE_URL,
    browser_headers,
)
from src.facebook.graphql.templates import (
    base_mapping,
    build_variables,
    load_template,
    substitute_known_ids,
)
from src.facebook.util import env_int

SELLING_URLS = (
    "https://www.facebook.com/marketplace/you/selling",
    "https://www.facebook.com/marketplace/you/selling?status=ACTIVE",
    "https://www.facebook.com/marketplace/you/selling?state=LIVE",
)

_ITEM_URL_RE = re.compile(r"/marketplace/item/(\d{8,})")
_FOR_SALE_ID_RE = re.compile(
    r'"(?:for_sale_item_id|listing_id|marketplace_listing_id|product_item_id)"\s*:\s*"?(\d{8,})"?',
    re.I,
)
_CREATION_NEAR_ID_RE = re.compile(
    r'(?:creation_time|created_time|listed_time|publish_time)"?\s*:\s*"?(\d{9,13})"?',
    re.I,
)
_TITLE_RE = re.compile(
    r'"(?:marketplace_listing_title|custom_title|attribute_data_title|title)"\s*:\s*"((?:\\.|[^"\\])*)"',
    re.I,
)

# Template basenames to try (first existing wins).
_SELLING_TEMPLATE_NAMES = (
    "selling_active",
    "selling_fast_active",
    "seller_hub",
    "selling_hub",
)


@dataclass
class ActiveListing:
    item_id: str
    title: str = ""
    age_days: float | None = None
    age_text: str = ""
    href: str = ""
    source: str = "graphql"

    def __post_init__(self) -> None:
        if not self.href:
            self.href = f"https://www.facebook.com/marketplace/item/{self.item_id}/"

    def card_text_like(self) -> str:
        return (self.title or self.item_id).strip()


def fetch_active_selling_listings(
    session: FbSessionContext,
    *,
    root: Path | None = None,
    client: GraphQLClient | None = None,
    max_pages: int | None = None,
) -> list[ActiveListing]:
    """Return active selling listings for the session user.

    Order:
      1. Captured GraphQL template (``selling_active.json`` etc.)
      2. Authenticated HTTP fetch of ``/marketplace/you/selling`` + JSON scrape
    """
    pages = max_pages if max_pages is not None else env_int("FB_GQL_SELLING_MAX_PAGES", 10)
    by_id: dict[str, ActiveListing] = {}

    template_name = _first_existing_template(root)
    if template_name:
        print(f"  GraphQL selling list via template={template_name}", flush=True)
        for item in _fetch_via_graphql_template(
            session, template_name, root=root, client=client, max_pages=pages
        ):
            by_id.setdefault(item.item_id, item)
    else:
        print(
            "  GraphQL selling list: no selling_*.json template — "
            "HTTP scrape of /you/selling",
            flush=True,
        )

    if not by_id:
        for item in _fetch_via_selling_html(session):
            by_id.setdefault(item.item_id, item)

    listings = list(by_id.values())
    print(f"  GraphQL/HTTP selling discovery: {len(listings)} active item(s)", flush=True)
    return listings


def _first_existing_template(root: Path | None) -> str | None:
    for name in _SELLING_TEMPLATE_NAMES:
        try:
            load_template(name, root=root, required=True)
            return name
        except FacebookGraphqlFallback:
            continue
        except Exception:
            continue
    return None


def _fetch_via_graphql_template(
    session: FbSessionContext,
    name: str,
    *,
    root: Path | None,
    client: GraphQLClient | None,
    max_pages: int,
) -> list[ActiveListing]:
    template = load_template(name, root=root, required=True)
    assert template is not None
    owns = client is None
    gql = client or GraphQLClient(session)
    found: dict[str, ActiveListing] = {}
    cursor: str | None = None
    try:
        for page_idx in range(max(1, max_pages)):
            mapping = base_mapping(
                c_user=session.c_user,
                extra={"cursor": cursor, "count": int(template.get("count") or 24)},
            )
            variables = _prepare_query_variables(template, mapping)
            friendly = str(
                template.get("friendly_name")
                or "useCometMarketplaceSellingFastActiveSectionPaginationQuery"
            )
            payload = gql.post(
                str(template["doc_id"]),
                variables,
                friendly_name=friendly,
                check_errors=True,
            )
            for item in parse_active_listings_payload(payload, source=f"gql:{name}"):
                found.setdefault(item.item_id, item)

            next_cursor = _dig_path(
                payload, template.get("cursor_path")
            ) or _find_end_cursor(payload)
            has_next = _dig_path(payload, template.get("has_next_path"))
            if has_next is None:
                has_next = bool(next_cursor) and next_cursor != cursor
            if not has_next or not next_cursor or next_cursor == cursor:
                break
            cursor = str(next_cursor)
            print(
                f"  selling page {page_idx + 1}: {len(found)} item(s), next cursor…",
                flush=True,
            )
    finally:
        if owns:
            gql.close()
    return list(found.values())


def _prepare_query_variables(
    template: dict[str, Any], mapping: dict[str, Any]
) -> dict[str, Any]:
    raw = template.get("variables")
    if raw is None:
        raise FacebookGraphqlFallback("Selling template missing variables")
    blob = json.dumps(raw)
    if "{{" in blob:
        # Allow null cursor: map missing/None cursor to JSON null via empty → null
        if mapping.get("cursor") is None:
            mapping = dict(mapping)
            mapping["cursor"] = None  # type: ignore[assignment]
        # Special-case: render "{{cursor}}" as null when cursor is None
        variables = json.loads(blob)
        variables = _render_cursor_aware(variables, mapping)
        return variables if isinstance(variables, dict) else build_variables(template, mapping)
    return substitute_known_ids(raw, mapping)


def _render_cursor_aware(node: Any, mapping: dict[str, Any]) -> Any:
    from src.facebook.graphql.templates import render_template_value

    if isinstance(node, str) and node.strip() in {"{{cursor}}", "{{ cursor }}"}:
        return mapping.get("cursor")  # may be None
    if isinstance(node, str):
        return render_template_value(node, {k: v for k, v in mapping.items() if v is not None or k == "cursor"})
    if isinstance(node, list):
        return [_render_cursor_aware(x, mapping) for x in node]
    if isinstance(node, dict):
        return {k: _render_cursor_aware(v, mapping) for k, v in node.items()}
    return node


def _fetch_via_selling_html(session: FbSessionContext) -> list[ActiveListing]:
    headers = browser_headers(session, for_graphql=False)
    found: dict[str, ActiveListing] = {}
    with httpx.Client(timeout=60.0, follow_redirects=True) as http:
        for url in SELLING_URLS:
            try:
                response = http.get(url, headers=headers)
            except httpx.HTTPError as exc:
                print(f"  WARNING: selling HTML fetch failed ({url}): {exc}", flush=True)
                continue
            if response.status_code >= 400:
                print(
                    f"  WARNING: selling HTML HTTP {response.status_code} ({url})",
                    flush=True,
                )
                continue
            html = response.text or ""
            for item in parse_active_listings_from_text(html, source="selling_html"):
                found.setdefault(item.item_id, item)
            if found:
                break
    # Also try Marketplace home embed as a weak secondary signal.
    if not found:
        try:
            with httpx.Client(timeout=60.0, follow_redirects=True) as http:
                response = http.get(MARKETPLACE_URL, headers=headers)
                if response.status_code < 400:
                    for item in parse_active_listings_from_text(
                        response.text or "", source="marketplace_html"
                    ):
                        found.setdefault(item.item_id, item)
        except httpx.HTTPError:
            pass
    return list(found.values())


def parse_active_listings_payload(
    payload: Any, *, source: str = "graphql"
) -> list[ActiveListing]:
    """Walk a GraphQL JSON payload and collect Marketplace listing nodes."""
    found: dict[str, ActiveListing] = {}

    def consider(
        item_id: str,
        *,
        title: str = "",
        creation_ts: float | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        item_id = str(item_id).strip()
        if not item_id.isdigit() or len(item_id) < 8:
            return
        age_days = _age_days_from_unix(creation_ts) if creation_ts else None
        age_text = (
            f"creation_time age={age_days:.1f}d"
            if age_days is not None
            else ""
        )
        title = title or _title_from_node(context or {})
        prev = found.get(item_id)
        if prev is None or (age_days is not None and prev.age_days is None):
            found[item_id] = ActiveListing(
                item_id=item_id,
                title=title,
                age_days=age_days,
                age_text=age_text,
                source=source,
            )

    def walk(node: Any, parent: dict[str, Any] | None = None) -> None:
        if isinstance(node, dict):
            typename = str(node.get("__typename") or "")
            creation = _first_unix(
                node.get("creation_time"),
                node.get("created_time"),
                node.get("listed_time"),
                node.get("publish_time"),
            )
            title = _title_from_node(node)
            for key in (
                "for_sale_item_id",
                "listing_id",
                "marketplace_listing_id",
                "product_item_id",
                "story_id",
            ):
                if key in node and node[key] is not None:
                    consider(str(node[key]), title=title, creation_ts=creation, context=node)
            node_id = node.get("id")
            if node_id is not None and (
                "Marketplace" in typename
                or "ForSale" in typename
                or "GroupCommerce" in typename
                or parent
                and any(
                    k in parent
                    for k in ("marketplace_listing_title", "listing_price", "is_live")
                )
                or "marketplace_listing_title" in node
                or "listing_price" in node
            ):
                consider(str(node_id), title=title, creation_ts=creation, context=node)
            for value in node.values():
                walk(value, node if _looks_listing_ish(node) else parent)
        elif isinstance(node, list):
            for item in node:
                walk(item, parent)

    walk(payload)
    # Regex fallback on serialized payload.
    try:
        blob = json.dumps(payload)
    except (TypeError, ValueError):
        blob = str(payload)
    for item in parse_active_listings_from_text(blob, source=source):
        if item.item_id not in found:
            found[item.item_id] = item
        elif found[item.item_id].age_days is None and item.age_days is not None:
            found[item.item_id] = item
    return list(found.values())


def parse_active_listings_from_text(text: str, *, source: str = "text") -> list[ActiveListing]:
    """Extract item ids (+ best-effort age/title) from HTML or JSON text."""
    if not text:
        return []
    found: dict[str, ActiveListing] = {}

    for match in _ITEM_URL_RE.finditer(text):
        item_id = match.group(1)
        window = text[max(0, match.start() - 400) : match.end() + 800]
        found.setdefault(
            item_id,
            ActiveListing(
                item_id=item_id,
                title=_first_title(window),
                age_days=_age_from_window(window),
                age_text=_age_text_from_window(window),
                source=source,
            ),
        )

    for match in _FOR_SALE_ID_RE.finditer(text):
        item_id = match.group(1)
        window = text[max(0, match.start() - 400) : match.end() + 800]
        prev = found.get(item_id)
        age = _age_from_window(window)
        title = _first_title(window)
        if prev is None or (age is not None and prev.age_days is None):
            found[item_id] = ActiveListing(
                item_id=item_id,
                title=title or (prev.title if prev else ""),
                age_days=age if age is not None else (prev.age_days if prev else None),
                age_text=_age_text_from_window(window)
                or (prev.age_text if prev else ""),
                source=source,
            )
    return list(found.values())


def _looks_listing_ish(node: dict[str, Any]) -> bool:
    keys = set(node.keys())
    return bool(
        keys
        & {
            "marketplace_listing_title",
            "listing_price",
            "for_sale_item",
            "is_live",
            "is_pending",
            "is_sold",
        }
    )


def _title_from_node(node: dict[str, Any]) -> str:
    for key in (
        "marketplace_listing_title",
        "custom_title",
        "attribute_data_title",
        "title",
        "name",
    ):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _first_unix(*values: Any) -> float | None:
    for value in values:
        if value is None or value == "":
            continue
        try:
            num = float(value)
        except (TypeError, ValueError):
            continue
        if num > 1e12:  # ms
            num = num / 1000.0
        if num > 1e9:
            return num
    return None


def _age_days_from_unix(ts: float) -> float:
    now = datetime.now(timezone.utc).timestamp()
    return max(0.0, (now - float(ts)) / 86400.0)


def _age_from_window(window: str) -> float | None:
    match = _CREATION_NEAR_ID_RE.search(window)
    if not match:
        return None
    ts = _first_unix(match.group(1))
    return _age_days_from_unix(ts) if ts else None


def _age_text_from_window(window: str) -> str:
    age = _age_from_window(window)
    if age is None:
        return ""
    return f"creation_time age={age:.1f}d"


def _first_title(window: str) -> str:
    match = _TITLE_RE.search(window)
    if not match:
        return ""
    raw = match.group(1)
    try:
        return json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        return raw.replace("\\/", "/").replace('\\"', '"')


def _find_end_cursor(payload: Any) -> str | None:
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "end_cursor" in node and node["end_cursor"]:
                found.append(str(node["end_cursor"]))
            page_info = node.get("page_info")
            if isinstance(page_info, dict) and page_info.get("end_cursor"):
                found.append(str(page_info["end_cursor"]))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return found[-1] if found else None


def _dig_path(payload: Any, path: Any) -> Any:
    if not path or not isinstance(path, str):
        return None
    node: Any = payload
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node
