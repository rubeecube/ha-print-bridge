"""PDF page-order helpers for Print Bridge."""

from __future__ import annotations

import io

from pypdf import PdfReader, PdfWriter


def reverse_pdf_pages(pdf_data: bytes) -> bytes:
    """Return *pdf_data* with pages in reverse order."""
    reader = PdfReader(io.BytesIO(pdf_data))
    if not reader.pages:
        raise ValueError("PDF has no pages")

    writer = PdfWriter()
    for page in reversed(reader.pages):
        writer.add_page(page)

    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
