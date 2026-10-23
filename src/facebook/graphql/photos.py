"""Marketplace composer photo upload over HTTP (no Playwright file input)."""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from src.facebook.errors import FacebookPostingError
from src.facebook.graphql.errors import (
    FacebookGraphqlError,
    FacebookGraphqlFallback,
    is_duplicate_graphql_payload,
)
from src.facebook.graphql.session_ctx import (
    FbSessionContext,
    jazoest_from_dtsg,
    session_to_httpx_cookies,
)
from src.facebook.graphql.templates import load_template
from src.facebook.util import env_str

# Comet multipart upload (returns photoID). Prefer upload.* host (cross-origin XHR).
# Do NOT use legacy composerx/saveunpublished — HTTP 404.
_PRIMARY_UPLOAD_URL = (
    "https://upload.facebook.com/ajax/react_composer/attachments/photo/upload"
)
_DEFAULT_UPLOAD_URLS = (
    _PRIMARY_UPLOAD_URL,
    "https://www.facebook.com/ajax/react_composer/attachments/photo/upload",
)

_DEAD_UPLOAD_MARKERS = (
    "composerx/attachment",
    "composerv/x/attachment",
    "composerv/",
    "saveunpublished",
    "media/saveunpublished",
)

# Comet uses ``farr`` as the file part name. Extra names are last-resort only.
_FILE_FIELD_PRIMARY = "farr"
_FILE_FIELD_FALLBACKS = ("file", "0")

# Two known-working Comet multipart strategies (soft-reject → try the other).
_STRATEGY_QUERY_DTSG = "query_dtsg"  # P.A.I.F: fb_dtsg on query only
_STRATEGY_BODY_DTSG = "body_dtsg"  # fbchat: fb_dtsg + __user + jazoest in body

_PHOTO_ID_RE = re.compile(
    r'"(?:photoID|photo_id|fbid|media_id)"\s*:\s*"?(\d{6,})"?',
    re.I,
)
_SOFT_REJECT_MARKERS = (
    "couldn't be processed",
    "could not be processed",
    "request couldn't be processed",
    "there was a problem with this request",
    "something went wrong",
    "errorSummary",
)


def resolve_photo_upload_config(root: Path | None = None) -> dict[str, Any]:
    """Load ``photo_upload.json`` or synthesize a working ajax upload config."""
    template = load_template("photo_upload", root=root, required=False) or {}
    url = str(template.get("url") or "").strip()
    env_url = env_str("FB_GQL_PHOTO_UPLOAD_URL", "")
    if env_url:
        url = env_url

    looks_like_graphql_query = bool(template.get("doc_id")) and not url
    friendly = str(template.get("friendly_name") or "")
    if looks_like_graphql_query or "SellerHub" in friendly or "Pagination" in friendly:
        print(
            "  photo_upload.json is a GraphQL query capture (no upload url) — "
            "using default Marketplace ajax photo upload",
            flush=True,
        )
        url = ""

    if url and _is_dead_upload_url(url):
        print(
            f"  ignoring dead photo upload url {url!r} — "
            f"using {_PRIMARY_UPLOAD_URL}",
            flush=True,
        )
        url = ""

    if not url:
        url = _PRIMARY_UPLOAD_URL

    field_name = str(
        template.get("field_name") or env_str("FB_GQL_PHOTO_FIELD", _FILE_FIELD_PRIMARY)
    )
    form_fields = template.get("form_fields") if isinstance(template.get("form_fields"), dict) else {}
    # Minimal Comet fields — template may override source / waterfallxapp.
    merged_fields = {
        "source": "8",
        "waterfallxapp": "comet",
        **{str(k): str(v) for k, v in form_fields.items()},
    }
    # Strip auth/CSRF that strategies place intentionally (query vs body).
    for banned in (
        "fb_dtsg",
        "lsd",
        "jazoest",
        "__user",
        "__a",
        "__req",
        "__rev",
        "Cookie",
        "profile_id",
        "upload_id",
    ):
        merged_fields.pop(banned, None)

    # Live composer response:
    #   for (;;);{"__ar":1,"payload":{"photoID":"1661…","width":1200,…},…}
    id_paths = template.get("id_paths") or [
        "payload.photoID",
        "payload.originalPhotoID",
        "payload.fbid",
        "photoID",
        "fbid",
    ]
    alt_urls: list[str] = []
    for candidate in list(template.get("alt_urls") or []) + list(_DEFAULT_UPLOAD_URLS):
        candidate = str(candidate).strip()
        if not candidate or candidate == url or _is_dead_upload_url(candidate):
            continue
        if candidate not in alt_urls:
            alt_urls.append(candidate)

    field_names = list(dict.fromkeys([field_name, _FILE_FIELD_PRIMARY, *_FILE_FIELD_FALLBACKS]))

    return {
        "url": url,
        "alt_urls": alt_urls,
        "field_name": field_name,
        "alt_field_names": field_names,
        "form_fields": merged_fields,
        "id_paths": [str(p) for p in id_paths],
    }


