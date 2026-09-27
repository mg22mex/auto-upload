"""PDF engine — vehicle quote / CrediAuto amortization PDFs."""

from src.pdf_engine.generator import (
    PdfEngineError,
    build_financing_quote_pdf_bytes,
    build_quote_pdf_bytes,
    generate_financing_quote_pdf,
    generate_vehicle_quote_pdf,
    quote_result_to_dict,
)

__all__ = [
    "PdfEngineError",
    "build_financing_quote_pdf_bytes",
    "build_quote_pdf_bytes",
    "generate_financing_quote_pdf",
    "generate_vehicle_quote_pdf",
    "quote_result_to_dict",
]
