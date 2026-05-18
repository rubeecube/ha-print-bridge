"""Tests for PDF page-order helpers."""

from __future__ import annotations

import io

from pypdf import PdfReader, PdfWriter

from page_order import reverse_pdf_pages


def _pdf_with_widths(widths: list[int]) -> bytes:
    writer = PdfWriter()
    for width in widths:
        writer.add_blank_page(width=width, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _widths(pdf_data: bytes) -> list[int]:
    reader = PdfReader(io.BytesIO(pdf_data))
    return [int(page.mediabox.width) for page in reader.pages]


def test_reverse_pdf_pages_reverses_page_order() -> None:
    result = reverse_pdf_pages(_pdf_with_widths([100, 200, 300]))

    assert _widths(result) == [300, 200, 100]
