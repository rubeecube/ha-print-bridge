"""Rearrange a PDF's pages for saddle-stitch (booklet) printing.

Usage:
    python3 booklet_maker.py <input.pdf>

Prints the path to the reordered output PDF on success.

Booklet page order for N sheets (N must be a multiple of 4):
    Sheet 1 front : [N,   1]
    Sheet 1 back  : [2,   N-1]
    Sheet 2 front : [N-2, 3]
    Sheet 2 back  : [4,   N-3]  ...

When the original page count is not a multiple of 4, blank pages are appended
until it is.  The blank pages are constructed via a *temporary* PdfWriter that
is separate from the final output writer, so they are never double-added.
"""

import logging
import os
import sys

from pypdf import PdfReader, PdfWriter

logger = logging.getLogger(__name__)


def create_booklet(input_path: str) -> str:
    """Reorder pages of *input_path* for booklet printing.

    Returns the path to the new file (same directory, ``-booklet`` suffix).
    """
    output_path = input_path.replace(".pdf", "-booklet.pdf")

    reader = PdfReader(input_path)
    pages = list(reader.pages)

    if not pages:
        raise ValueError(f"PDF has no pages: {input_path}")

    page_width = pages[0].mediabox.width
    page_height = pages[0].mediabox.height

    # Pad to next multiple of 4.
    # FIX: blank pages are created via a *temporary* writer so they are not
    # pre-added to the final writer before the reordering loop runs.
    while len(pages) % 4 != 0:
        pad_writer = PdfWriter()
        blank = pad_writer.add_blank_page(width=page_width, height=page_height)
        pages.append(blank)

    num_pages = len(pages)

    # Build the booklet page order (0-based indices).
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

    with open(output_path, "wb") as f:
        writer.write(f)

    return output_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR, stream=sys.stderr)
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <input.pdf>", file=sys.stderr)
        sys.exit(1)

    input_file = sys.argv[1]
    if not os.path.exists(input_file):
        print(f"File not found: {input_file}", file=sys.stderr)
        sys.exit(1)

    print(create_booklet(input_file))
