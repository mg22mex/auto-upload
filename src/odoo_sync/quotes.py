"""Automated quote PDF rendering and delivery to CRM lead chatter.

Wraps ``pdf_engine`` (ReportLab when installed) and ``DocumentsMixin`` /
``post_quote_to_chatter`` for branch-branded quote sheets.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.odoo_sync.client import OdooCRMClient
from src.odoo_sync.crm import (
    PRIMARY_BRANCH,
    PLACEHOLDER_BRANCH,
    infer_physical_location,
    normalize_crm_branch,
)

# Per-branch dealership presentation (header/footer contact block).
BRANCH_BRANDING: dict[str, dict[str, str]] = {
    PRIMARY_BRANCH: {
        "brand": "Autosell MX",
        "branch_label": "Periférico",
        "phone": "Tel. (614) 410-0000",
        "email": "periferico@autosell.mx",
        "web": "https://www.autosell.mx",
        "city": "Av. Periférico de la Juventud, Chihuahua, MX",
        "address": "Av. Periférico de la Juventud, Chihuahua, MX",
    },
    PLACEHOLDER_BRANCH: {
        "brand": "Autosell MX",
        "branch_label": "San Felipe",
        "phone": "Tel. (614) 420-0000",
        "email": "sanfelipe@autosell.mx",
        "web": "https://www.autosell.mx",
        "city": "San Felipe, Chihuahua, MX",
        "address": "San Felipe, Chihuahua, MX",
    },
}


def resolve_quote_branch(
    branch: str | None = None,
    *,
    physical_location: str | None = None,
    vehicle: dict[str, Any] | None = None,
    lead: dict[str, Any] | None = None,
) -> str:
    """Pick branding branch: physical location wins, then explicit branch."""
    for candidate in (
        physical_location,
        (lead or {}).get("physical_location"),
        (lead or {}).get("branch"),
        (vehicle or {}).get("physical_location"),
        (vehicle or {}).get("location"),
        (vehicle or {}).get("ubicacion"),
        branch,
    ):
        if not candidate:
            continue
        inferred = infer_physical_location(str(candidate))
        if inferred:
            return inferred
        norm = normalize_crm_branch(str(candidate))
        if norm in BRANCH_BRANDING:
            return norm
    return PRIMARY_BRANCH


def branding_for_branch(branch: str) -> dict[str, str]:
    key = normalize_crm_branch(branch)
    if key not in BRANCH_BRANDING:
        key = PRIMARY_BRANCH
    return dict(BRANCH_BRANDING[key])


def reportlab_available() -> bool:
    try:
        from src.pdf_engine.generator import _HAS_REPORTLAB

        return bool(_HAS_REPORTLAB)
    except ImportError:
        return False


def _fallback_pdf_bytes(
    *,
    vehicle: dict[str, Any],
    quote: dict[str, Any],
    client: dict[str, Any],
    branding: dict[str, str],
) -> bytes:
    """Minimal PDF when ReportLab is missing (still valid %PDF- header)."""
    name = str(
        vehicle.get("name")
        or vehicle.get("vehicle_name")
        or vehicle.get("title")
        or "Vehiculo"
    )
    vin = str(vehicle.get("vin") or "n/a")
    client_name = str(client.get("name") or client.get("client_name") or "Cliente")
    branch = branding.get("branch_label", "Autosell")
    price = quote.get("vehicle_price", quote.get("price", "—"))
    monthly = quote.get("estimated_monthly_payment", "—")
    # Tiny valid PDF with visible text streams for simple tests.
    lines = [
        f"Autosell MX - {branch}",
        f"Cliente: {client_name}",
        f"Vehiculo: {name}",
        f"VIN: {vin}",
        f"Precio: {price}",
        f"Mensualidad: {monthly}",
        branding.get("city", "Chihuahua, MX"),
    ]
    # Escape for PDF literal string (latin-1-ish plain ASCII)
    safe = "\\n".join(
        re.sub(r"[^\x20-\x7E]", "?", line) for line in lines
    )
    stream = f"BT /F1 10 Tf 50 750 Td ({safe}) Tj ET"
    stream_bytes = stream.encode("latin-1", errors="replace")
    objects = [
        b"1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n",
        b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj\n",
        (
            b"3 0 obj<< /Type /Page /Parent 2 0 R "
            b"/MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>endobj\n"
        ),
        (
            f"4 0 obj<< /Length {len(stream_bytes)} >>stream\n".encode("ascii")
            + stream_bytes
            + b"\nendstream\nendobj\n"
        ),
        b"5 0 obj<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>endobj\n",
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for obj in objects:
        offsets.append(len(out))
        out.extend(obj)
    xref_pos = len(out)
    out.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(
        f"trailer<< /Size {len(offsets)} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n".encode("ascii")
    )
    return bytes(out)


@dataclass
class QuotePDFManager:
    """Render branch-branded quote PDFs and attach them to ``crm.lead``."""

    client: OdooCRMClient | None = None
    dry_run: bool | None = None
    _client: OdooCRMClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._client = self.client or OdooCRMClient()
        if self.dry_run is not None:
            self._client.dry_run = bool(self.dry_run)

    @property
    def odoo(self) -> OdooCRMClient:
        return self._client

    def _use_dry_run(self) -> bool:
        return self._client._use_dry_run(self.dry_run)

    def generate_quote_pdf(
        self,
        vehicle: dict[str, Any],
        quote: dict[str, Any],
        client: dict[str, Any] | None = None,
        *,
        branch: str | None = None,
        physical_location: str | None = None,
        lead: dict[str, Any] | None = None,
        valid_days: int = 7,
    ) -> dict[str, Any]:
        """Build PDF bytes for a vehicle quote.

        Returns::

            {
              "ok": bool,
              "pdf_bytes": bytes | None,
              "filename": str,
              "branch": str,
              "engine": "reportlab" | "fallback",
              "branding": {...},
              "error": str | None,
              "dry_run": bool,
            }
        """
        vehicle = vehicle or {}
        quote = quote or {}
        client = client or {}
        branch_key = resolve_quote_branch(
            branch,
            physical_location=physical_location,
            vehicle=vehicle,
            lead=lead,
        )
        branding = branding_for_branch(branch_key)
        contact = {**branding, **{k: v for k, v in client.items() if v}}

        sku = str(
            vehicle.get("sku")
            or vehicle.get("autosell_id")
            or vehicle.get("vin")
            or "vehicle"
        ).strip()
        safe_sku = re.sub(r"[^\w\-]+", "_", sku)[:40] or "vehicle"
        stamp = datetime.now().strftime("%Y%m%d")
        filename = f"cotizacion_{safe_sku}_{stamp}.pdf"

        use_dry = self._use_dry_run()
        engine = "reportlab" if reportlab_available() else "fallback"
        pdf_bytes: bytes | None = None
        error: str | None = None

        try:
            if reportlab_available():
                from src.pdf_engine.generator import build_quote_pdf_bytes

                # Surface VIN / photo count in vehicle block for advisors
                vehicle_payload = dict(vehicle)
                photos = vehicle.get("photos") or vehicle.get("image_urls") or []
                if photos and not vehicle_payload.get("features"):
                    vehicle_payload["features"] = vehicle_payload.get("features") or []
                if isinstance(photos, (list, tuple)) and photos:
                    feat = list(vehicle_payload.get("features") or [])
                    feat.append(f"Fotos: {len(photos)}")
                    vehicle_payload["features"] = feat
                if vehicle_payload.get("physical_location") is None:
                    vehicle_payload["physical_location"] = branch_key
                pdf_bytes = build_quote_pdf_bytes(
                    quote,
                    vehicle_payload,
                    contact=contact,
                    valid_days=valid_days,
                )
                engine = "reportlab"
            else:
                pdf_bytes = _fallback_pdf_bytes(
                    vehicle=vehicle,
                    quote=quote,
                    client=client,
                    branding=branding,
                )
                engine = "fallback"
                print(
                    "WARN QuotePDFManager: reportlab missing — "
                    "using minimal fallback PDF"
                )
        except Exception as exc:
            error = str(exc)
            print(f"WARN QuotePDFManager generate_quote_pdf: {exc}")
            # Last-resort fallback so dry pipelines still deliver *something*
            try:
                pdf_bytes = _fallback_pdf_bytes(
                    vehicle=vehicle,
                    quote=quote,
                    client=client,
                    branding=branding,
                )
                engine = "fallback"
                error = f"{error}; used fallback"
            except Exception as exc2:
                pdf_bytes = None
                error = f"{error}; fallback failed: {exc2}"

        ok = bool(pdf_bytes and pdf_bytes.startswith(b"%PDF"))
        if use_dry:
            print(
                f"DRY-RUN QuotePDFManager.generate_quote_pdf "
                f"branch={branch_key!r} engine={engine} "
                f"filename={filename!r} ok={ok} bytes="
                f"{len(pdf_bytes) if pdf_bytes else 0}"
            )

        return {
            "ok": ok,
            "pdf_bytes": pdf_bytes,
            "filename": filename,
            "branch": branch_key,
            "engine": engine,
            "branding": branding,
            "error": error,
            "dry_run": use_dry,
        }

    def attach_quote_to_lead(
        self,
        lead_id: int,
        pdf_bytes: bytes,
        filename: str,
        *,
        message: str | None = None,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        """Attach PDF to ``crm.lead`` and post a chatter note.

        Returns::

            {
              "ok": bool,
              "lead_id": int,
              "attachment_id": int | None,
              "message_id": int | None,
              "filename": str,
              "dry_run": bool,
              "error": str | None,
            }
        """
        use_dry = self._client._use_dry_run(dry_run if dry_run is not None else self.dry_run)
        name = (filename or "cotizacion.pdf").strip() or "cotizacion.pdf"
        if not name.lower().endswith(".pdf"):
            name = f"{name}.pdf"

        if not pdf_bytes:
            return {
                "ok": False,
                "lead_id": int(lead_id),
                "attachment_id": None,
                "message_id": None,
                "filename": name,
                "dry_run": use_dry,
                "error": "pdf_bytes is empty",
            }

        if use_dry:
            print(
                f"DRY-RUN QuotePDFManager.attach_quote_to_lead "
                f"lead={lead_id} filename={name!r} bytes={len(pdf_bytes)}"
            )
            return {
                "ok": True,
                "lead_id": int(lead_id),
                "attachment_id": -1,
                "message_id": -1,
                "filename": name,
                "dry_run": True,
                "error": None,
            }

        attachment_id: int | None = None
        message_id: int | None = None
        err: str | None = None
        try:
            attachment_id = self._client.attach_file(
                model="crm.lead",
                res_id=int(lead_id),
                filename=name,
                content=pdf_bytes,
                mimetype="application/pdf",
                dry_run=False,
            )
        except Exception as exc:
            err = f"attachment failed: {exc}"
            print(f"WARN attach_quote_to_lead lead={lead_id}: {err}")
            return {
                "ok": False,
                "lead_id": int(lead_id),
                "attachment_id": None,
                "message_id": None,
                "filename": name,
                "dry_run": False,
                "error": err,
            }

        body = (message or f"Cotización adjunta: {name}").strip()
        try:
            message_id = self._post_chatter_with_attachment(
                int(lead_id),
                body=body,
                attachment_id=int(attachment_id) if attachment_id is not None else None,
            )
        except Exception as exc:
            # Attachment already ok — soft-fail chatter
            err = f"chatter failed: {exc}"
            print(f"WARN attach_quote_to_lead chatter lead={lead_id}: {err}")

        return {
            "ok": attachment_id is not None,
            "lead_id": int(lead_id),
            "attachment_id": attachment_id,
            "message_id": message_id,
            "filename": name,
            "dry_run": False,
            "error": err,
        }

    def render_and_attach(
        self,
        lead_id: int,
        vehicle: dict[str, Any],
        quote: dict[str, Any],
        client: dict[str, Any] | None = None,
        *,
        branch: str | None = None,
        physical_location: str | None = None,
    ) -> dict[str, Any]:
        """Generate quote PDF and attach it to the lead in one step."""
        generated = self.generate_quote_pdf(
            vehicle,
            quote,
            client,
            branch=branch,
            physical_location=physical_location,
        )
        if not generated.get("ok") or not generated.get("pdf_bytes"):
            return {
                "ok": False,
                "lead_id": int(lead_id),
                "generate": generated,
                "attach": None,
                "error": generated.get("error") or "PDF generation failed",
            }
        branch_label = (generated.get("branding") or {}).get(
            "branch_label", generated.get("branch")
        )
        attach = self.attach_quote_to_lead(
            int(lead_id),
            generated["pdf_bytes"],
            generated["filename"],
            message=(
                f"Cotización generada ({branch_label}) — "
                f"{generated['filename']}"
            ),
        )
        return {
            "ok": bool(attach.get("ok")),
            "lead_id": int(lead_id),
            "generate": generated,
            "attach": attach,
            "branch": generated.get("branch"),
            "error": attach.get("error"),
        }

    def _post_chatter_with_attachment(
        self,
        lead_id: int,
        *,
        body: str,
        attachment_id: int | None,
    ) -> int:
        """Post mail.message; link ir.attachment when field accepted."""
        safe = (
            body.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br/>")
        )
        vals: dict[str, Any] = {
            "model": "crm.lead",
            "res_id": int(lead_id),
            "body": f"<p>{safe}</p>",
            "message_type": "comment",
        }
        if attachment_id is not None and attachment_id > 0:
            vals["attachment_ids"] = [(6, 0, [int(attachment_id)])]

        try:
            return int(self._client.execute_kw("mail.message", "create", [vals]))
        except Exception:
            vals.pop("attachment_ids", None)
            try:
                return int(
                    self._client.execute_kw("mail.message", "create", [vals])
                )
            except Exception:
                # Use existing helper as last resort
                return int(self._client.post_quote_to_chatter(int(lead_id), body))


__all__ = [
    "BRANCH_BRANDING",
    "QuotePDFManager",
    "branding_for_branch",
    "reportlab_available",
    "resolve_quote_branch",
]