def _is_dead_upload_url(url: str) -> bool:
    low = (url or "").lower()
    return any(marker in low for marker in _DEAD_UPLOAD_MARKERS)


def upload_photos_for_listing(
    session: FbSessionContext,
    photo_paths: list[Path],
    *,
    root: Path | None = None,
    autosell_id: str = "",
) -> list[str]:
    """Upload local JPEGs; return Facebook photo ids for create mutation."""
    if not photo_paths:
        raise FacebookPostingError(f"No photos to upload for {autosell_id or 'listing'}")
    if not session.fb_dtsg:
        raise FacebookGraphqlError("Cannot upload photos without fb_dtsg")
    if not session.c_user or not session.xs:
        raise FacebookGraphqlError("Cannot upload photos without c_user/xs cookies")

    cfg = resolve_photo_upload_config(root)
    photo_ids: list[str] = []
    urls = [cfg["url"], *cfg["alt_urls"]]
    field_names: list[str] = list(cfg["alt_field_names"])

    print(
        f"  {autosell_id or 'photo'}: upload endpoint={urls[0]} "
        f"(alts={len(urls) - 1}) field={field_names[0]}",
        flush=True,
    )

    cookies = session_to_httpx_cookies(session)
    with httpx.Client(
        timeout=120.0,
        follow_redirects=True,
        cookies=cookies,
    ) as client:
        for path in photo_paths:
            photo_id = _upload_one(
                client,
                session,
                path,
                urls=urls,
                field_names=field_names,
                form_fields=cfg["form_fields"],
                id_paths=cfg["id_paths"],
                autosell_id=autosell_id,
            )
            photo_ids.append(photo_id)
            print(
                f"  {autosell_id or 'photo'}: uploaded {path.name} → id={photo_id}",
                flush=True,
            )
    if not photo_ids:
        raise FacebookGraphqlFallback(
            "Photo upload produced no ids — capture a real composer upload "
            "into data/graphql/photo_upload.json"
        )
    return photo_ids


def _upload_headers(session: FbSessionContext, *, upload_host: str) -> dict[str, str]:
    """Browser-like headers for Comet photo XHR.

    Cookies go both in the httpx jar and as an explicit ``Cookie`` header
    (same pattern as GraphQLClient — jar alone has missed soft rejects).
    """
    # upload.facebook.com is same-site with www (eTLD+1), not cross-site.
    if upload_host.endswith("upload.facebook.com"):
        fetch_site = "same-site"
    else:
        fetch_site = "same-origin"
    headers: dict[str, str] = {
        "User-Agent": session.user_agent,
        "Accept": "*/*",
        "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
        # Always the page origin — never upload.facebook.com.
        "Origin": "https://www.facebook.com",
        "Referer": "https://www.facebook.com/marketplace/create/vehicle",
        "Cookie": session.cookie_header,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": fetch_site,
        "sec-ch-ua-mobile": "?0",
    }
    if session.lsd:
        headers["X-FB-LSD"] = session.lsd
    return headers


