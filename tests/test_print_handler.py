"""Tests for custom_components/auto_print/print_handler.py.

Covers:
  - IPP 2.0 packet structure (header bytes, attribute encoding).
  - determine_sides() logic for all duplex / booklet combinations.
  - is_booklet_job() pattern matching (case-insensitive, partial match).
"""

import struct

import pytest

from print_handler import (
    build_ipp_packet,
    determine_sides,
    is_booklet_job,
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


def test_ipp_packet_contains_sides_attribute():
    pkt = build_ipp_packet(PRINTER, "test.pdf", "two-sided-long-edge", PDF_STUB)
    assert b"sides" in pkt
    assert b"two-sided-long-edge" in pkt


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
        ("Au Puits de Jacob.pdf", ["Au Puits"], True),
        ("au puits de jacob.pdf", ["Au Puits"], True),    # case-insensitive
        ("AU PUITS programme.pdf", ["au puits"], True),
        ("regular_invoice.pdf", ["Au Puits"], False),
        ("", ["Au Puits"], False),
        ("booklet.pdf", [], False),                        # no patterns configured
        ("Bulletin.pdf", ["Bulletin", "Au Puits"], True),  # first pattern matches
        ("other.pdf", ["Bulletin", "Au Puits"], False),
    ],
)
def test_is_booklet_job(filename, patterns, expected):
    assert is_booklet_job(filename, patterns) is expected
