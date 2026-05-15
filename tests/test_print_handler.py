"""Tests for custom_components/print_bridge/print_handler.py.

Covers:
  - IPP 2.0 packet structure (header bytes, attribute encoding).
  - determine_sides() logic for all duplex / booklet combinations.
  - is_booklet_job() pattern matching (case-insensitive, partial match).
"""

import struct

import pytest

from print_handler import (
    build_get_printer_attributes_packet,
    build_ipp_packet,
    determine_sides,
    ipp_response_succeeded,
    is_booklet_job,
    parse_ipp_attributes,
    parse_ipp_response_status,
    sanitize_ipp_job_name,
)

PDF_STUB = b"%PDF-1.4 minimal"
PRINTER = "Canon_MG3600_series"


# ── IPP packet structure ──────────────────────────────────────────────────────


def test_ipp_packet_starts_with_version_20():
    pkt = build_ipp_packet(PRINTER, "test.pdf", "one-sided", PDF_STUB)
    version = struct.unpack(">H", pkt[:2])[0]
    assert version == 0x0200, "First 2 bytes must be IPP version 2.0 (0x0200)"


def test_ipp_packet_operation_is_print_job():
    pkt = build_ipp_packet(PRINTER, "test.pdf", "one-sided", PDF_STUB)
    op_id = struct.unpack(">H", pkt[2:4])[0]
    assert op_id == 0x0002, "Bytes 2-3 must be Print-Job operation-id (0x0002)"


def test_ipp_packet_request_id_is_one():
    pkt = build_ipp_packet(PRINTER, "test.pdf", "one-sided", PDF_STUB)
    req_id = struct.unpack(">I", pkt[4:8])[0]
    assert req_id == 1


def test_ipp_packet_contains_charset_attribute():
    pkt = build_ipp_packet(PRINTER, "test.pdf", "one-sided", PDF_STUB)
    assert b"attributes-charset" in pkt
    assert b"utf-8" in pkt


def test_ipp_packet_contains_document_format():
    pkt = build_ipp_packet(PRINTER, "test.pdf", "one-sided", PDF_STUB)
    assert b"document-format" in pkt
    assert b"application/pdf" in pkt


def test_ipp_packet_can_use_non_pdf_document_format():
    pkt = build_ipp_packet(
        PRINTER,
        "test.pdf",
        "one-sided",
        b"raster",
        document_format="image/pwg-raster",
    )
    assert b"document-format" in pkt
    assert b"image/pwg-raster" in pkt
    assert pkt.endswith(b"raster")


def test_ipp_packet_contains_sides_attribute():
    pkt = build_ipp_packet(PRINTER, "test.pdf", "two-sided-long-edge", PDF_STUB)
    assert b"sides" in pkt
    assert b"two-sided-long-edge" in pkt


def test_ipp_packet_can_include_copies_orientation_and_media():
    pkt = build_ipp_packet(
        PRINTER,
        "booklet.pdf",
        "two-sided-short-edge",
        PDF_STUB,
        copies=2,
        orientation_requested=4,
        media="iso_a4_210x297mm",
        print_scaling="fit",
    )

    assert _ipp_attr(0x21, "copies", struct.pack(">i", 2)) in pkt
    assert _ipp_attr(0x23, "orientation-requested", struct.pack(">i", 4)) in pkt
    assert b"media" in pkt
    assert b"iso_a4_210x297mm" in pkt
    assert b"print-scaling" in pkt
    assert b"fit" in pkt


def test_ipp_packet_ends_with_pdf_data():
    pkt = build_ipp_packet(PRINTER, "test.pdf", "one-sided", PDF_STUB)
    assert pkt.endswith(PDF_STUB)


def test_ipp_packet_end_of_attributes_tag_present():
    """The end-of-attributes tag (0x03) must appear before the PDF payload."""
    pkt = build_ipp_packet(PRINTER, "test.pdf", "one-sided", PDF_STUB)
    header = pkt[: -len(PDF_STUB)]
    assert b"\x03" in header


def test_ipp_packet_job_name_encoded():
    pkt = build_ipp_packet(PRINTER, "my-document.pdf", "one-sided", PDF_STUB)
    assert b"my-document.pdf" in pkt


def test_sanitize_ipp_job_name_removes_direction_marks():
    name = "\u200f" * 200 + "שיחת שבוע בצרפתית 294 - אמור.pdf"

    assert sanitize_ipp_job_name(name) == "שיחת שבוע בצרפתית 294 - אמור.pdf"


def test_sanitize_ipp_job_name_limits_utf8_bytes_and_keeps_extension():
    name = ("שיחת שבוע " * 80) + ".pdf"
    clean_name = sanitize_ipp_job_name(name)

    assert len(clean_name.encode("utf-8")) <= 255
    assert clean_name.endswith(".pdf")


def test_ipp_packet_sanitizes_job_name_before_encoding():
    pkt = build_ipp_packet(
        PRINTER,
        "\u200f" * 20 + "שיחת שבוע בצרפתית.pdf",
        "one-sided",
        PDF_STUB,
    )

    assert "\u200f".encode("utf-8") not in pkt
    assert "שיחת שבוע בצרפתית.pdf".encode("utf-8") in pkt