def _with_upload_query(
    url: str,
    session: FbSessionContext,
    *,
    strategy: str,
) -> str:
    """Attach query params for the chosen CSRF strategy.

    ``query_dtsg``: ``__a=1&fb_dtsg=…`` only (P.A.I.F / browser XHR).
    ``body_dtsg``: ``__a=1`` only — tokens ride in multipart.
    """
    parsed = urlparse(url)
    q = dict(parse_qsl(parsed.query, keep_blank_values=True))
    q["__a"] = "1"
    # Drop leftovers that trigger soft rejects when misplaced.
    for drop in ("fb_dtsg", "__user", "__rev", "jazoest", "lsd", "__req"):
        q.pop(drop, None)
    if strategy == _STRATEGY_QUERY_DTSG:
        q["fb_dtsg"] = session.fb_dtsg
    return urlunparse(parsed._replace(query=urlencode(q)))


def _form_and_file_parts(
    session: FbSessionContext,
    *,
    form_fields: dict[str, Any],
    field_name: str,
    filename: str,
    data_bytes: bytes,
    strategy: str = _STRATEGY_QUERY_DTSG,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Split text fields vs file part for httpx multipart.

    Shared body (both strategies):
      profile_id, source=8, waterfallxapp=comet, upload_id + ``farr`` file

    ``query_dtsg``: CSRF only on query string (no fb_dtsg/lsd/__user in body).
    ``body_dtsg``: also ship fb_dtsg, lsd, __user, __a, __req, __rev, jazoest
    (fbchat_muqit ``get_params()`` merge).
    """
    data: dict[str, str] = {
        "profile_id": session.c_user,
        "source": "8",
        "waterfallxapp": "comet",
        "upload_id": f"jsc_c_{uuid.uuid4().hex[:8]}",
    }
    for key, value in form_fields.items():
        data[str(key)] = str(value).replace("{{c_user}}", session.c_user)

    if strategy == _STRATEGY_BODY_DTSG:
        jazoest = session.jazoest or jazoest_from_dtsg(session.fb_dtsg)
        data.update(
            {
                "fb_dtsg": session.fb_dtsg,
                "__user": session.c_user,
                "__a": "1",
                "__req": "1",
                "__rev": str(session.client_revision or "1"),
                "jazoest": jazoest,
            }
        )
        if session.lsd:
            data["lsd"] = session.lsd

    safe_name = filename if "." in filename else f"{filename}.jpg"
    if not safe_name.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
        safe_name = f"{Path(safe_name).stem}.jpg"
    content_type = "image/jpeg"
    if safe_name.lower().endswith(".png"):
        content_type = "image/png"
    elif safe_name.lower().endswith(".webp"):
        content_type = "image/webp"

    files = {field_name: (safe_name, data_bytes, content_type)}
    return data, files


def _upload_one(
    client: httpx.Client,
    session: FbSessionContext,
    path: Path,
    *,
    urls: list[str],
    field_names: list[str],
    form_fields: dict[str, Any],
    id_paths: list[str],
    autosell_id: str,
) -> str:
    data_bytes = path.read_bytes()
    if len(data_bytes) < 100:
        raise FacebookGraphqlError(f"Photo file too small/empty: {path.name}")
    if len(data_bytes) > 4_000_000:
        print(
            f"  WARN {autosell_id or path.name}: photo {path.name} is "
            f"{len(data_bytes) // 1024}KB (>4MB may fail)",
            flush=True,
        )

    strategies = (_STRATEGY_QUERY_DTSG, _STRATEGY_BODY_DTSG)
    last_error = ""
    for url in urls:
        if _is_dead_upload_url(url):
            continue
        host = urlparse(url).hostname or ""
        headers = _upload_headers(session, upload_host=host)

        for strategy in strategies:
            post_url = _with_upload_query(url, session, strategy=strategy)
            for field_name in field_names:
                data, files = _form_and_file_parts(
                    session,
                    form_fields=form_fields,
                    field_name=field_name,
                    filename=path.name,
                    data_bytes=data_bytes,
                    strategy=strategy,
                )
                try:
                    # data= text fields + files= binary → proper multipart/form-data
                    response = client.post(
                        post_url, data=data, files=files, headers=headers
                    )
                except httpx.HTTPError as exc:
                    last_error = f"{url} ({field_name}/{strategy}): {exc}"
                    continue

                body_preview = (response.text or "")[:300]
                if response.status_code >= 400:
                    last_error = (
                        f"{url} ({field_name}/{strategy}): HTTP "
                        f"{response.status_code} {body_preview!r}"
                    )
                    continue

                payload = _parse_upload_payload(response.text)
                if is_duplicate_graphql_payload(payload):
                    raise FacebookPostingError(
                        f"SKIP_CREATE: Facebook duplicate listing detected during "
                        f"photo upload for {autosell_id or path.name}"
                    )
                soft = _soft_reject_message(payload, response.text or "")
                if soft:
                    last_error = f"{url} ({field_name}/{strategy}): {soft}"
                    continue
                if _upload_payload_error(payload):
                    last_error = (
                        f"{url} ({field_name}/{strategy}): "
                        f"{_upload_payload_error(payload)}"
                    )
                    continue
                photo_id = _extract_photo_id(payload, id_paths)
                if photo_id:
                    if strategy != _STRATEGY_QUERY_DTSG:
                        print(
                            f"  {autosell_id or path.name}: photo upload ok via "
                            f"{strategy} on {host}",
                            flush=True,
                        )
                    return photo_id
                last_error = (
                    f"{url} ({field_name}/{strategy}): no photo id in "
                    f"{str(payload)[:180]}"
                )
    raise FacebookGraphqlError(
        f"Photo upload failed for {path.name}: {last_error or 'unknown'}"
    )


def _soft_reject_message(payload: Any, raw_text: str) -> str:
    blob = f"{payload_text(payload)} {raw_text}".lower()
    if any(marker in blob for marker in _SOFT_REJECT_MARKERS):
        # Prefer FB's errorSummary when present.
        if isinstance(payload, dict):
            summary = payload.get("errorSummary") or payload.get("errorDescription")
            if summary:
                return str(summary)
        return "Your Request Couldn't be Processed (soft reject)"
    return ""


def payload_text(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(payload)


def _parse_upload_payload(text: str) -> Any:
    """Strip FB's ``for (;;);`` anti-hijack prefix, then JSON-decode."""
    raw = (text or "").strip()
    # Tolerate whitespace / BOM before the classic anti-CSRF prefix.
    if "for (;;);" in raw[:32]:
        raw = raw.split("for (;;);", 1)[1].lstrip()
    json_start = raw.find("{")
    if json_start >= 0:
        raw = raw[json_start:]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw[:2000]}


