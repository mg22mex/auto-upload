"""PDF vehicle spec sheet / quote breakdown generator (ReportLab)."""
from __future__ import annotations

import io
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

try:
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

    _HAS_REPORTLAB = True
except ImportError:  # pragma: no cover - optional in minimal test envs
    colors = None  # type: ignore[assignment]
    TA_CENTER = TA_LEFT = TA_RIGHT = None  # type: ignore[assignment]
    letter = None  # type: ignore[assignment]
    ParagraphStyle = getSampleStyleSheet = None  # type: ignore[assignment]
    inch = None  # type: ignore[assignment]
    Paragraph = SimpleDocTemplate = Spacer = Table = TableStyle = None  # type: ignore[assignment]
    _HAS_REPORTLAB = False


class PdfEngineError(RuntimeError):
    """Raised when PDF generation fails."""


FINANCING_ESTIMATE_DISCLAIMER = (
    "IMPORTANTE: Esta cotización es únicamente una estimación informativa. "
    "Los montos reales, tasa de interés, comisiones y mensualidad final serán "
    "calculados y confirmados en el momento de la solicitud formal, sujetos a "
    "aprobación crediticia y políticas vigentes de la institución financiera."
)

_CHAT_LEAD_IN_RE = re.compile(
    r"^(?:"
    r"(?:hola|buen[oa]s?(?:\s+(?:d[ií]as|tardes|noches))?)[,!]?\s+|"
    r"(?:quiero|quisiera|me\s+gustar[ií]a|busco|necesito|deseo)\s+"
    r"(?:cotizar|informaci[oó]n(?:\s+(?:de|sobre))?|info(?:\s+(?:de|sobre))?|"
    r"saber(?:\s+(?:de|sobre))?|una\s+cotizaci[oó]n\s+(?:de|para))?\s*"
    r"(?:una?|el|la|unos?)?\s*"
    r")+",
    re.IGNORECASE,
)
_CHAT_TRAIL_RE = re.compile(
    r"\s*(?:,?\s*)?(?:por\s+favor|gracias|pls|please)\s*[.!]?\s*$",
    re.IGNORECASE,
)


def _require_reportlab() -> None:
    if not _HAS_REPORTLAB:
        raise PdfEngineError(
            "reportlab is not installed; run: pip install reportlab"
        )


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


