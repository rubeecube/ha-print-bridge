"""Saddle-stitch booklet imposition for the Print Bridge integration.

All functions are pure (bytes in → bytes out) with no I/O or HA dependencies.
"""

from __future__ import annotations

import io
import logging

from pypdf import PdfReader, PdfWriter

logger = logging.getLogger(__name__)


def create_booklet(pdf_data: bytes) -> bytes:
    """Reorder the pages of *pdf_data* for saddle-stitch (booklet) printing.

    Pages are rearranged so that when the sheets are printed duplex
    (two-sided short-edge) and folded, they read in the correct order.

    The page count is padded to the next multiple of 4 with blank pages as
    needed.  Blank pages are produced via a *temporary* PdfWriter instance
    that is discarded after padding, ensuring they are never pre-inserted into
    the output writer before the reordering loop runs.

    Args:
        pdf_data: Raw bytes of the source PDF.

    Returns:
        Raw bytes of the booklet-ordered PDF.

    Raises:
        ValueError: If *pdf_data* cannot be parsed or has no pages.
    """
    reader = PdfReader(io.BytesIO(pdf_data))
    if not reader.pages:
        raise ValueError("PDF has no pages")

    pages = list(reader.pages)
    page_width = pages[0].mediabox.width
    page_height = pages[0].mediabox.height

    # Pad to the next multiple of 4.
    # Each blank is created via a fresh temporary PdfWriter that is immediately
    # discarded, so no pages leak into the final writer ahead of time.
    while len(pages) % 4 != 0:
        pad = PdfWriter()
        blank = pad.add_blank_page(width=page_width, height=page_height)
        pages.append(blank)

    num_pages = len(pages)

    # Build booklet page order (0-based indices).
    # Sheet i front : [last-i, i]   for even i
    # Sheet i back  : [i,   last-i] for odd i
    new_order: list[int] = []
    for i in range(num_pages // 2):
        if i % 2 == 0:
            new_order.append(num_pages - 1 - i)
            new_order.append(i)
        else:
            new_order.append(i)
            new_order.append(num_pages - 1 - i)

    writer = PdfWriter()
    for idx in new_order:
        writer.add_page(pages[idx])

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()
