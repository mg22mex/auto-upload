"""Direct Facebook Marketplace GraphQL (httpx) — bypass fragile UI clicks.

Requires captured ``doc_id`` templates under ``data/graphql/`` (see
``scripts/fb_capture_graphql.py``). Enable with ``FB_USE_GRAPHQL=1``.
"""

from __future__ import annotations

from src.facebook.graphql.client import GraphQLClient
from src.facebook.graphql.errors import (
    FacebookGraphqlError,
    FacebookGraphqlFallback,
    is_duplicate_graphql_payload,
)
from src.facebook.graphql.session_ctx import FbSessionContext, extract_session_context

__all__ = [
    "FbSessionContext",
    "FacebookGraphqlError",
    "FacebookGraphqlFallback",
    "GraphQLClient",
    "extract_session_context",
    "is_duplicate_graphql_payload",
]
