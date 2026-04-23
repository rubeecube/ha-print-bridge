"""IPP 2.0 packet construction for the Auto Print integration.

All functions are pure (no I/O, no HA dependencies).  Network I/O is handled
by the coordinator using aiohttp.
"""

from __future__ import annotations

import logging
import struct

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# IPP value-tag constants (RFC 8011)
# ---------------------------------------------------------------------------
_TAG_CHARSET = 0x47          # charset
_TAG_NAT_LANG = 0x48         # naturalLanguage
_TAG_URI = 0x45              # uri
_TAG_NAME = 0x42             # nameWithoutLanguage
_TAG_MIME = 0x49             # mimeMediaType
_TAG_KEYWORD = 0x44          # keyword

# IPP group delimiter tags
_GROUP_OPERATION = b"\x01"
_GROUP_JOB = b"\x02"
_GROUP_END = b"\x03"


def _encode_attr(tag: int, name: str, value: str) -> bytes:
    """Encode one IPP attribute: tag(1B) + name-len(2B) + name + value-len(2B) + value."""
    name_b = name.encode()
    value_b = value.encode()
    return (
        struct.pack(">BH", tag, len(name_b))
        + name_b
        + struct.pack(">H", len(value_b))
        + value_b
    )


def build_ipp_packet(printer_name: str, file_name: str, sides: str, pdf_data: bytes) -> bytes:
    """Construct a valid IPP 2.0 Print-Job request packet.

    Args:
        printer_name: CUPS queue name, used to build the printer-uri attribute.
        file_name:    Display name for the job (the PDF's filename).
        sides:        IPP sides keyword, e.g. "two-sided-long-edge".
        pdf_data:     Raw bytes of the PDF file (appended after the IPP header).

    Returns:
        Complete IPP request bytes ready to POST to the CUPS printer endpoint.
    """
    # IPP/2.0 header: version(2B) + Print-Job op-id(2B) + request-id(4B)
    header = struct.pack(">HHI", 0x0200, 0x0002, 0x00000001)

    header += _GROUP_OPERATION
    header += _encode_attr(_TAG_CHARSET, "attributes-charset", "utf-8")
    header += _encode_attr(_TAG_NAT_LANG, "attributes-natural-language", "en")
    header += _encode_attr(
        _TAG_URI, "printer-uri", f"ipp://localhost/printers/{printer_name}"
    )
    header += _encode_attr(_TAG_NAME, "job-name", file_name)
    header += _encode_attr(_TAG_MIME, "document-format", "application/pdf")

    header += _GROUP_JOB
    header += _encode_attr(_TAG_KEYWORD, "sides", sides)

    header += _GROUP_END

    return header + pdf_data


def determine_sides(duplex_mode: str, is_booklet: bool) -> str:
    """Return the IPP ``sides`` keyword for the given duplex mode and booklet flag.

    Booklet jobs are always printed two-sided on the short edge so the pages
    fold correctly.
    """
    if is_booklet:
        return "two-sided-short-edge"
    return duplex_mode


def is_booklet_job(filename: str, booklet_patterns: list[str]) -> bool:
    """Return True if *filename* matches any of the booklet trigger patterns."""
    lower = filename.lower()
    return any(pattern.lower() in lower for pattern in booklet_patterns)
