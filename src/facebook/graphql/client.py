from __future__ import annotations

import json
import time
from typing import Any

import httpx

from src.facebook.errors import FacebookSessionError
from src.facebook.graphql.errors import (
    FacebookGraphqlError,
    raise_for_graphql_payload,
)
from src.facebook.graphql.session_ctx import (
    GRAPHQL_URL,
    FbSessionContext,
    browser_headers,
    jazoest_from_dtsg,
)
from src.facebook.util import env_float


class GraphQLClient:
    """httpx POST wrapper for ``https://www.facebook.com/api/graphql/``."""

    def __init__(
        self,
        session: FbSessionContext,
        *,
        timeout_sec: float | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.session = session
        self._owns_client = client is None
        timeout = timeout_sec if timeout_sec is not None else env_float("FB_GQL_TIMEOUT_SEC", 60.0)
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self._req_counter = 0

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> GraphQLClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def post(
        self,
        doc_id: str,
        variables: dict[str, Any] | list[Any] | str,
        *,
        friendly_name: str = "",
        check_errors: bool = True,
        require_scaffold: bool = False,
    ) -> dict[str, Any]:
        if not doc_id:
            raise FacebookGraphqlError("GraphQL doc_id is empty")

        self._req_counter += 1
        jazoest = self.session.jazoest or jazoest_from_dtsg(self.session.fb_dtsg)
        variables_json = (
            variables
            if isinstance(variables, str)
            else json.dumps(variables, separators=(",", ":"), ensure_ascii=False)
        )
        # Start from harvested Comet Relay extras when present (create needs them).
        form: dict[str, str] = {}
        if self.session.comet_scaffold:
            form.update(
                {
                    str(k): str(v)
                    for k, v in self.session.comet_scaffold.items()
                    if v is not None and str(v).strip()
                }
            )
            form["__spin_t"] = str(int(time.time()))
        elif require_scaffold:
            raise FacebookGraphqlError(
                "GraphQL create requires Comet scaffold (__dyn/__spin_*/__crn); "
                "open Marketplace composer once so harvest_comet_scaffold can run"
            )
        form.update(
            {
                "av": self.session.c_user,
                "fb_dtsg": self.session.fb_dtsg,
                "lsd": self.session.lsd or "",
                "jazoest": jazoest,
                "doc_id": str(doc_id),
                "variables": variables_json,
                "__a": "1",
                "__req": format(self._req_counter, "x"),
                "__rev": self.session.client_revision or "1",
                "__user": self.session.c_user,
                # Browser Comet always sends this on RelayModern POSTs.
                "server_timestamps": "true",
            }
        )
        if friendly_name:
            form["fb_api_req_friendly_name"] = friendly_name
            form["fb_api_caller_class"] = "RelayModern"

        headers = browser_headers(self.session, for_graphql=True)
        if friendly_name:
            headers["X-FB-Friendly-Name"] = friendly_name
        response = self._client.post(GRAPHQL_URL, data=form, headers=headers)

        if response.status_code in {401, 403}:
            raise FacebookSessionError(
                f"GraphQL auth failed HTTP {response.status_code} "
                f"(c_user={self.session.c_user})"
            )
        if response.status_code >= 400:
            raise FacebookGraphqlError(
                f"GraphQL HTTP {response.status_code}: {response.text[:300]}",
                status_code=response.status_code,
            )

        payload = parse_graphql_response_text(response.text)
        if check_errors:
            action = friendly_name or doc_id
            raise_for_graphql_payload(payload, action=action)
        if not isinstance(payload, dict):
            raise FacebookGraphqlError(
                f"GraphQL response was not a JSON object: {type(payload).__name__}",
                payload=payload,
            )
        return payload


def parse_graphql_response_text(text: str) -> Any:
    """Strip FB ``for (;;);`` anti-JSONP prefix and parse JSON (or NDJSON first object)."""
    raw = (text or "").strip()
    if not raw:
        raise FacebookGraphqlError("Empty GraphQL response body")

    if raw.startswith("for (;;);"):
        raw = raw[len("for (;;);") :].lstrip()

    json_start = raw.find("{")
    if json_start < 0:
        json_start = raw.find("[")
    if json_start > 0:
        raw = raw[json_start:]

    # Some FB responses are newline-delimited JSON objects.
    decoder = json.JSONDecoder()
    try:
        obj, _idx = decoder.raw_decode(raw)
        return obj
    except json.JSONDecodeError:
        pass

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("for (;;);"):
            line = line[len("for (;;);") :].lstrip()
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue

    raise FacebookGraphqlError(
        f"Failed to parse GraphQL JSON: {raw[:240]!r}",
        payload=raw[:1000],
    )
