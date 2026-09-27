"""Parsed webform lead from an inbound notification email."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class WebLead:
    name: str
    phone: str
    email: str = ""
    vehicle: str = ""
    message: str = ""
    branch: str = "periferico"
    subject: str = ""
    message_id: str = ""
    raw_from: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.name.strip() and self.phone.strip())

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