def _upload_payload_error(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    if payload.get("error") or payload.get("errorSummary"):
        return str(payload.get("errorSummary") or payload.get("error") or "error")
    errors = payload.get("errors")
    if errors:
        return str(errors)[:200]
    return ""


def _coerce_photo_id(value: Any) -> str | None:
    """Accept digit strings (and ints) of length ≥6; skip empty / junk."""
    if value is None:
        return None
    text = str(value).strip()
    if text.isdigit() and len(text) >= 6:
        return text
    return None


def _extract_photo_id(payload: Any, id_paths: list[str]) -> str | None:
    """Pull ``photoID`` from composer ajax envelope ``payload.photoID``."""
    # Canonical live shape first (ignore empty originalPhotoID).
    if isinstance(payload, dict):
        inner = payload.get("payload")
        if isinstance(inner, dict):
            for key in ("photoID", "photo_id", "fbid", "originalPhotoID"):
                photo_id = _coerce_photo_id(inner.get(key))
                if photo_id:
                    return photo_id
        photo_id = _coerce_photo_id(payload.get("photoID") or payload.get("fbid"))
        if photo_id:
            return photo_id

    for path in id_paths:
        node: Any = payload
        ok = True
        for part in str(path).split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                ok = False
                break
        if ok:
            photo_id = _coerce_photo_id(node)
            if photo_id:
                return photo_id
    try:
        blob = json.dumps(payload)
    except (TypeError, ValueError):
        blob = str(payload)
    match = _PHOTO_ID_RE.search(blob)
    if match:
        return match.group(1)
    return None
