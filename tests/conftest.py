"""Shared pytest fixtures and sys.path wiring for print-bridge tests."""

import io
import sys
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

# ── path setup ───────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
# Add the print_bridge package directory first so its pure modules are found
# ahead of home-assistant/ when module names collide (e.g. booklet_maker).
sys.path.insert(0, str(ROOT / "home-assistant"))
sys.path.insert(0, str(ROOT / "custom_components" / "print_bridge"))


# ── shared helpers ────────────────────────────────────────────────────────────

def make_pdf(num_pages: int) -> bytes:
    """Create a minimal valid PDF with *num_pages* blank A4 pages."""
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def pdf_page_count(data: bytes) -> int:
    """Return the number of pages in a PDF given as raw bytes."""
    return len(PdfReader(io.BytesIO(data)).pages)


@pytest.fixture()
def four_page_pdf() -> bytes:
    return make_pdf(4)


@pytest.fixture()
def eight_page_pdf() -> bytes:
    return make_pdf(8)
