"""Tests for PDF-to-PWG Raster conversion helpers."""

import io

from pypdf import PdfWriter

from raster_converter import convert_pdf_to_pwg_raster


def _make_landscape_a4_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=842, height=595)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _c_string(data: bytes, offset: int, length: int = 64) -> str:
    return data[offset: offset + length].split(b"\0", 1)[0].decode("ascii")


def test_landscape_a4_pwg_header_uses_a4_media_name():
    raster = convert_pdf_to_pwg_raster(
        _make_landscape_a4_pdf(),
        "two-sided-short-edge",
        dpi=72,
    )

    assert _c_string(raster, 4 + 1732) == "iso_a4_210x297mm"
