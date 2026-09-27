"""PDF engine — vehicle quote / CrediAuto amortization PDFs."""

from src.pdf_engine.generator import (
    FINANCING_ESTIMATE_DISCLAIMER,
    PdfEngineError,
    build_financing_quote_pdf_bytes,
    build_quote_pdf_bytes,
    generate_financing_quote_pdf,
    generate_vehicle_quote_pdf,
    quote_result_to_dict,
    resolve_vehicle_title,
    sanitize_vehicle_title,
)

__all__ = [
    "FINANCING_ESTIMATE_DISCLAIMER",
    "PdfEngineError",
    "build_financing_quote_pdf_bytes",
    "build_quote_pdf_bytes",
    "generate_financing_quote_pdf",
    "generate_vehicle_quote_pdf",
    "quote_result_to_dict",
    "resolve_vehicle_title",
    "sanitize_vehicle_title",
]
