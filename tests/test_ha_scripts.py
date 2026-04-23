"""Tests for the home-assistant/ standalone scripts.

These test the Phase-1 fixed scripts that run as shell-command subprocesses
inside Home Assistant.  All filesystem and IMAP I/O is mocked.

Covers:
  - pdf_downloader.download_attachment: regression for UnboundLocalError in
    finally when the IMAP connection itself fails.
  - pdf_downloader.download_attachment: returns None on login failure.
  - pdf_downloader.download_attachment: returns None when attachment not found.
  - pdf_downloader.download_attachment: saves file and returns path on success.
  - booklet_maker.create_booklet (file-based): correct page count and no
    double-adding of blank pages.
  - booklet_maker.create_booklet (file-based): output filename has -booklet suffix.
"""

import importlib.util
import io
import unittest.mock as mock
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

from conftest import ROOT, make_pdf

# ── explicit importlib loads to avoid name shadowing with custom-component ────
# Both home-assistant/ and custom_components/print_bridge/ have booklet_maker.py.
# We load the HA version by its absolute path to guarantee the right module.

def _load_module(name: str, rel_path: str):
    abs_path = ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, abs_path)
    mod = importlib.util.module_from_spec(spec)   # type: ignore[arg-type]
    spec.loader.exec_module(mod)                   # type: ignore[union-attr]
    return mod

pdf_downloader = _load_module("ha_pdf_downloader", "home-assistant/pdf_downloader.py")
ha_booklet_maker = _load_module("ha_booklet_maker", "home-assistant/booklet_maker.py")


# ── pdf_downloader ────────────────────────────────────────────────────────────


def _build_raw_email(pdf_bytes: bytes, filename: str) -> bytes:
    msg = MIMEMultipart()
    msg["From"] = "sender@example.com"
    part = MIMEApplication(pdf_bytes, Name=filename)
    part["Content-Disposition"] = f'attachment; filename="{filename}"'
    msg.attach(part)
    return msg.as_bytes()


def _mock_secrets(monkeypatch):
    monkeypatch.setattr(
        pdf_downloader,
        "_get_secret",
        lambda key: "testuser" if key == "email_user" else "testpass",
    )


def test_download_attachment_no_unbound_error_on_connection_failure(monkeypatch, tmp_path):
    """Regression: when IMAP4_SSL() raises, the finally block must not raise
    UnboundLocalError because mail was never assigned.
    """
    _mock_secrets(monkeypatch)
    monkeypatch.setattr(
        pdf_downloader,
        "SAVE_PATH",
        str(tmp_path),
    )
    with mock.patch("imaplib.IMAP4_SSL", side_effect=OSError("refused")):
        result = pdf_downloader.download_attachment("42", "doc.pdf")  # must not raise

    assert result is None


def test_download_attachment_returns_none_on_login_failure(monkeypatch, tmp_path):
    _mock_secrets(monkeypatch)
    monkeypatch.setattr(pdf_downloader, "SAVE_PATH", str(tmp_path))

    imap_mock = mock.MagicMock()
    imap_mock.login.return_value = ("NO", [b"Bad credentials"])

    with mock.patch("imaplib.IMAP4_SSL", return_value=imap_mock):
        result = pdf_downloader.download_attachment("1", "a.pdf")

    assert result is None


def test_download_attachment_returns_none_when_attachment_not_found(monkeypatch, tmp_path):
    _mock_secrets(monkeypatch)
    monkeypatch.setattr(pdf_downloader, "SAVE_PATH", str(tmp_path))

    # Email has no attachments at all.
    raw = MIMEMultipart().as_bytes()
    imap_mock = mock.MagicMock()
    imap_mock.login.return_value = ("OK", [])
    imap_mock.select.return_value = ("OK", [b"1"])
    imap_mock.uid.return_value = ("OK", [(b"1 (RFC822)", raw)])

    with mock.patch("imaplib.IMAP4_SSL", return_value=imap_mock):
        result = pdf_downloader.download_attachment("1", "missing.pdf")

    assert result is None


def test_download_attachment_saves_file_and_returns_path(monkeypatch, tmp_path):
    _mock_secrets(monkeypatch)
    monkeypatch.setattr(pdf_downloader, "SAVE_PATH", str(tmp_path))

    pdf_bytes = b"%PDF-1.4 test content"
    raw = _build_raw_email(pdf_bytes, "invoice.pdf")

    imap_mock = mock.MagicMock()
    imap_mock.login.return_value = ("OK", [])
    imap_mock.select.return_value = ("OK", [b"1"])
    imap_mock.uid.return_value = ("OK", [(b"1 (RFC822)", raw)])

    with mock.patch("imaplib.IMAP4_SSL", return_value=imap_mock):
        path = pdf_downloader.download_attachment("99", "invoice.pdf")

    assert path is not None
    saved = Path(path)
    assert saved.exists()
    assert saved.read_bytes() == pdf_bytes


def test_download_attachment_logout_called_on_success(monkeypatch, tmp_path):
    _mock_secrets(monkeypatch)
    monkeypatch.setattr(pdf_downloader, "SAVE_PATH", str(tmp_path))

    pdf_bytes = b"%PDF-1.4"
    raw = _build_raw_email(pdf_bytes, "doc.pdf")

    imap_mock = mock.MagicMock()
    imap_mock.login.return_value = ("OK", [])
    imap_mock.select.return_value = ("OK", [b"1"])
    imap_mock.uid.return_value = ("OK", [(b"1 (RFC822)", raw)])

    with mock.patch("imaplib.IMAP4_SSL", return_value=imap_mock):
        pdf_downloader.download_attachment("1", "doc.pdf")

    imap_mock.logout.assert_called_once()


# ── home-assistant booklet_maker (file-based) ─────────────────────────────────


def _write_pdf(path: Path, num_pages: int) -> Path:
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=595, height=842)
    path.write_bytes(_pdf_bytes(writer))
    return path


def _pdf_bytes(writer: PdfWriter) -> bytes:
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _page_count(path: Path) -> int:
    return len(PdfReader(str(path)).pages)


@pytest.mark.parametrize(
    "input_pages, expected_pages",
    [
        (4, 4),
        (6, 8),   # padded: 6 → 8
        (3, 4),   # padded: 3 → 4
    ],
)
def test_ha_booklet_page_count(tmp_path, input_pages, expected_pages):
    src = _write_pdf(tmp_path / "source.pdf", input_pages)
    out = ha_booklet_maker.create_booklet(str(src))
    assert _page_count(Path(out)) == expected_pages


def test_ha_booklet_no_double_blank_pages(tmp_path):
    """Regression: 6-page input must produce exactly 8 pages (not more)."""
    src = _write_pdf(tmp_path / "source.pdf", 6)
    out = ha_booklet_maker.create_booklet(str(src))
    count = _page_count(Path(out))
    assert count == 8, f"Expected 8 pages but got {count} — blank pages may be doubled"


def test_ha_booklet_output_filename_has_booklet_suffix(tmp_path):
    src = _write_pdf(tmp_path / "document.pdf", 4)
    out = ha_booklet_maker.create_booklet(str(src))
    assert out.endswith("-booklet.pdf")


def test_ha_booklet_output_file_exists(tmp_path):
    src = _write_pdf(tmp_path / "doc.pdf", 4)
    out = ha_booklet_maker.create_booklet(str(src))
    assert Path(out).exists()
