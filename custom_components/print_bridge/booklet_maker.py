"""Saddle-stitch booklet imposition for the Print Bridge integration.

All functions are pure (bytes in → bytes out) with no I/O or HA dependencies.
"""

from __future__ import annotations

import copy
import io
import logging

from pypdf import PageObject, PdfReader, PdfWriter, Transformation

logger = logging.getLogger(__name__)


def create_booklet(pdf_data: bytes) -> bytes:
    """Impose *pdf_data* for saddle-stitch booklet printing.

    The output PDF contains two logical source pages on each physical side.
    When printed duplex using two-sided short-edge and folded, the pages read
    in the correct order.

    The page count is padded to the next multiple of 4 with blank pages as
    needed.

    Args:
        pdf_data: Raw bytes of the source PDF.

    Returns:
        Raw bytes of the imposed booklet PDF.

    Raises:
        ValueError: If *pdf_data* cannot be parsed or has no pages.
    """
    reader = PdfReader(io.BytesIO(pdf_data))
    if not reader.pages:
        raise ValueError("PDF has no pages")

    pages = [_normalise_page_for_imposition(page) for page in reader.pages]
    page_width = float(pages[0].mediabox.width)
    page_height = float(pages[0].mediabox.height)

    # Pad to the next multiple of 4.
    while len(pages) % 4 != 0:
        blank = PageObject.create_blank_page(width=page_width, height=page_height)
        pages.append(blank)

    num_pages = len(pages)

    writer = PdfWriter()
    for sheet_index in range(num_pages // 4):
        left_front = num_pages - 1 - (sheet_index * 2)
        right_front = sheet_index * 2
        left_back = sheet_index * 2 + 1
        right_back = num_pages - 2 - (sheet_index * 2)
        writer.add_page(
            _impose_spread(pages[left_front], pages[right_front], page_width, page_height)
        )
        writer.add_page(
            _impose_spread(pages[left_back], pages[right_back], page_width, page_height)
        )

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _impose_spread(
    left_page: PageObject,
    right_page: PageObject,
    source_width: float,
    source_height: float,
) -> PageObject:
    """Return one landscape sheet side containing two scaled source pages."""
    sheet_width = max(source_width, source_height)
    sheet_height = min(source_width, source_height)
    slot_width = sheet_width / 2
    scale = min(slot_width / source_width, sheet_height / source_height)
    rendered_width = source_width * scale
    rendered_height = source_height * scale
    y_offset = (sheet_height - rendered_height) / 2

    sheet = PageObject.create_blank_page(width=sheet_width, height=sheet_height)
    _merge_page(
        sheet,
        left_page,
        Transformation()
        .scale(scale)
        .translate((slot_width - rendered_width) / 2, y_offset),
    )
    _merge_page(
        sheet,
        right_page,
        Transformation()
        .scale(scale)
        .translate(slot_width + (slot_width - rendered_width) / 2, y_offset),
    )
    return sheet


def _merge_page(
    sheet: PageObject,
    source_page: PageObject,
    transformation: Transformation,
) -> None:
    # pypdf page merge operations can mutate page boxes, so use a copy.
    sheet.merge_transformed_page(copy.copy(source_page), transformation)


def _normalise_page_for_imposition(page: PageObject) -> PageObject:
    """Return a copy with PDF rotation applied to content and page boxes."""
    normalised = copy.copy(page)
    rotation = int(normalised.get("/Rotate", 0) or 0) % 360
    if rotation:
        normalised.transfer_rotation_to_content()
    return normalised
