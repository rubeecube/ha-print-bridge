"""Tests for custom_components/print_bridge/booklet_maker.py.

Covers:
  - Correct physical sheet-side count after booklet imposition.
  - Padding to next multiple of 4.
  - Correct 2-up booklet page order for 4-page and 8-page inputs.
"""

import io

import pypdfium2 as pdfium
import pytest
from pypdf import PdfReader
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from conftest import make_pdf, pdf_page_count
from booklet_maker import create_booklet


# ── page count tests ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "input_pages, expected_pages",
    [
        (4, 2),   # already multiple of 4 -> one duplex sheet
        (8, 4),
        (1, 2),   # padded from 1 -> 4 logical pages -> two sides
        (2, 2),
        (3, 2),
        (5, 4),   # padded from 5 -> 8 logical pages -> four sides
        (6, 4),
        (7, 4),
        (9, 6),   # padded from 9 -> 12 logical pages -> six sides
    ],
)
def test_output_page_count_is_padded_to_multiple_of_4(input_pages, expected_pages):
    result = create_booklet(make_pdf(input_pages))
    assert pdf_page_count(result) == expected_pages


# ── regression: blank pages must NOT be double-added ─────────────────────────


def test_blank_padding_pages_not_doubled():
    """Regression: padding blanks should only exist inside imposed sheet sides."""
    result = create_booklet(make_pdf(6))
    assert pdf_page_count(result) == 4, (
        "Blank pages were double-added; expected four physical sides for six pages"
    )


def test_no_extra_pages_for_already_aligned_input():
    """A 4-page PDF requires no padding; output must be one duplex sheet."""
    result = create_booklet(make_pdf(4))
    assert pdf_page_count(result) == 2


def test_imposed_sheet_is_landscape_two_up():
    result = create_booklet(make_pdf(4))
    first_sheet_side = PdfReader(io.BytesIO(result)).pages[0]

    assert float(first_sheet_side.mediabox.width) == pytest.approx(842)
    assert float(first_sheet_side.mediabox.height) == pytest.approx(595)


def test_imposed_sheet_stays_landscape_for_landscape_source_metadata():
    writer = PdfWriter()
    for _ in range(4):
        writer.add_blank_page(width=842, height=595)
    buf = io.BytesIO()
    writer.write(buf)

    result = create_booklet(buf.getvalue())
    first_sheet_side = PdfReader(io.BytesIO(result)).pages[0]

    assert float(first_sheet_side.mediabox.width) == pytest.approx(842)
    assert float(first_sheet_side.mediabox.height) == pytest.approx(595)


def test_imposed_sheet_applies_source_page_rotation_before_layout():
    writer = PdfWriter()
    for _ in range(4):
        page = writer.add_blank_page(width=842, height=595)
        page.rotate(90)
    buf = io.BytesIO()
    writer.write(buf)

    result = create_booklet(buf.getvalue())
    first_sheet_side = PdfReader(io.BytesIO(result)).pages[0]

    assert float(first_sheet_side.mediabox.width) == pytest.approx(842)
    assert float(first_sheet_side.mediabox.height) == pytest.approx(595)


# ── booklet page order tests ──────────────────────────────────────────────────

def _make_gray_indexed_pdf(num_pages: int) -> tuple[bytes, list[int]]:
    """Create a PDF where each page has a unique grayscale fill."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    grays = [30 + (i * 24) for i in range(num_pages)]
    for gray in grays:
        page = writer.add_blank_page(width=100, height=140)
        stream = DecodedStreamObject()
        stream.set_data(
            f"{gray / 255:.3f} g 0 0 100 140 re f\n".encode("ascii")
        )
        page[NameObject("/Contents")] = writer._add_object(stream)

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue(), grays


def _sample_spreads(data: bytes) -> list[tuple[int, int]]:
    """Return the detected grayscale value for the left and right page slots."""
    document = pdfium.PdfDocument(data)
    try:
        spreads = []
        for page_index in range(len(document)):
            page = document[page_index]
            try:
                bitmap = page.render(scale=1)
                image = bitmap.to_pil()
                width, height = image.size
                left = image.getpixel((width // 4, height // 2))[0]
                right = image.getpixel(((width * 3) // 4, height // 2))[0]
                spreads.append((left, right))
            finally:
                page.close()
        return spreads
    finally:
        document.close()


def test_booklet_order_4_pages():
    """4-page booklet: sheet 1 front=[3,0], sheet 1 back=[1,2]."""
    pdf_data, grays = _make_gray_indexed_pdf(4)
    result = create_booklet(pdf_data)

    assert _sample_spreads(result) == [
        (pytest.approx(grays[3], abs=2), pytest.approx(grays[0], abs=2)),
        (pytest.approx(grays[1], abs=2), pytest.approx(grays[2], abs=2)),
    ]


def test_booklet_order_8_pages():
    """8-page booklet: [7,0], [1,6], [5,2], [3,4]."""
    pdf_data, grays = _make_gray_indexed_pdf(8)
    result = create_booklet(pdf_data)

    assert _sample_spreads(result) == [
        (pytest.approx(grays[7], abs=2), pytest.approx(grays[0], abs=2)),
        (pytest.approx(grays[1], abs=2), pytest.approx(grays[6], abs=2)),
        (pytest.approx(grays[5], abs=2), pytest.approx(grays[2], abs=2)),
        (pytest.approx(grays[3], abs=2), pytest.approx(grays[4], abs=2)),
    ]


# ── error handling ────────────────────────────────────────────────────────────


def test_empty_pdf_raises():
    """An empty PDF (no pages) must raise ValueError, not silently succeed."""
    from pypdf import PdfWriter
    buf = io.BytesIO()
    PdfWriter().write(buf)
    with pytest.raises(ValueError, match="no pages"):
        create_booklet(buf.getvalue())


def test_invalid_bytes_raises():
    with pytest.raises(Exception):
        create_booklet(b"this is not a pdf")