def sanitize_vehicle_title(
    raw: str | None,
    *,
    make: str | None = None,
    model: str | None = None,
    year: int | str | None = None,
    default: str = "Vehículo",
) -> str:
    """Prefer structured inventory name; strip chat greetings from free text.

    Examples:
      ``Hola, quiero cotizar una Ford Ranger XLT 2021`` → ``Ford Ranger XLT 2021``
      make/model/year kwargs always win when provided.
    """
    make_s = _text(make, "").strip()
    model_s = _text(model, "").strip()
    year_s = _text(year, "").strip()
    if make_s and model_s:
        parts = [make_s, model_s]
        if year_s and year_s != "—":
            parts.append(year_s)
        return " ".join(parts)

    text = (raw or "").strip()
    if not text:
        return default

    cleaned = _CHAT_LEAD_IN_RE.sub("", text).strip(" ,.-:")
    cleaned = _CHAT_TRAIL_RE.sub("", cleaned).strip(" ,.-:")
    # Drop trailing chat clauses after the vehicle name.
    cleaned = re.split(
        r"[.!?]|\s+con\s+enganche|\s+a\s+\d|\s+por\s+favor",
        cleaned,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip(" ,.-:")
    # Collapse whitespace; cap length for the PDF label column.
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > 80:
        cleaned = cleaned[:77].rsplit(" ", 1)[0].rstrip(",.-") + "…"
    # Still looks like a full chat sentence → too noisy.
    if len(cleaned) > 70 and cleaned.lower().startswith(("hola", "buenas", "quiero")):
        return default
    return cleaned or default


def resolve_vehicle_title(vehicle_data: dict[str, Any] | None) -> str:
    """Pick the cleanest vehicle label from a vehicle_data dict."""
    data = vehicle_data or {}
    return sanitize_vehicle_title(
        _text(
            data.get("name")
            or data.get("vehicle_name")
            or data.get("title")
            or data.get("display_name"),
            "",
        ),
        make=data.get("make") or data.get("brand"),
        model=data.get("model"),
        year=data.get("year"),
    )


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
        "disclaimer": ParagraphStyle(
            "Disclaimer",
            parent=base["Normal"],
            fontSize=8.5,
            leading=11,
            textColor=colors.HexColor("#6B1D1D"),
            backColor=colors.HexColor("#F8EFEF"),
            borderPadding=6,
            spaceBefore=8,
            spaceAfter=8,
            alignment=TA_LEFT,
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
    brand_name = _text(contact.get("brand") or contact.get("company"), "Autosell MX")
    branch_name = _text(contact.get("branch_label") or contact.get("branch"), "")
    brand = Paragraph(f"<b>{brand_name}</b>", styles["brand"])
    if branch_name and branch_name != "—":
        tagline = Paragraph(
            f"Cotización · Sucursal <b>{branch_name}</b>",
            styles["sub"],
        )
    else:
        tagline = Paragraph("Cotización / ficha técnica de vehículo", styles["sub"])
    left = [brand, tagline]
    right_lines = [
        _text(contact.get("phone"), "Tel. (614) —"),
        _text(contact.get("email"), "contacto@autosell.mx"),
        _text(contact.get("web"), "https://www.autosell.mx"),
        _text(contact.get("city") or contact.get("address"), "Chihuahua, MX"),
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
    _require_reportlab()
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

        vehicle_name = resolve_vehicle_title(vehicle_data)
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
                f"Autosell MX · {_text(contact.get('branch_label') or contact.get('city'), 'Chihuahua')} "
                f"· autosell.mx",
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


def quote_result_to_dict(quote: Any) -> dict[str, Any]:
    """Flatten a ``QuoteResult`` (or quote-like object) into PDF-ready dicts."""
    schedule_rows: list[dict[str, Any]] = []
    for row in getattr(quote, "schedule", ()) or ():
        schedule_rows.append(
            {
                "period": getattr(row, "month", None) or getattr(row, "period", None),
                "beginning_balance": getattr(row, "beginning_balance", None),
                "interest": getattr(row, "interest", None),
                "iva": getattr(row, "iva", None),
                "principal": getattr(row, "principal", None),
                "base_payment": getattr(row, "base_payment", None),
                "auto_insurance": getattr(row, "auto_insurance", None),
                "life_insurance": getattr(row, "life_insurance", None),
                "admin_fee": getattr(row, "admin_fee", None),
                "total_payment": getattr(row, "total_payment", None),
                "ending_balance": getattr(row, "ending_balance", None),
                "is_opening": bool(getattr(row, "is_opening", False)),
            }
        )
    return {
        "vehicle_price": getattr(quote, "vehicle_price", None),
        "term_months": getattr(quote, "term_months", None),
        "annual_rate": getattr(quote, "annual_rate", None),
        "down_payment": getattr(quote, "down_payment", None),
        "cash_down_payment": getattr(quote, "cash_down_payment", None),
        "net_trade_in_equity": getattr(quote, "net_trade_in_equity", None),
        "amount_to_finance": getattr(quote, "amount_to_finance", None),
        "origination_fee": getattr(quote, "origination_fee", None),
        "financed_principal": getattr(quote, "financed_principal", None),
        "base_monthly_payment": getattr(quote, "base_monthly_payment", None),
        "monthly_auto_insurance": getattr(quote, "monthly_auto_insurance", None),
        "monthly_life_insurance": getattr(quote, "monthly_life_insurance", None),
        "average_monthly_iva": getattr(quote, "average_monthly_iva", None),
        "estimated_monthly_payment": getattr(quote, "estimated_monthly_payment", None),
        "monthly_admin_fee": getattr(quote, "monthly_admin_fee", None),
        "profile_name": getattr(quote, "profile_name", "Scotiabank CrediAuto"),
        "schedule": schedule_rows,
        "term_cap_note": getattr(quote, "term_cap_note", None),
        "requested_term_months": getattr(quote, "requested_term_months", None),
        "vehicle_year": getattr(quote, "vehicle_year", None),
    }


def _amortization_table(schedule: list[dict[str, Any]]) -> Table:
    """Compact amortization grid (period / interest / principal / total / balance)."""
    header = ["#", "Interés", "IVA", "Capital", "Pago", "Saldo"]
    data: list[list[Any]] = [header]
    for row in schedule:
        period = row.get("period")
        label = "Apertura" if row.get("is_opening") else str(period if period is not None else "")
        data.append(
            [
                label,
                _money(row.get("interest")),
                _money(row.get("iva")),
                _money(row.get("principal")),
                _money(row.get("total_payment")),
                _money(row.get("ending_balance")),
            ]
        )
    col_w = [0.85 * inch, 1.15 * inch, 1.0 * inch, 1.15 * inch, 1.15 * inch, 1.2 * inch]
    table = Table(data, colWidths=col_w, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0B3D2E")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 7.5),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("ALIGN", (0, 0), (0, -1), "CENTER"),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#CCD5D3")),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                (
                    "ROWBACKGROUNDS",
                    (0, 1),
                    (-1, -1),
                    [colors.white, colors.HexColor("#F4F8F6")],
                ),
            ]
        )
    )
    return table


