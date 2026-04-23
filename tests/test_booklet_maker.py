"""Tests for custom_components/auto_print/booklet_maker.py.

Covers:
  - Correct page count after booklet conversion (no double-adding of blank pages).
  - Padding to next multiple of 4.
  - Correct booklet page order for 4-page and 8-page inputs.
  - Round-trip: booklet of a 4-page PDF is itself 4 pages.
"""

import io

import pytest
from pypdf import PdfReader

from conftest import make_pdf, pdf_page_count
from booklet_maker import create_booklet


# ── page count tests ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "input_pages, expected_pages",
    [
        (4, 4),   # already multiple of 4 — no padding
        (8, 8),
        (1, 4),   # padded from 1 → 4
        (2, 4),   # padded from 2 → 4
        (3, 4),   # padded from 3 → 4
        (5, 8),   # padded from 5 → 8
        (6, 8),
        (7, 8),
        (9, 12),  # padded from 9 → 12
    ],
)
def test_output_page_count_is_padded_to_multiple_of_4(input_pages, expected_pages):
    result = create_booklet(make_pdf(input_pages))
    assert pdf_page_count(result) == expected_pages


# ── regression: blank pages must NOT be double-added ─────────────────────────


def test_blank_padding_pages_not_doubled():
    """Regression: the original code called writer.add_blank_page() which both
    added the page to the writer immediately and returned it for the pages list,
    so the same blank pages were later added a second time via add_page().
    Verify that a 6-page input produces exactly 8 pages, not more.
    """
    result = create_booklet(make_pdf(6))
    assert pdf_page_count(result) == 8, (
        "Blank pages were double-added — got more than 8 pages for a 6-page input"
    )


def test_no_extra_pages_for_already_aligned_input():
    """A 4-page PDF requires no padding; output must be exactly 4 pages."""
    result = create_booklet(make_pdf(4))
    assert pdf_page_count(result) == 4


# ── booklet page order tests ──────────────────────────────────────────────────

def _page_labels(data: bytes) -> list[int]:
    """Return 0-based page indices in the order they appear in *data*.

    We encode the original page number as a unique page size: page N has
    width = 100 + N points.  This lets us read back the order without needing
    page labels in the PDF.
    """
    reader = PdfReader(io.BytesIO(data))
    return [int(float(p.mediabox.width)) - 100 for p in reader.pages]


def make_indexed_pdf(num_pages: int) -> bytes:
    """Create a PDF where page N has width = (100 + N) points."""
    from pypdf import PdfWriter
    writer = PdfWriter()
    for i in range(num_pages):
        writer.add_blank_page(width=100 + i, height=842)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_booklet_order_4_pages():
    """4-page booklet: sheet 1 front=[3,0], sheet 1 back=[1,2]."""
    result = create_booklet(make_indexed_pdf(4))
    assert _page_labels(result) == [3, 0, 1, 2]


def test_booklet_order_8_pages():
    """8-page booklet: [7,0,1,6,5,2,3,4]."""
    result = create_booklet(make_indexed_pdf(8))
    assert _page_labels(result) == [7, 0, 1, 6, 5, 2, 3, 4]


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
