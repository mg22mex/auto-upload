from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

from playwright.sync_api import Error, Page, Response

try:
    from playwright._impl._errors import TargetClosedError
except ImportError:  # pragma: no cover
    TargetClosedError = Error  # type: ignore[misc, assignment]

logger = logging.getLogger(__name__)

ITEM_URL_PATTERN = re.compile(r"/marketplace/item/(\d+)")
LISTING_ID_PATTERNS = (
    re.compile(r'"listing_id"\s*:\s*"?(\d+)"?'),
    re.compile(r'"story_id"\s*:\s*"?(\d+)"?'),
    re.compile(r'"product_item_id"\s*:\s*"?(\d+)"?'),
    re.compile(r'"for_sale_item_id"\s*:\s*"?(\d+)"?'),
    re.compile(r'"marketplace_listing_id"\s*:\s*"?(\d+)"?'),
    re.compile(r'"listing"\s*:\s*\{[^}]*"id"\s*:\s*"?(\d+)"?'),
)

_RESPONSE_TEXT_ERRORS = (Error, TargetClosedError, asyncio.CancelledError)


@dataclass
class MarketplaceItemCapture:
    item_ids: list[str] = field(default_factory=list)
    _attached: bool = False

    def attach(self, page: Page) -> None:
        if self._attached:
            return
        page.on("response", self._on_response)
        self._attached = True

    def _on_response(self, response: Response) -> None:
        try:
            if response.status < 200 or response.status >= 400:
                return
            if "facebook.com" not in response.url:
                return
        except Exception:
            return

        try:
            body = response.text()
        except _RESPONSE_TEXT_ERRORS as exc:
            # Never let body reads crash the Playwright event loop during nav.
            url = "?"
            try:
                url = response.url
            except Exception:
                pass
            logger.debug(
                "marketplace response.text() failed/timed out url=%s: %s",
                url,
                exc,
                exc_info=True,
            )
            return

        for pattern in (ITEM_URL_PATTERN, *LISTING_ID_PATTERNS):
            for match in pattern.finditer(body):
                item_id = match.group(1)
                if item_id and len(item_id) >= 8 and item_id not in self.item_ids:
                    self.item_ids.append(item_id)

    def all_urls(self) -> list[str]:
        return [f"https://www.facebook.com/marketplace/item/{item_id}/" for item_id in self.item_ids]

    def latest_url(self) -> str | None:
        if not self.item_ids:
            return None
        return f"https://www.facebook.com/marketplace/item/{self.item_ids[-1]}/"
