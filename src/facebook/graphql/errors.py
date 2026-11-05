from __future__ import annotations

import json
import re
from typing import Any

from src.facebook.errors import FacebookPostingError, FacebookSessionError


class FacebookGraphqlFallback(Exception):
    """Signal callers to fall back to Playwright UI automation.

    Not a ``FacebookPostingError`` so create/delete wrappers can catch it
    without treating missing templates as hard SKIP_CREATE failures.
    """


class FacebookGraphqlError(FacebookPostingError):
    """GraphQL HTTP/JSON failure with optional parsed payload."""

    def __init__(
        self,
        message: str,
        *,
        payload: Any = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.payload = payload
        self.status_code = status_code


_DUPLICATE_MARKERS = (
    "duplicate",
    "duplicad",
    "already listed",
    "already have a listing",
    "you've already",
    "you already",
    "publicación duplicada",
    "publicacion duplicada",
    "parece un duplicado",
    "anuncio duplicado",
    "looks like a duplicate",
    "looks like you already",
    # Exact Marketplace create banner (account_1 EN):
    # "This looks like a duplicate so we didn't post it. Delete it and try again"
    "didn't post it",
    "did not post it",
    "we didn't post",
    "delete it and try again",
    "delete and try again",
    "no lo publicamos",
    "no la publicamos",
    "no se publicó porque parece",
    "elimínala y vuelve a intentarlo",
    "elimínalo y vuelve a intentarlo",
    "duplicate_listing",
    "listing_duplicate",
)

_SESSION_MARKERS = (
    "not logged in",
    "session expired",
    "login required",
    "checkpoint",
    "please log in",
    "inicia sesión",
)

# FB rejects delete on phantom / archived / already-gone selling-hub ghosts.
_FIELD_EXCEPTION_MARKERS = (
    "field_exception",
    "field exception",
    "is not a valid for_sale_item",
    "not a valid marketplace",
    "does not exist",
    "no longer available",
    "cannot delete",
    "unable to delete",
)


def _flatten_strings(obj: Any, *, limit: int = 80) -> list[str]:
    out: list[str] = []

    def walk(node: Any) -> None:
        if len(out) >= limit:
            return
        if isinstance(node, str):
            text = node.strip()
            if text:
                out.append(text)
            return
        if isinstance(node, dict):
            for key in ("message", "description", "summary", "title", "error", "code"):
                if key in node:
                    walk(node[key])
            for value in node.values():
                walk(value)
            return
        if isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(obj)
    return out


def payload_text_blob(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    parts = _flatten_strings(payload)
    if parts:
        return " | ".join(parts)
    try:
        return json.dumps(payload, ensure_ascii=False)[:4000]
    except (TypeError, ValueError):
        return str(payload)


def is_duplicate_graphql_payload(payload: Any) -> bool:
    blob = payload_text_blob(payload).lower()
    return any(marker in blob for marker in _DUPLICATE_MARKERS)


def is_session_graphql_payload(payload: Any) -> bool:
    blob = payload_text_blob(payload).lower()
    return any(marker in blob for marker in _SESSION_MARKERS)


def is_field_exception_payload(payload: Any) -> bool:
    """True when FB returns ``field_exception`` (phantom/archived listing delete)."""
    blob = payload_text_blob(payload).lower()
    if "field_exception" in blob or "field exception" in blob:
        return True
    return any(marker in blob for marker in _FIELD_EXCEPTION_MARKERS)


def is_phantom_delete_error(exc: BaseException | str | None = None, payload: Any = None) -> bool:
    """True when a delete mutation failed because the item is not deletable via GraphQL."""
    if payload is not None and is_field_exception_payload(payload):
        return True
    if isinstance(exc, FacebookGraphqlError) and is_field_exception_payload(exc.payload):
        return True
    if exc is not None and is_field_exception_payload(str(exc)):
        return True
    return False


_RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate_limit",
    "ratelimit",
    "too many requests",
    "too many request",
    "try again later",
    "slow down",
    "temporarily blocked",
    "request limit",
    "429",
)


def is_rate_limit_error(
    exc: BaseException | str | None = None, payload: Any = None
) -> bool:
    """True when FB throttles GraphQL/HTTP (caller should fall back to Playwright)."""
    if isinstance(exc, FacebookGraphqlError) and exc.status_code == 429:
        return True
    blob = ""
    if payload is not None:
        blob = payload_text_blob(payload).lower()
    if exc is not None:
        blob = f"{blob} {str(exc).lower()}"
    return any(marker in blob for marker in _RATE_LIMIT_MARKERS)


def skipped_duplicate_message(detail: str = "", *, action: str = "") -> str:
    """Canonical log/exception text for FB duplicate create rejects."""
    bits = ["SKIPPED_DUPLICATE"]
    if action:
        bits.append(action)
    if detail:
        bits.append(detail.strip()[:240])
    return ": ".join(bits)


def raise_for_graphql_payload(payload: Any, *, action: str = "graphql") -> None:
    """Raise typed errors when FB returns error / duplicate payloads."""
    if payload is None:
        return
    if is_duplicate_graphql_payload(payload):
        raise FacebookPostingError(
            skipped_duplicate_message(
                "Facebook rejected create as duplicate listing",
                action=action,
            )
        )
    errors = None
    if isinstance(payload, dict):
        errors = payload.get("errors")
    if not errors:
        return
    blob = payload_text_blob(errors)
    if is_duplicate_graphql_payload(errors) or is_duplicate_graphql_payload(blob):
        raise FacebookPostingError(
            skipped_duplicate_message(blob[:200], action=action)
        )
    if is_session_graphql_payload(errors) or is_session_graphql_payload(blob):
        raise FacebookSessionError(
            f"Facebook session expired during GraphQL {action}: {blob[:240]}"
        )
    raise FacebookGraphqlError(
        f"GraphQL {action} errors: {blob[:400]}",
        payload=payload,
    )


_ITEM_ID_RE = re.compile(r"(?:marketplace/item/|/item/)(\d{8,})")


def extract_listing_id_from_payload(payload: Any) -> str | None:
    """Best-effort marketplace item id from a create/delete GraphQL response."""
    if payload is None:
        return None
    patterns = (
        re.compile(r'"marketplace_listing_id"\s*:\s*"?(\d{8,})"?'),
        re.compile(r'"listing_id"\s*:\s*"?(\d{8,})"?'),
        re.compile(r'"for_sale_item_id"\s*:\s*"?(\d{8,})"?'),
        re.compile(r'"story_id"\s*:\s*"?(\d{8,})"?'),
        re.compile(r'"product_item_id"\s*:\s*"?(\d{8,})"?'),
        re.compile(r'"id"\s*:\s*"?(\d{8,})"?'),
    )
    try:
        blob = json.dumps(payload) if not isinstance(payload, str) else payload
    except (TypeError, ValueError):
        blob = str(payload)
    for pattern in patterns:
        match = pattern.search(blob)
        if match:
            return match.group(1)
    match = _ITEM_ID_RE.search(blob)
    return match.group(1) if match else None
