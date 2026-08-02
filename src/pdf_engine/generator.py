"""PDF vehicle spec sheet / quote breakdown generator (ReportLab)."""
from __future__ import annotations

import io
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


class PdfEngineError(RuntimeError):
    """Raised when PDF generation fails."""


def _money(value: Any) -> str:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return str(value or "—")
    sign = "-" if amount < 0 else ""
    whole, _, frac = f"{abs(amount):.2f}".partition(".")
    grouped = ",".join(
        reversed([whole[max(0, i - 3) : i] for i in range(len(whole), 0, -3)])
    )
    return f"{sign}${grouped}.{frac}"


def _text(value: Any, default: str = "—") -> str:
    if value is None or value == "":
        return default
    return str(value).strip() or default


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "brand": ParagraphStyle(
            "Brand",
            parent=base["Heading1"],
            fontSize=18,
            textColor=colors.HexColor("#0B3D2E"),
            spaceAfter=2,
            alignment=TA_LEFT,
        ),
        "sub": ParagraphStyle(
            "Sub",
            parent=base["Normal"],
            fontSize=9,
            textColor=colors.HexColor("#44555A"),
            alignment=TA_LEFT,
        ),
        "h2": ParagraphStyle(
            "H2",
            parent=base["Heading2"],
            fontSize=12,
            textColor=colors.HexColor("#0B3D2E"),
            spaceBefore=12,
            spaceAfter=6,
        ),
        "body": ParagraphStyle(
            "Body",
            parent=base["Normal"],
            fontSize=10,
            leading=14,
        ),
        "footer": ParagraphStyle(
            "Footer",
            parent=base["Normal"],
            fontSize=8,
            textColor=colors.HexColor("#555555"),
            alignment=TA_CENTER,
        ),
        "right": ParagraphStyle(
            "Right",
            parent=base["Normal"],
            fontSize=9,
            alignment=TA_RIGHT,
            textColor=colors.HexColor("#44555A"),
        ),
    }


def _header_table(styles: dict[str, ParagraphStyle], contact: dict[str, Any]) -> Table:
    brand = Paragraph("<b>Autosell MX</b>", styles["brand"])
    tagline = Paragraph("Cotización / ficha técnica de vehículo", styles["sub"])
    left = [brand, tagline]
    right_lines = [
        _text(contact.get("phone"), "Tel. (614) —"),
        _text(contact.get("email"), "contacto@autosell.mx"),
        _text(contact.get("web"), "https://www.autosell.mx"),
        _text(contact.get("city"), "Chihuahua, MX"),
    ]
    right = Paragraph("<br/>".join(right_lines), styles["right"])
    table = Table([[left, right]], colWidths=[4.2 * inch, 2.8 * inch])
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, -1), 1.2, colors.HexColor("#0B3D2E")),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    return table


def _kv_table(rows: list[tuple[str, str]]) -> Table:
    data = [[Paragraph(f"<b>{k}</b>", getSampleStyleSheet()["Normal"]), v] for k, v in rows]
    table = Table(data, colWidths=[2.2 * inch, 4.8 * inch])
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#0B3D2E")),
            ]
        )
    )
    return table


def _finance_table(quote_data: dict[str, Any]) -> Table:
    rows = [
        ["Concepto", "Monto"],
        ["Precio del vehículo", _money(quote_data.get("vehicle_price"))],
        ["Enganche (total)", _money(quote_data.get("down_payment"))],
        ["Enganche efectivo", _money(quote_data.get("cash_down_payment"))],
        ["Trade-in (equity)", _money(quote_data.get("net_trade_in_equity") or 0)],
        ["Monto a financiar", _money(quote_data.get("financed_principal"))],
        ["Comisión por apertura", _money(quote_data.get("origination_fee") or 0)],
        [
            f"Plazo",
            f"{_text(quote_data.get('term_months'), '—')} meses",
        ],
        [
            "Mensualidad estimada",
            _money(quote_data.get("estimated_monthly_payment")),
        ],
    ]
    if quote_data.get("monthly_admin_fee") not in (None, "", 0, "0"):
        rows.insert(
            -1,
            ["Cuota admin. mensual", _money(quote_data.get("monthly_admin_fee"))],
        )
    table = Table(rows, colWidths=[4.5 * inch, 2.5 * inch])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0B3D2E")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CCD5D3")),
                ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#E8F2EE")),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    return table


