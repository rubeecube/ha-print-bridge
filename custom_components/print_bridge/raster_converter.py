"""PDF-to-printer-format conversion helpers.

Direct IPP printers often do not accept PDF bytes even when they support
AirPrint.  Many of those printers accept PWG Raster, which is a compressed
CUPS/PWG raster stream containing rendered page bitmaps.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import struct

from PIL import Image

_PWG_SYNC = b"RaS2"
_PWG_HEADER_LEN = 1796

_CUPS_ORDER_CHUNKED = 0
_CUPS_CSPACE_SW = 18
_CUPS_CSPACE_SRGB = 19

_PAGE_SIZE_NAMES = (
    ("iso_a4_210x297mm", 595, 842),
    ("na_letter_8.5x11in", 612, 792),
    ("na_legal_8.5x14in", 612, 1008),
)


@dataclass(frozen=True)
class ConvertedDocument:
    """Document bytes plus the MIME type to advertise over IPP."""

    data: bytes
    document_format: str


def convert_pdf_to_pwg_raster(
    pdf_data: bytes,
    sides: str,
    *,
    dpi: int = 300,
    color_type: str = "srgb_8",
    sheet_back: str | None = None,
) -> bytes:
    """Render a PDF and encode it as PWG Raster.

    The generated raster stream uses the standard CUPS/PWG Raster v2 header and
    PackBits-like row compression used by IPP Everywhere/AirPrint printers.
    """
    import pypdfium2 as pdfium

    if dpi <= 0:
        raise ValueError("dpi must be positive")
    if color_type not in {"srgb_8", "sgray_8"}:
        raise ValueError(f"Unsupported PWG raster type: {color_type}")

    document = pdfium.PdfDocument(pdf_data)
    if len(document) == 0:
        raise ValueError("PDF contains no pages")

    out = BytesIO()
    out.write(_PWG_SYNC)

    scale = dpi / 72.0
    for page_index in range(len(document)):
        page = document[page_index]
        width_pt, height_pt = page.get_size()
        bitmap = page.render(scale=scale)
        image = bitmap.to_pil()
        image = _prepare_page_image(image, color_type)

        out.write(
            _build_pwg_header(
                width_px=image.width,
                height_px=image.height,
                width_pt=width_pt,
                height_pt=height_pt,
                dpi=dpi,
                color_type=color_type,
                sides=sides,
                page_index=page_index,
                sheet_back=sheet_back,
            )
        )

        bpp = 3 if color_type == "srgb_8" else 1
        raw = image.tobytes()
        row_len = image.width * bpp
        for row_start in range(0, len(raw), row_len):
            out.write(_pack_pwg_row(raw[row_start: row_start + row_len], bpp))

    return out.getvalue()


def convert_pdf_to_jpeg(pdf_data: bytes, *, dpi: int = 300) -> bytes:
    """Render the first page of a single-page PDF to JPEG."""
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(pdf_data)
    if len(document) != 1:
        raise ValueError("JPEG direct printing supports only one-page PDFs")
    page = document[0]
    bitmap = page.render(scale=dpi / 72.0)
    image = bitmap.to_pil().convert("RGB")
    out = BytesIO()
    image.save(out, format="JPEG", quality=92)
    return out.getvalue()


def _prepare_page_image(image: Image.Image, color_type: str) -> Image.Image:
    if color_type == "sgray_8":
        return image.convert("L")
    return image.convert("RGB")


def _build_pwg_header(
    *,
    width_px: int,
    height_px: int,
    width_pt: float,
    height_pt: float,
    dpi: int,
    color_type: str,
    sides: str,
    page_index: int,
    sheet_back: str | None,
) -> bytes:
    header = bytearray(_PWG_HEADER_LEN)
    bits_per_pixel = 24 if color_type == "srgb_8" else 8
    num_colors = 3 if color_type == "srgb_8" else 1
    color_space = _CUPS_CSPACE_SRGB if color_type == "srgb_8" else _CUPS_CSPACE_SW
    bytes_per_line = width_px * num_colors
    page_width_pt = round(width_pt)
    page_height_pt = round(height_pt)
    duplex = 1 if sides in {"two-sided-long-edge", "two-sided-short-edge"} else 0
    tumble = 1 if sides == "two-sided-short-edge" else 0

    _put_cstr(header, 0, "PwgRaster")
    _put_cstr(header, 128, "stationery")
    _put_cstr(header, 1668, "auto")
    _put_cstr(header, 1732, _page_size_name(width_pt, height_pt))

    _put_u32(header, 272, duplex)
    _put_u32(header, 276, dpi)
    _put_u32(header, 280, dpi)
    _put_u32(header, 292, page_width_pt)
    _put_u32(header, 296, page_height_pt)
    _put_u32(header, 340, 1)
    _put_u32(header, 352, page_width_pt)
    _put_u32(header, 356, page_height_pt)
    _put_u32(header, 368, tumble)
    _put_u32(header, 372, width_px)
    _put_u32(header, 376, height_px)
    _put_u32(header, 384, 8)
    _put_u32(header, 388, bits_per_pixel)
    _put_u32(header, 392, bytes_per_line)
    _put_u32(header, 396, _CUPS_ORDER_CHUNKED)
    _put_u32(header, 400, color_space)
    _put_u32(header, 420, num_colors)

    cross_feed_transform = 1
    feed_transform = 1
    if page_index % 2 == 1 and sheet_back:
        if sheet_back == "flipped":
            if tumble:
                cross_feed_transform = 0xFFFFFFFF
            else:
                feed_transform = 0xFFFFFFFF
        elif sheet_back == "manual-tumble":
            if tumble:
                cross_feed_transform = 0xFFFFFFFF
                feed_transform = 0xFFFFFFFF
        elif sheet_back == "rotated":
            if not tumble:
                cross_feed_transform = 0xFFFFFFFF
                feed_transform = 0xFFFFFFFF

    _put_u32(header, 452, cross_feed_transform)
    _put_u32(header, 456, feed_transform)
    _put_u32(header, 464, 0)
    _put_u32(header, 468, 0)
    _put_u32(header, 472, width_px)
    _put_u32(header, 476, height_px)
    _put_u32(header, 480, 0xFFFFFF)
    return bytes(header)


def _pack_pwg_row(row: bytes, bpp: int) -> bytes:
    """Pack one row using the PackBits-like PWG/CUPS row encoding."""
    if len(row) % bpp:
        raise ValueError("Row length must be a multiple of bytes per pixel")

    out = bytearray()
    # Row repeat count: this row appears once.
    out.append(0)
    values = [row[index: index + bpp] for index in range(0, len(row), bpp)]
    index = 0
    total = len(values)
    while index < total:
        value = values[index]
        repeat = 1
        while index + repeat < total and repeat < 128 and values[index + repeat] == value:
            repeat += 1
        if repeat > 1:
            out.append(repeat - 1)
            out.extend(value)
            index += repeat
            continue

        literal_start = index
        index += 1
        while index < total and (index - literal_start) < 128:
            if index + 1 < total and values[index] == values[index + 1]:
                break
            index += 1
        literal_count = index - literal_start
        if literal_count == 1:
            out.append(0)
            out.extend(values[literal_start])
        else:
            out.append(257 - literal_count)
            for literal_index in range(literal_start, index):
                out.extend(values[literal_index])
    return bytes(out)


def _put_cstr(header: bytearray, offset: int, value: str, length: int = 64) -> None:
    encoded = value.encode("ascii", errors="ignore")[: length - 1]
    header[offset: offset + len(encoded)] = encoded


def _put_u32(header: bytearray, offset: int, value: int) -> None:
    header[offset: offset + 4] = struct.pack(">I", value)


def _page_size_name(width_pt: float, height_pt: float) -> str:
    for name, expected_width, expected_height in _PAGE_SIZE_NAMES:
        if abs(width_pt - expected_width) <= 4 and abs(height_pt - expected_height) <= 4:
            return name
        if abs(width_pt - expected_height) <= 4 and abs(height_pt - expected_width) <= 4:
            return name
    return "custom"
