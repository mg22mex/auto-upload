from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Vehicle:
    autosell_id: str
    slug: str
    title: str
    brand: str
    year: str
    price: str
    mileage: str
    version: str
    url: str
    image_urls: list[str] = field(default_factory=list)
    specs: dict[str, str] = field(default_factory=dict)

    @property
    def marketplace_title(self) -> str:
        parts = [self.year, self.brand, self.title]
        return " ".join(p for p in parts if p).strip()

    def content_hash(self) -> str:
        payload: dict[str, Any] = {
            "title": self.title,
            "brand": self.brand,
            "year": self.year,
            "price": self.price,
            "mileage": self.mileage,
            "version": self.version,
            "specs": self.specs,
            "image_urls": self.image_urls,
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["content_hash"] = self.content_hash()
        data["marketplace_title"] = self.marketplace_title
        return data


@dataclass
class SyncAction:
    action: str  # create | update | remove
    autosell_id: str
    account_id: str | None
    slug: str
    reason: str
    vehicle: Vehicle | None = None
