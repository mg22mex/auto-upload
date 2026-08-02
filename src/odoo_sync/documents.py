"""Document / file attachments on crm.lead via ir.attachment."""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Protocol


class _OdooSession(Protocol):
    dry_run: bool

    def execute_kw(
        self,
        model: str,
        method: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> Any: ...

    def _use_dry_run(self, dry_run: bool | None) -> bool: ...


class DocumentsMixin:
    """Attach PDFs / IDs / receipts to leads (and other records)."""

    def attach_file(
        self: _OdooSession,
        *,
        model: str,
        res_id: int,
        filename: str,
        content: bytes,
        mimetype: str = "application/pdf",
        dry_run: bool | None = None,
    ) -> int:
        """Create ``ir.attachment`` linked to ``model`` / ``res_id``. Returns id."""
        use_dry = self._use_dry_run(dry_run)
        if not model or not str(model).strip():
            raise ValueError("model is required")
        if not filename or not str(filename).strip():
            raise ValueError("filename is required")
        if not content:
            raise ValueError("attachment content is empty")

        if use_dry:
            print(
                f"DRY-RUN attach_file model={model} res_id={res_id} "
                f"filename={filename!r} bytes={len(content)}"
            )
            return -1

        vals = {
            "name": str(filename).strip(),
            "res_model": str(model).strip(),
            "res_id": int(res_id),
            "type": "binary",
            "datas": base64.b64encode(content).decode("ascii"),
            "mimetype": mimetype or "application/octet-stream",
        }
        try:
            att_id = int(self.execute_kw("ir.attachment", "create", [vals]))
            print(
                f"Attached file ir.attachment id={att_id} "
                f"to {model}({res_id})"
            )
            return att_id
        except Exception as exc:
            raise RuntimeError(f"ir.attachment create failed: {exc}") from exc

    def attach_document_to_lead(
        self: _OdooSession,
        lead_id: int,
        file_path_or_bytes: str | Path | bytes,
        filename: str | None = None,
        *,
        mimetype: str | None = None,
        dry_run: bool | None = None,
    ) -> int | None:
        """Attach a document (path or bytes) to ``crm.lead``. Soft-fails → None."""
        use_dry = self._use_dry_run(dry_run)
        try:
            if isinstance(file_path_or_bytes, (str, Path)):
                path = Path(file_path_or_bytes)
                content = path.read_bytes()
                name = filename or path.name
            else:
                content = bytes(file_path_or_bytes)
                name = filename or "document.bin"
            if not name:
                raise ValueError("filename is required for byte attachments")

            mime = mimetype
            if mime is None:
                lower = name.lower()
                if lower.endswith(".pdf"):
                    mime = "application/pdf"
                elif lower.endswith((".jpg", ".jpeg")):
                    mime = "image/jpeg"
                elif lower.endswith(".png"):
                    mime = "image/png"
                else:
                    mime = "application/octet-stream"

            return self.attach_file(  # type: ignore[attr-defined]
                model="crm.lead",
                res_id=int(lead_id),
                filename=name,
                content=content,
                mimetype=mime,
                dry_run=use_dry,
            )
        except Exception as exc:
            print(f"WARN attach_document_to_lead lead={lead_id}: {exc}")
            return None
