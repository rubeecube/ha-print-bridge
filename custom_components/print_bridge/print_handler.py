"""IPP 2.0 packet construction for the Print Bridge integration.

All functions are pure (no I/O, no HA dependencies).  Network I/O is handled
by the coordinator using aiohttp.

Two printing modes are supported:

  CUPS mode  — POST to http://cups-host:631/printers/<queue-name>
               printer-uri attribute = ipp://cups-host:631/printers/<queue-name>

  Direct IPP — POST to http://printer-ip/ipp/print  (or :631/ipp/print)
               printer-uri attribute = ipp://printer-ip/ipp/print
               Works with any AirPrint / IPP-capable printer without CUPS.
"""

from __future__ import annotations

import logging
import os
import struct
import unicodedata

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
_TAG_INTEGER = 0x21          # integer
_TAG_ENUM = 0x23             # enum

# IPP group delimiter tags
_GROUP_OPERATION = b"\x01"
_GROUP_JOB = b"\x02"
_GROUP_END = b"\x03"

_IPP_STATUS_NAMES = {
    0x0000: "successful-ok",
    0x0001: "successful-ok-ignored-or-substituted-attributes",
    0x0002: "successful-ok-conflicting-attributes",
    0x0400: "client-error-bad-request",
    0x0403: "client-error-forbidden",
    0x0404: "client-error-not-possible",
    0x0405: "client-error-timeout",
    0x0406: "client-error-not-found",
    0x0408: "client-error-request-entity-too-large",
    0x0409: "client-error-request-value-too-long",
    0x040A: "client-error-document-format-not-supported",
    0x040B: "client-error-attributes-or-values-not-supported",
    0x040C: "client-error-uri-scheme-not-supported",
    0x0412: "client-error-not-accepting-jobs",
    0x0500: "server-error-internal-error",
    0x0503: "server-error-service-unavailable",
    0x0504: "server-error-version-not-supported",
    0x0505: "server-error-device-error",
}


def _truncate_utf8(value: str, max_bytes: int) -> str:
    """Truncate a string to a UTF-8 byte budget, preserving whole characters."""
    if len(value.encode("utf-8")) <= max_bytes:
        return value

    stem, extension = os.path.splitext(value)
    extension_bytes = extension.encode("utf-8")
    suffix = extension if len(extension_bytes) < max_bytes - 16 else ""
    budget = max_bytes - len(suffix.encode("utf-8"))
    truncated = ""
    used = 0
    for character in stem:
        encoded = character.encode("utf-8")
        if used + len(encoded) > budget:
            break
        truncated += character
        used += len(encoded)
    return (truncated.strip() or "print-bridge-job") + suffix


def sanitize_ipp_job_name(file_name: str, max_bytes: int = 255) -> str:
    """Return a printer-safe IPP job name.

    Some email senders prepend many invisible Unicode format characters to
    attachment filenames. Home printers can reject or stall on very long IPP
    ``nameWithoutLanguage`` values, so strip control/format characters and keep
    the encoded job-name inside the common IPP 255-octet limit.
    """
    name = os.path.basename(str(file_name or "")).replace("\x00", "")
    name = unicodedata.normalize("NFC", name)
    name = "".join(
        character
        for character in name
        if unicodedata.category(character)[0] != "C"
    )
    name = " ".join(name.split()).strip()
    if not name:
        name = "print-bridge-job.pdf"
    return _truncate_utf8(name, max_bytes)


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


def _encode_int_attr(tag: int, name: str, value: int) -> bytes:
    """Encode one IPP integer/enum attribute."""
    name_b = name.encode()
    return (
        struct.pack(">BH", tag, len(name_b))
        + name_b
        + struct.pack(">H", 4)
        + struct.pack(">i", value)
    )