def build_quote_pdf_bytes(
    quote_data: dict[str, Any],
    vehicle_data: dict[str, Any],
    *,
    contact: dict[str, Any] | None = None,
    valid_days: int = 7,
) -> bytes:
    """Render a one-page Autosell quote / spec sheet PDF."""
    if not isinstance(quote_data, dict) or not isinstance(vehicle_data, dict):
        raise PdfEngineError("quote_data and vehicle_data must be dicts")

    styles = _styles()
    contact = contact or {}
    buffer = io.BytesIO()
    # Uncompressed streams keep quote fields searchable / testable.
    from reportlab import rl_config

    prev_compression = rl_config.pageCompression
    rl_config.pageCompression = 0
    try:
        doc = SimpleDocTemplate(
            buffer,
            pagesize=letter,
            leftMargin=0.75 * inch,
            rightMargin=0.75 * inch,
            topMargin=0.6 * inch,
            bottomMargin=0.6 * inch,
            title="Autosell MX — Cotización",
            author="Autosell MX",
        )

        vehicle_name = _text(
            vehicle_data.get("name")
            or vehicle_data.get("vehicle_name")
            or vehicle_data.get("title"),
            "Vehículo",
        )
        features = vehicle_data.get("features") or vehicle_data.get("key_features") or []
        if isinstance(features, str):
            feature_text = features
        elif isinstance(features, (list, tuple)):
            feature_text = ", ".join(str(f) for f in features if f) or "—"
        else:
            feature_text = "—"

        issued = date.today()
        expires = issued + timedelta(days=max(1, int(valid_days)))

        story: list[Any] = [
            _header_table(styles, contact),
            Spacer(1, 0.25 * inch),
            Paragraph("Resumen del vehículo", styles["h2"]),
            _kv_table(
                [
                    ("Vehículo", vehicle_name),
                    ("Año", _text(vehicle_data.get("year"))),
                    ("Marca", _text(vehicle_data.get("make") or vehicle_data.get("brand"))),
                    ("Modelo", _text(vehicle_data.get("model"))),
                    ("VIN", _text(vehicle_data.get("vin"))),
                    (
                        "Kilometraje",
                        _text(
                            vehicle_data.get("mileage")
                            or vehicle_data.get("mileage_km")
                        ),
                    ),
                    (
                        "Transmisión",
                        _text(vehicle_data.get("transmission")),
                    ),
                    (
                        "SKU / ID",
                        _text(
                            vehicle_data.get("sku") or vehicle_data.get("autosell_id")
                        ),
                    ),
                    ("Características", feature_text),
                ]
            ),
            Paragraph("Desglose financiero", styles["h2"]),
            _finance_table(quote_data),
            Spacer(1, 0.2 * inch),
            Paragraph("Próximos pasos", styles["h2"]),
            Paragraph(
                "1. Confirma disponibilidad del vehículo con un asesor Autosell.<br/>"
                "2. Prepara identificación oficial e ingresos para precalificación.<br/>"
                "3. Agenda inspección / prueba de manejo en sucursal Chihuahua.",
                styles["body"],
            ),
            Spacer(1, 0.25 * inch),
            Paragraph(
                f"Cotización emitida: {issued.isoformat()} · Vigencia hasta: "
                f"{expires.isoformat()} · Informativa, sujeta a aprobación crediticia "
                f"y disponibilidad de inventario.",
                styles["footer"],
            ),
            Paragraph(
                "Autosell MX · Chihuahua · autosell.mx",
                styles["footer"],
            ),
        ]

        try:
            doc.build(story)
        except Exception as exc:
            raise PdfEngineError(f"PDF build failed: {exc}") from exc
    finally:
        rl_config.pageCompression = prev_compression
    return buffer.getvalue()


def generate_vehicle_quote_pdf(
    quote_data: dict[str, Any],
    vehicle_data: dict[str, Any],
    *,
    output_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    lead_id: int | None = None,
    contact: dict[str, Any] | None = None,
    valid_days: int = 7,
    attach_to_odoo: bool = False,
    odoo_client: Any | None = None,
    odoo_model: str = "crm.lead",
    odoo_res_id: int | None = None,
    result_meta: dict[str, Any] | None = None,
) -> bytes | Path:
    """Generate quote PDF; optionally save to disk and/or attach in Odoo.

    Returns ``Path`` when written to disk, otherwise raw ``bytes``.
    When ``result_meta`` is provided, fills ``path``, ``bytes_len``, ``attachment_id``.
    """
    pdf_bytes = build_quote_pdf_bytes(
        quote_data,
        vehicle_data,
        contact=contact,
        valid_days=valid_days,
    )
    if not pdf_bytes:
        raise PdfEngineError("PDF generation returned empty content")

    sku = _text(
        vehicle_data.get("sku")
        or vehicle_data.get("autosell_id")
        or vehicle_data.get("name")
        or "vehicle",
        "vehicle",
    )
    path: Path | None = None
    if output_path is not None:
        path = Path(output_path)
    elif output_dir is not None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_sku = "".join(c if c.isalnum() or c in "-_" else "_" for c in sku)[:40]
        path = Path(output_dir) / f"quote_{safe_sku}_{stamp}.pdf"

    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(pdf_bytes)
        print(
            f"Generated PDF spec sheet for vehicle {sku} / "
            f"lead {lead_id if lead_id is not None else 'n/a'} at {path}"
        )
    else:
        print(
            f"Generated PDF spec sheet for vehicle {sku} / "
            f"lead {lead_id if lead_id is not None else 'n/a'} "
            f"({len(pdf_bytes)} bytes)"
        )

    attachment_id: int | None = None
    res_id = odoo_res_id if odoo_res_id is not None else lead_id
    if attach_to_odoo and odoo_client is not None and res_id is not None:
        filename = path.name if path is not None else f"quote_{sku}.pdf"
        attachment_id = odoo_client.attach_file(
            model=odoo_model,
            res_id=int(res_id),
            filename=filename,
            content=pdf_bytes,
            mimetype="application/pdf",
        )
        print(
            f"Attached PDF ir.attachment id={attachment_id} "
            f"to {odoo_model}({res_id})"
        )

    if result_meta is not None:
        result_meta["path"] = str(path) if path is not None else None
        result_meta["bytes_len"] = len(pdf_bytes)
        result_meta["attachment_id"] = attachment_id
        result_meta["sku"] = sku

    return path if path is not None else pdf_bytes
