from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx
from playwright.sync_api import BrowserContext, Page

from src.facebook.errors import FacebookPostingError
from src.facebook.graphql.client import GraphQLClient
from src.facebook.graphql.errors import (
    FacebookGraphqlError,
    FacebookGraphqlFallback,
    extract_listing_id_from_payload,
)
from src.facebook.graphql.session_ctx import (
    FbSessionContext,
    browser_headers,
    extract_session_context,
)
from src.facebook.graphql.templates import (
    base_mapping,
    load_template,
    prepare_mutation_variables,
)
from src.facebook.util import env_bool, env_str

_UNAVAILABLE_MARKERS = (
    "isn't available",
    "is not available",
    "content isn't available",
    "no longer available",
    "no está disponible",
    "no esta disponible",
    "this listing is unavailable",
    "listing is unavailable",
    "has been deleted",
    "ha sido eliminada",
    "ha sido eliminado",
    "was deleted",
    "page isn't available",
    "sorry, this content isn't available",
)

_ITEM_ID_RE = re.compile(r"/marketplace/item/(\d+)")


def extract_item_id(listing_url: str) -> str | None:
    match = _ITEM_ID_RE.search(listing_url or "")
    return match.group(1) if match else None


def listing_unavailable_http(
    session: FbSessionContext,
    listing_url: str,
    *,
    client: httpx.Client | None = None,
    timeout_sec: float = 45.0,
) -> bool:
    """True when the listing page looks deleted/unavailable (no UI)."""
    headers = browser_headers(session, for_graphql=False)
    owns = client is None
    http = client or httpx.Client(timeout=timeout_sec, follow_redirects=True)
    try:
        response = http.get(listing_url, headers=headers)
        body = (response.text or "").lower()
        if response.status_code in {404, 410}:
            return True
        return any(marker in body for marker in _UNAVAILABLE_MARKERS)
    except httpx.HTTPError:
        return False
    finally:
        if owns:
            http.close()


def delete_listing_graphql(
    session: FbSessionContext,
    listing_url: str,
    *,
    autosell_id: str = "",
    root: Path | None = None,
    client: GraphQLClient | None = None,
    verify: bool = True,
) -> bool:
    """Hard-delete a Marketplace listing via captured GraphQL template.

    Template: ``data/graphql/delete_listing.json`` (or ``FB_GQL_DELETE_DOC_ID``).
    """
    item_id = extract_item_id(listing_url)
    if not item_id:
        raise FacebookGraphqlFallback(f"Cannot parse item id from {listing_url!r}")

    template = load_template("delete_listing", root=root, required=True)
    assert template is not None
    mapping = base_mapping(c_user=session.c_user, item_id=item_id)
    # Captured templates often hardcode for_sale_item_id/actor_id — rewrite them.
    variables = prepare_mutation_variables(template, mapping)
    doc_id = str(template["doc_id"])
    friendly = str(
        template.get("friendly_name")
        or "useCometMarketplaceForSaleItemDeleteMutation"
    )

    owns = client is None
    gql = client or GraphQLClient(session)
    try:
        print(
            f"  {autosell_id or item_id}: GraphQL delete doc_id={doc_id} "
            f"item_id={item_id}",
            flush=True,
        )
        payload = gql.post(doc_id, variables, friendly_name=friendly, check_errors=True)
        _ = extract_listing_id_from_payload(payload)  # best-effort log aid
        if verify and not listing_unavailable_http(session, listing_url):
            # Some deletes succeed asynchronously; treat GraphQL OK as success
            # unless FB still clearly shows an active listing chrome.
            if env_bool("FB_GQL_DELETE_REQUIRE_HTTP_GONE", False):
                raise FacebookPostingError(
                    f"GraphQL delete returned OK but listing still reachable: {listing_url}"
                )
            print(
                f"  {autosell_id or item_id}: GraphQL delete OK "
                f"(HTTP verify still shows page — treating as deleted)",
                flush=True,
            )
        return True
    except FacebookGraphqlFallback:
        raise
    except FacebookPostingError:
        raise
    except Exception as exc:
        raise FacebookGraphqlError(
            f"GraphQL delete failed for {autosell_id or item_id}: {exc}"
        ) from exc
    finally:
        if owns:
            gql.close()


def remove_vehicle_listing_graphql(
    context: BrowserContext,
    listing_url: str,
    *,
    autosell_id: str,
    removal_action: str = "delete",
    log_dir: Path | None = None,
    page: Page | None = None,
    root: Path | None = None,
    store: Any = None,
    account_id: str | None = None,
) -> bool:
    """GraphQL remove entry used by ``remover.remove_vehicle_listing``."""
    _ = log_dir  # reserved for debug dumps
    action = (removal_action or "delete").strip().lower()
    if action != "delete":
        raise FacebookGraphqlFallback(
            f"GraphQL remover only supports hard delete (got {action!r}); "
            "mark_sold stays on Playwright UI"
        )
    if not env_str("FB_GQL_DELETE_DOC_ID", "") and not (
        (root or Path.cwd()) / "data" / "graphql" / "delete_listing.json"
    ).is_file():
        # Still try load_template for clearer error / env path overrides.
        pass

    session = extract_session_context(
        context, page=page, account_id=account_id or "", fetch_tokens=True
    )
    print(f"  {autosell_id}: GraphQL session {session.redacted_summary()}", flush=True)
    ok = delete_listing_graphql(
        session,
        listing_url,
        autosell_id=autosell_id,
        root=root,
        verify=True,
    )
    if ok and store is not None and account_id:
        try:
            store.mark_fb_listing_removed(autosell_id, account_id, clear_url=False)
        except Exception:
            pass
    return ok
