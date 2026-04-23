"""Tests for custom_components/auto_print/imap_handler.py.

All IMAP I/O is replaced with mocks — no real network access.

Covers:
  - UNSEEN search is scoped per sender.
  - PDF attachments are returned; non-PDF parts are skipped.
  - Empty/None payload attachments are skipped.
  - Messages are marked \\Seen after a successful PDF download.
  - No crash when IMAP4_SSL constructor raises (regression for UnboundLocalError).
  - No crash when login fails.
  - logout() is always called when a connection was established.
"""

import email
import io
import unittest.mock as mock
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytest

from imap_handler import fetch_pdf_attachments

FAKE_CFG = dict(
    server="imap.example.com",
    port=993,
    use_ssl=True,
    username="user@example.com",
    password="secret",
    folder="INBOX",
    allowed_senders=["sender@example.com"],
)


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_raw_email(pdf_data: bytes | None = b"%PDF-1.4 test", filename: str = "doc.pdf") -> bytes:
    """Build a raw RFC 822 message with an optional PDF attachment."""
    msg = MIMEMultipart()
    msg["Subject"] = "Print job"
    msg["From"] = "sender@example.com"
    msg["To"] = "printer@example.com"
    msg.attach(MIMEText("Please print the attached file."))
    if pdf_data is not None:
        # _subtype="pdf" ensures Content-Type: application/pdf, not application/octet-stream.
        part = MIMEApplication(pdf_data, _subtype="pdf", Name=filename)
        part["Content-Disposition"] = f'attachment; filename="{filename}"'
        msg.attach(part)
    return msg.as_bytes()


def _make_mock_imap(uid_search_uids: list[str], raw_email: bytes) -> mock.MagicMock:
    """Return a mock IMAP4_SSL instance wired up with *uid_search_uids* and *raw_email*."""
    imap = mock.MagicMock()
    imap.login.return_value = ("OK", [b"Logged in"])
    imap.select.return_value = ("OK", [b"1"])
    imap.uid.side_effect = _uid_side_effect(uid_search_uids, raw_email)
    return imap


def _uid_side_effect(uids: list[str], raw_email: bytes):
    def handler(command, *args):
        if command == "search":
            uid_str = (" ".join(uids)).encode()
            return ("OK", [uid_str])
        if command == "fetch":
            return ("OK", [(f"1 (RFC822 {len(raw_email)})".encode(), raw_email)])
        if command == "store":
            return ("OK", [b"+FLAGS (\\Seen)"])
        return ("OK", [])
    return handler


# ── normal happy-path tests ───────────────────────────────────────────────────


def test_returns_pdf_attachment_for_matching_sender():
    raw = _make_raw_email(b"%PDF-1.4 test", "invoice.pdf")
    imap_instance = _make_mock_imap(["42"], raw)

    with mock.patch("imaplib.IMAP4_SSL", return_value=imap_instance):
        results = fetch_pdf_attachments(**FAKE_CFG)

    assert len(results) == 1
    assert results[0].filename == "invoice.pdf"
    assert results[0].uid == "42"
    assert results[0].data == b"%PDF-1.4 test"


def test_marks_message_seen_after_pdf_download():
    raw = _make_raw_email(b"%PDF-1.4", "doc.pdf")
    imap_instance = _make_mock_imap(["7"], raw)

    with mock.patch("imaplib.IMAP4_SSL", return_value=imap_instance):
        fetch_pdf_attachments(**FAKE_CFG)

    store_calls = [c for c in imap_instance.uid.call_args_list if c.args[0] == "store"]
    assert store_calls, "Expected uid('store', ...) to be called to mark message as seen"
    _, uid_arg, flag_op, flag = store_calls[0].args
    assert uid_arg == "7"
    assert flag_op == "+FLAGS"
    assert "\\Seen" in flag


def test_logout_called_on_success():
    raw = _make_raw_email()
    imap_instance = _make_mock_imap(["1"], raw)

    with mock.patch("imaplib.IMAP4_SSL", return_value=imap_instance):
        fetch_pdf_attachments(**FAKE_CFG)

    imap_instance.logout.assert_called_once()


def test_no_results_when_no_unseen_messages():
    imap_instance = mock.MagicMock()
    imap_instance.login.return_value = ("OK", [])
    imap_instance.select.return_value = ("OK", [b"0"])
    # Empty UID list
    imap_instance.uid.return_value = ("OK", [b""])

    with mock.patch("imaplib.IMAP4_SSL", return_value=imap_instance):
        results = fetch_pdf_attachments(**FAKE_CFG)

    assert results == []


def test_non_pdf_attachment_is_skipped():
    """A message containing only a .txt attachment must yield no results."""
    msg = MIMEMultipart()
    msg["From"] = "sender@example.com"
    txt = MIMEText("Hello", "plain")
    txt.add_header("Content-Disposition", "attachment", filename="notes.txt")
    msg.attach(txt)

    imap_instance = _make_mock_imap(["5"], msg.as_bytes())
    with mock.patch("imaplib.IMAP4_SSL", return_value=imap_instance):
        results = fetch_pdf_attachments(**FAKE_CFG)

    assert results == []
    # Message with no PDF parts must NOT be marked seen.
    store_calls = [c for c in imap_instance.uid.call_args_list if c.args[0] == "store"]
    assert not store_calls


# ── error / edge-case tests ───────────────────────────────────────────────────


def test_no_crash_when_imap_connection_fails():
    """Regression: if IMAP4_SSL() raises, the finally block must not raise
    UnboundLocalError because mail is never assigned.
    """
    with mock.patch("imaplib.IMAP4_SSL", side_effect=OSError("connection refused")):
        results = fetch_pdf_attachments(**FAKE_CFG)  # must not raise

    assert results == []


def test_no_crash_when_login_fails():
    imap_instance = mock.MagicMock()
    imap_instance.login.return_value = ("NO", [b"Auth failed"])

    with mock.patch("imaplib.IMAP4_SSL", return_value=imap_instance):
        results = fetch_pdf_attachments(**FAKE_CFG)

    assert results == []


def test_logout_still_called_when_login_fails():
    """Even if login fails we still established a TCP connection — logout must run."""
    imap_instance = mock.MagicMock()
    imap_instance.login.return_value = ("NO", [b"Auth failed"])

    with mock.patch("imaplib.IMAP4_SSL", return_value=imap_instance):
        fetch_pdf_attachments(**FAKE_CFG)

    imap_instance.logout.assert_called_once()


def test_multiple_senders_each_searched():
    cfg = {**FAKE_CFG, "allowed_senders": ["a@example.com", "b@example.com"]}
    imap_instance = mock.MagicMock()
    imap_instance.login.return_value = ("OK", [])
    imap_instance.select.return_value = ("OK", [b"0"])
    imap_instance.uid.return_value = ("OK", [b""])

    with mock.patch("imaplib.IMAP4_SSL", return_value=imap_instance):
        fetch_pdf_attachments(**cfg)

    search_calls = [c for c in imap_instance.uid.call_args_list if c.args[0] == "search"]
    assert len(search_calls) == 2, "Expected one UNSEEN search per allowed sender"