def build_financing_quote_pdf_bytes(
    quote_data: dict[str, Any],
    *,
    vehicle_data: dict[str, Any] | None = None,
    contact: dict[str, Any] | None = None,
    customer_name: str | None = None,
    valid_days: int = 7,
) -> bytes:
    """Scotiabank CrediAuto amortization schedule PDF (``financing_quote.pdf``)."""
    _require_reportlab()
    if not isinstance(quote_data, dict):
        raise PdfEngineError("quote_data must be a dict")

    styles = _styles()
    contact = contact or {}
    vehicle_data = vehicle_data or {}
    buffer = io.BytesIO()
    from reportlab import rl_config

    prev_compression = rl_config.pageCompression
    rl_config.pageCompression = 0
    try:
        doc = SimpleDocTemplate(
            buffer,
            pagesize=letter,
            leftMargin=0.55 * inch,
            rightMargin=0.55 * inch,
            topMargin=0.5 * inch,
            bottomMargin=0.5 * inch,
            title="Autosell MX — Tabla de Amortización CrediAuto",
            author="Autosell MX",
        )
        issued = date.today()
        expires = issued + timedelta(days=max(1, int(valid_days)))
        vehicle_name = resolve_vehicle_title(vehicle_data)
        profile = _text(quote_data.get("profile_name"), "Scotiabank CrediAuto")
        client = _text(customer_name, "")

        story: list[Any] = [
            _header_table(
                styles,
                {
                    **contact,
                    "brand": contact.get("brand") or "Autosell MX",
                    "branch_label": contact.get("branch_label")
                    or contact.get("branch")
                    or "CrediAuto",
                },
            ),
            Spacer(1, 0.15 * inch),
            Paragraph(f"Tabla de amortización — {profile}", styles["h2"]),
        ]
        summary_rows: list[tuple[str, str]] = []
        if client and client != "—":
            summary_rows.append(("Cliente", client))
        summary_rows.extend(
            [
                ("Vehículo", vehicle_name),
                ("Precio", _money(quote_data.get("vehicle_price"))),
                ("Enganche total", _money(quote_data.get("down_payment"))),
                ("Monto financiado", _money(quote_data.get("financed_principal"))),
                ("Plazo", f"{_text(quote_data.get('term_months'), '—')} meses"),
                (
                    "Mensualidad estimada",
                    _money(quote_data.get("estimated_monthly_payment")),
                ),
            ]
        )
        story.append(_kv_table(summary_rows))
        story.append(Spacer(1, 0.1 * inch))
        term_note = _text(quote_data.get("term_cap_note"), "")
        if term_note and term_note != "—":
            story.append(
                Paragraph(f"<b>{term_note}</b>", styles["disclaimer"])
            )
            story.append(Spacer(1, 0.08 * inch))
        story.append(
            Paragraph(
                f"<b>{FINANCING_ESTIMATE_DISCLAIMER}</b>",
                styles["disclaimer"],
            )
        )
        story.append(Spacer(1, 0.08 * inch))
        story.append(Paragraph("Desglose financiero", styles["h2"]))
        story.append(_finance_table(quote_data))

        schedule = quote_data.get("schedule") or []
        if isinstance(schedule, (list, tuple)) and schedule:
            story.append(Spacer(1, 0.15 * inch))
            story.append(Paragraph("Calendario de pagos", styles["h2"]))
            story.append(_amortization_table(list(schedule)))

        story.append(Spacer(1, 0.2 * inch))
        story.append(
            Paragraph(
                f"<b>{FINANCING_ESTIMATE_DISCLAIMER}</b>",
                styles["disclaimer"],
            )
        )
        story.append(Spacer(1, 0.08 * inch))
        story.append(
            Paragraph(
                f"Cotización emitida: {issued.isoformat()} · Vigencia hasta: "
                f"{expires.isoformat()} · Informativa, sujeta a aprobación crediticia "
                f"Scotiabank CrediAuto y disponibilidad de inventario.",
                styles["footer"],
            )
        )
        story.append(
            Paragraph(
                f"Autosell MX · {_text(contact.get('branch_label') or contact.get('city'), 'Chihuahua')} "
                f"· autosell.mx",
                styles["footer"],
            )
        )
        try:
            doc.build(story)
        except Exception as exc:
            raise PdfEngineError(f"financing PDF build failed: {exc}") from exc
    finally:
        rl_config.pageCompression = prev_compression
    return buffer.getvalue()


def generate_financing_quote_pdf(
    quote: Any,
    *,
    output_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    vehicle_data: dict[str, Any] | None = None,
    contact: dict[str, Any] | None = None,
    customer_name: str | None = None,
    valid_days: int = 7,
    filename: str = "financing_quote.pdf",
    result_meta: dict[str, Any] | None = None,
) -> Path:
    """Write Scotiabank amortization PDF; default name ``financing_quote.pdf``."""
    if isinstance(quote, dict):
        quote_data = dict(quote)
    else:
        quote_data = quote_result_to_dict(quote)

    pdf_bytes = build_financing_quote_pdf_bytes(
        quote_data,
        vehicle_data=vehicle_data,
        contact=contact,
        customer_name=customer_name,
        valid_days=valid_days,
    )
    if not pdf_bytes:
        raise PdfEngineError("financing PDF generation returned empty content")

    if output_path is not None:
        path = Path(output_path)
    elif output_dir is not None:
        path = Path(output_dir) / filename
    else:
        import tempfile

        path = Path(tempfile.mkdtemp(prefix="autosell_financing_")) / filename

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pdf_bytes)
    print(f"Generated financing amortization PDF at {path} ({len(pdf_bytes)} bytes)")
    if result_meta is not None:
        result_meta["path"] = str(path)
        result_meta["bytes_len"] = len(pdf_bytes)
        result_meta["filename"] = path.name
    return path