def http_url_to_ipp_uri(http_url: str) -> str:
    """Convert an http(s) endpoint URL to its ipp(s) equivalent.

    ``http://host:631/ipp/print`` → ``ipp://host:631/ipp/print``

    The IPP ``printer-uri`` attribute must use the ``ipp://`` or ``ipps://``
    scheme even when the transport uses plain HTTP/HTTPS.
    """
    url = http_url.strip()
    if url.startswith("ipp://") or url.startswith("ipps://"):
        return url
    if url.startswith("https://"):
        return "ipps://" + url[len("https://"):]
    if url.startswith("http://"):
        return "ipp://" + url[len("http://"):]
    return url  # already correct or unknown scheme


def cups_printer_uri(cups_base_url: str, queue_name: str) -> str:
    """Return the correct IPP ``printer-uri`` for a CUPS queue.

    ``http://cups.local:631``, ``Canon_MG3600_series``
    → ``ipp://cups.local:631/printers/Canon_MG3600_series``
    """
    base = cups_base_url.rstrip("/")
    # Strip the http/https scheme and re-add as ipp/ipps
    if base.startswith("http://"):
        ipp_base = "ipp://" + base[len("http://"):]
    elif base.startswith("https://"):
        ipp_base = "ipps://" + base[len("https://"):]
    else:
        ipp_base = base
    return f"{ipp_base}/printers/{queue_name}"


_REQUESTED_CAPABILITY_ATTRIBUTES = (
    "document-format-supported",
    "document-format-default",
    "pdf-versions-supported",
    "pwg-raster-document-type-supported",
    "pwg-raster-document-resolution-supported",
    "pwg-raster-document-sheet-back",
    "sides-supported",
    "printer-make-and-model",
    "printer-name",
)


def build_ipp_packet(
    printer_uri: str,
    file_name: str,
    sides: str,
    document_data: bytes,
    document_format: str = "application/pdf",
    copies: int | None = None,
    orientation_requested: int | None = None,
    media: str | None = None,
) -> bytes:
    """Construct a valid IPP 2.0 Print-Job request packet.

    Args:
        printer_uri:  The full IPP ``printer-uri`` value, e.g.
                      ``ipp://cups.local:631/printers/Canon_MG3600_series``
                      or ``ipp://printer.local/ipp/print``.
        file_name:    Display name for the job (the PDF's filename).
        sides:        IPP sides keyword, e.g. "two-sided-long-edge".
        document_data: Raw bytes of the document file appended after the IPP header.
        document_format: IPP document-format value for the appended bytes.
        copies: Optional IPP ``copies`` value.
        orientation_requested: Optional IPP orientation enum
                              (3=portrait, 4=landscape).
        media: Optional IPP media keyword, e.g. ``iso_a4_210x297mm``.

    Returns:
        Complete IPP request bytes ready to POST to the printer/CUPS endpoint.
    """
    # IPP/2.0 header: version(2B) + Print-Job op-id(2B) + request-id(4B)
    header = struct.pack(">HHI", 0x0200, 0x0002, 0x00000001)

    header += _GROUP_OPERATION
    header += _encode_attr(_TAG_CHARSET, "attributes-charset", "utf-8")
    header += _encode_attr(_TAG_NAT_LANG, "attributes-natural-language", "en")
    header += _encode_attr(_TAG_URI, "printer-uri", printer_uri)
    header += _encode_attr(_TAG_NAME, "job-name", sanitize_ipp_job_name(file_name))
    header += _encode_attr(_TAG_MIME, "document-format", document_format)

    header += _GROUP_JOB
    header += _encode_attr(_TAG_KEYWORD, "sides", sides)
    if copies is not None:
        header += _encode_int_attr(_TAG_INTEGER, "copies", copies)
    if orientation_requested is not None:
        header += _encode_int_attr(
            _TAG_ENUM, "orientation-requested", orientation_requested
        )
    if media:
        header += _encode_attr(_TAG_KEYWORD, "media", media)

    header += _GROUP_END

    return header + document_data