def test_parse_successful_ipp_response():
    body = struct.pack(">HHI", 0x0200, 0x0000, 1) + b"\x03"

    status, description = parse_ipp_response_status(body)
    ok, ok_description = ipp_response_succeeded(body)

    assert status == 0x0000
    assert "successful-ok" in description
    assert ok is True
    assert ok_description == description


def test_parse_rejected_ipp_response():
    body = struct.pack(">HHI", 0x0200, 0x040A, 1) + b"\x03"

    status, description = parse_ipp_response_status(body)
    ok, ok_description = ipp_response_succeeded(body)

    assert status == 0x040A
    assert "document-format-not-supported" in description
    assert ok is False
    assert ok_description == description


def test_parse_busy_ipp_response():
    body = struct.pack(">HHI", 0x0200, 0x0507, 1) + b"\x03"

    status, description = parse_ipp_response_status(body)
    ok, ok_description = ipp_response_succeeded(body)

    assert status == 0x0507
    assert "server-error-busy" in description
    assert ok is False
    assert ok_description == description


def test_parse_html_response_is_not_ipp_success():
    ok, description = ipp_response_succeeded(b"<!DOCTYPE HTML><html></html>")

    assert ok is False
    assert "Invalid IPP response version" in description


def test_get_printer_attributes_packet_uses_operation_id():
    pkt = build_get_printer_attributes_packet("ipp://printer.local/ipp/print")
    op_id = struct.unpack(">H", pkt[2:4])[0]
    assert op_id == 0x000B
    assert b"Get-Printer-Attributes" not in pkt
    assert b"document-format-supported" in pkt
    assert b"pwg-raster-document-type-supported" in pkt


def _ipp_attr(tag: int, name: str, value: bytes) -> bytes:
    name_b = name.encode()
    return (
        struct.pack(">BH", tag, len(name_b))
        + name_b
        + struct.pack(">H", len(value))
        + value
    )


def _ipp_more_attr(tag: int, value: bytes) -> bytes:
    return struct.pack(">BH", tag, 0) + struct.pack(">H", len(value)) + value


def test_parse_ipp_attributes_repeated_values_and_resolution():
    body = (
        struct.pack(">HHI", 0x0200, 0x0000, 1)
        + b"\x04"
        + _ipp_attr(0x49, "document-format-supported", b"application/octet-stream")
        + _ipp_more_attr(0x49, b"image/pwg-raster")
        + _ipp_attr(0x44, "pwg-raster-document-type-supported", b"srgb_8")
        + _ipp_attr(
            0x32,
            "pwg-raster-document-resolution-supported",
            struct.pack(">IIB", 600, 600, 3),
        )
        + b"\x03"
    )

    attrs = parse_ipp_attributes(body)

    assert attrs["document-format-supported"] == [
        "application/octet-stream",
        "image/pwg-raster",
    ]
    assert attrs["pwg-raster-document-type-supported"] == ["srgb_8"]
    assert attrs["pwg-raster-document-resolution-supported"] == ["600dpi"]


def test_parse_ipp_attributes_printer_readiness_values():
    body = (
        struct.pack(">HHI", 0x0200, 0x0000, 1)
        + b"\x04"
        + _ipp_attr(0x22, "printer-is-accepting-jobs", b"\x01")
        + _ipp_attr(0x23, "printer-state", struct.pack(">i", 4))
        + _ipp_attr(0x44, "printer-state-reasons", b"none")
        + _ipp_attr(0x21, "queued-job-count", struct.pack(">i", 2))
        + b"\x03"
    )

    attrs = parse_ipp_attributes(body)

    assert attrs["printer-is-accepting-jobs"] == ["true"]
    assert attrs["printer-state"] == ["processing"]
    assert attrs["printer-state-reasons"] == ["none"]
    assert attrs["queued-job-count"] == ["2"]


# ── determine_sides ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "duplex_mode, booklet, expected_sides",
    [
        ("one-sided", False, "one-sided"),
        ("two-sided-long-edge", False, "two-sided-long-edge"),
        ("two-sided-short-edge", False, "two-sided-short-edge"),
        # Booklet always overrides duplex_mode with short-edge.
        ("one-sided", True, "two-sided-short-edge"),
        ("two-sided-long-edge", True, "two-sided-short-edge"),
        ("two-sided-short-edge", True, "two-sided-short-edge"),
    ],
)
def test_determine_sides(duplex_mode, booklet, expected_sides):
    assert determine_sides(duplex_mode, booklet) == expected_sides


# ── is_booklet_job ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "filename, patterns, expected",
    [
        ("Sunday Programme.pdf", ["Programme"], True),
        ("sunday programme.pdf", ["Programme"], True),    # case-insensitive
        ("SUNDAY PROGRAMME.pdf", ["programme"], True),
        ("regular_invoice.pdf", ["Programme"], False),
        ("", ["Programme"], False),
        ("booklet.pdf", [], False),                        # no patterns configured
        ("Bulletin.pdf", ["Bulletin", "Programme"], True),  # first pattern matches
        ("other.pdf", ["Bulletin", "Programme"], False),
    ],
)
def test_is_booklet_job(filename, patterns, expected):
    assert is_booklet_job(filename, patterns) is expected
