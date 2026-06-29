from __future__ import annotations

import re
from dataclasses import dataclass, field

from playwright.sync_api import Page, Response

ITEM_URL_PATTERN = re.compile(r"/marketplace/item/(\d+)")
LISTING_ID_PATTERNS = (
    re.compile(r'"listing_id"\s*:\s*"?(\d+)"?'),
    re.compile(r'"story_id"\s*:\s*"?(\d+)"?'),
    re.compile(r'"product_item_id"\s*:\s*"?(\d+)"?'),
    re.compile(r'"for_sale_item_id"\s*:\s*"?(\d+)"?'),
)


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
            url = response.url.lower()
            if not any(token in url for token in ("graphql", "marketplace", "commerce")):
                return
            body = response.text()
        except Exception:
            return

        for pattern in (ITEM_URL_PATTERN, *LISTING_ID_PATTERNS):
            for match in pattern.finditer(body):
                item_id = match.group(1)
                if item_id and item_id not in self.item_ids:
                    self.item_ids.append(item_id)

    def latest_url(self) -> str | None:
        if not self.item_ids:
            return None
        return f"https://www.facebook.com/marketplace/item/{self.item_ids[-1]}/"