def build_get_printer_attributes_packet(
    printer_uri: str,
    requested_attributes: tuple[str, ...] = _REQUESTED_CAPABILITY_ATTRIBUTES,
) -> bytes:
    """Construct an IPP Get-Printer-Attributes request."""
    header = struct.pack(">HHI", 0x0200, 0x000B, 0x00000001)
    header += _GROUP_OPERATION
    header += _encode_attr(_TAG_CHARSET, "attributes-charset", "utf-8")
    header += _encode_attr(_TAG_NAT_LANG, "attributes-natural-language", "en")
    header += _encode_attr(_TAG_URI, "printer-uri", printer_uri)
    for index, attr in enumerate(requested_attributes):
        name = "requested-attributes" if index == 0 else ""
        header += _encode_attr(_TAG_KEYWORD, name, attr)
    header += _GROUP_END
    return header


def parse_ipp_attributes(response: bytes) -> dict[str, list[str]]:
    """Parse simple string-like attributes from an IPP response body.

    This intentionally handles only the IPP value types Print Bridge needs for
    capability display and format selection.
    """
    attrs: dict[str, list[str]] = {}
    if len(response) < 9:
        return attrs

    offset = 8
    current_name: str | None = None
    string_tags = {
        _TAG_CHARSET,
        _TAG_NAT_LANG,
        _TAG_URI,
        _TAG_NAME,
        _TAG_MIME,
        _TAG_KEYWORD,
        0x41,  # textWithoutLanguage
    }

    while offset < len(response):
        tag = response[offset]
        offset += 1
        if tag == _GROUP_END[0]:
            break
        if tag in (0x01, 0x02, 0x04, 0x05):
            current_name = None
            continue
        if offset + 2 > len(response):
            break

        name_len = struct.unpack(">H", response[offset:offset + 2])[0]
        offset += 2
        if offset + name_len > len(response):
            break
        name_bytes = response[offset:offset + name_len]
        offset += name_len
        if name_len:
            current_name = name_bytes.decode("utf-8", errors="replace")
        if offset + 2 > len(response):
            break

        value_len = struct.unpack(">H", response[offset:offset + 2])[0]
        offset += 2
        if offset + value_len > len(response):
            break
        value = response[offset:offset + value_len]
        offset += value_len

        if not current_name:
            continue
        if tag in string_tags:
            attrs.setdefault(current_name, []).append(
                value.decode("utf-8", errors="replace")
            )
        elif tag == 0x32 and value_len == 9:  # resolution
            xres, yres, units = struct.unpack(">IIB", value)
            suffix = "dpi" if units == 3 else "dpcm"
            if xres == yres:
                rendered = f"{xres}{suffix}"
            else:
                rendered = f"{xres}x{yres}{suffix}"
            attrs.setdefault(current_name, []).append(rendered)

    return attrs


def parse_ipp_response_status(response: bytes) -> tuple[int | None, str]:
    """Return ``(status_code, description)`` from a binary IPP response.

    IPP is transported over HTTP, but the operation result lives in the IPP
    response body. HTTP 200 can still carry an IPP client/server error.
    """
    if len(response) < 4:
        return None, "Invalid IPP response: shorter than 4 bytes"

    major, minor = response[0], response[1]
    if major not in (1, 2):
        return None, f"Invalid IPP response version: {major}.{minor}"

    status_code = struct.unpack(">H", response[2:4])[0]
    name = _IPP_STATUS_NAMES.get(status_code, "unknown-status")
    return status_code, f"IPP 0x{status_code:04x} {name}"


def ipp_response_succeeded(response: bytes) -> tuple[bool, str]:
    """Return whether an IPP response body represents a successful operation."""
    status_code, description = parse_ipp_response_status(response)
    if status_code is None:
        return False, description
    return status_code < 0x0100, description


def determine_sides(duplex_mode: str, is_booklet: bool) -> str:
    """Return the IPP ``sides`` keyword for the given duplex mode and booklet flag."""
    if is_booklet:
        return "two-sided-short-edge"
    return duplex_mode


def is_booklet_job(filename: str, booklet_patterns: list[str]) -> bool:
    """Return True if *filename* matches any of the booklet trigger patterns."""
    lower = filename.lower()
    return any(pattern.lower() in lower for pattern in booklet_patterns)
