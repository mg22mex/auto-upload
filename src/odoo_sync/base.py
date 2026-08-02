"""Shared Odoo XML-RPC session — auth, execute_kw, dry_run."""
from __future__ import annotations

import os
import xmlrpc.client
from typing import Any


class OdooCRMError(RuntimeError):
    """Raised when Odoo auth or RPC calls fail."""


class OdooClient:
    """Central XML-RPC session. Secrets from env only. Sub-mixins share this."""

    ENV_URL = "ODOO_URL"
    ENV_DB = "ODOO_DB"
    ENV_USER = "ODOO_USERNAME"
    ENV_KEY = "ODOO_API_KEY"
    ENV_DEFAULT_ACTIVITY_USER = "ODOO_ACTIVITY_USER_ID"

    def __init__(
        self,
        *,
        url: str | None = None,
        db: str | None = None,
        username: str | None = None,
        api_key: str | None = None,
        common: Any | None = None,
        models: Any | None = None,
        dry_run: bool = False,
    ) -> None:
        self.url = (os.getenv(self.ENV_URL, "") if url is None else url).rstrip("/")
        self.db = os.getenv(self.ENV_DB, "") if db is None else db
        self.username = (
            (
                os.getenv(self.ENV_USER, "")
                or os.getenv("ODOO_USER", "")
            )
            if username is None
            else username
        )
        self.api_key = (
            (
                os.getenv(self.ENV_KEY, "")
                or os.getenv("ODOO_PASSWORD", "")
            )
            if api_key is None
            else api_key
        )
        self.uid: int | None = None
        self._common = common
        self._models = models
        self.dry_run = bool(dry_run) or (
            (os.getenv("ODOO_DRY_RUN") or "").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self._rr_cursor: dict[int, int] = {}

    def authenticate(self) -> int:
        """Authenticate; set uid."""
        missing = [
            name
            for name, val in (
                (self.ENV_URL, self.url),
                (self.ENV_DB, self.db),
                (self.ENV_USER, self.username),
                (self.ENV_KEY, self.api_key),
            )
            if not val
        ]
        if missing:
            raise OdooCRMError(f"Missing Odoo env: {', '.join(missing)}")

        common = self._common or xmlrpc.client.ServerProxy(
            f"{self.url}/xmlrpc/2/common", allow_none=True
        )
        self._common = common
        uid = common.authenticate(self.db, self.username, self.api_key, {})
        if not uid:
            raise OdooCRMError("Odoo authenticate failed (check DB/user/API key)")
        self.uid = int(uid)
        if self._models is None:
            self._models = xmlrpc.client.ServerProxy(
                f"{self.url}/xmlrpc/2/object", allow_none=True
            )
        return self.uid

    def _ensure_auth(self) -> tuple[int, Any]:
        if self.uid is None or self._models is None:
            self.authenticate()
        assert self.uid is not None and self._models is not None
        return self.uid, self._models

    def execute_kw(
        self,
        model: str,
        method: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> Any:
        uid, models = self._ensure_auth()
        return models.execute_kw(
            self.db,
            uid,
            self.api_key,
            model,
            method,
            args or [],
            kwargs or {},
        )

    def _use_dry_run(self, dry_run: bool | None) -> bool:
        return self.dry_run if dry_run is None else bool(dry_run)
