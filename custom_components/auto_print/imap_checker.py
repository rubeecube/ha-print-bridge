"""IMAP filter preview — connects to a mailbox and lists emails that would be
printed given the current allowed-senders configuration.

All functions are blocking and must be called via hass.async_add_executor_job.
"""

from __future__ import annotations

import email as email_module
import imaplib
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Maximum number of messages inspected per sender search to avoid timeouts.
_MAX_PER_SENDER = 30
_MAX_TOTAL = 100


@dataclass
class EmailPreview:
    """Metadata for one email found during a filter-preview check."""

    uid: str
    subject: str
    sender: str
    date: str
    folder: str
    has_pdf: bool
    pdf_count: int
    matches_filter: bool   # True if sender is in allowed_senders (or list is empty)

    def as_dict(self) -> dict:
        return {
            "uid": self.uid,
            "subject": self.subject,
            "sender": self.sender,
            "date": self.date,
            "folder": self.folder,
            "has_pdf": self.has_pdf,
            "pdf_count": self.pdf_count,
            "matches_filter": self.matches_filter,
        }


def preview_mailbox(
    server: str,
    port: int,
    use_ssl: bool,
    username: str,
    password: str,
    folders: list[str],
    allowed_senders: list[str],
) -> list[EmailPreview]:
    """Connect to IMAP, search one or more folders, and return email previews.

    *folders* — IMAP folder names to inspect (e.g. ``["INBOX", "INBOX/Print"]``).
    *allowed_senders* — filter results to these addresses; empty = all senders.

    Returns a combined list sorted newest-first across all folders.
    """
    mail: imaplib.IMAP4 | imaplib.IMAP4_SSL | None = None
    results: list[EmailPreview] = []

    try:
        mail = imaplib.IMAP4_SSL(server, port) if use_ssl else imaplib.IMAP4(server, port)
        status, _ = mail.login(username, password)
        if status != "OK":
            logger.error("IMAP login failed for %s@%s", username, server)
            return results

        for folder in folders:
            folder_results = _search_folder(mail, folder, allowed_senders)
            results.extend(folder_results)

    except (imaplib.IMAP4.error, OSError):
        logger.exception("IMAP filter preview failed for %s@%s", username, server)
    finally:
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass

    # Sort newest-first (RFC2822 date strings sort approximately correctly lexicographically).
    results.sort(key=lambda e: e.date, reverse=True)
    return results


def _search_folder(
    mail: imaplib.IMAP4 | imaplib.IMAP4_SSL,
    folder: str,
    allowed_senders: list[str],
) -> list[EmailPreview]:
    """Select *folder*, search for matching messages, return previews."""
    folder_results: list[EmailPreview] = []

    status, _ = mail.select(folder, readonly=True)
    if status != "OK":
        logger.warning("Cannot select IMAP folder '%s' — skipping", folder)
        return folder_results

    uid_set: set[str] = set()
    if allowed_senders:
        for sender in allowed_senders:
            status, data = mail.uid("search", None, f'FROM "{sender}"')
            if status == "OK" and data and data[0]:
                uids = data[0].decode().split()
                uid_set.update(uids[-_MAX_PER_SENDER:])
    else:
        status, data = mail.uid("search", None, "ALL")
        if status == "OK" and data and data[0]:
            all_uids = data[0].decode().split()
            uid_set.update(all_uids[-_MAX_TOTAL:])

    for uid in sorted(uid_set):
        preview = _build_preview(mail, uid, allowed_senders, folder)
        if preview:
            folder_results.append(preview)

    return folder_results


def _build_preview(
    mail: imaplib.IMAP4 | imaplib.IMAP4_SSL,
    uid: str,
    allowed_senders: list[str],
    folder: str = "",
) -> EmailPreview | None:
    """Fetch headers + body structure for one UID and build an EmailPreview."""
    try:
        # Fetch only headers — efficient even for large attachments.
        status, hdata = mail.uid("fetch", uid, "(RFC822.HEADER)")
        if status != "OK" or not hdata or hdata[0] is None:
            return None

        raw_headers = hdata[0][1]  # type: ignore[index]
        if not isinstance(raw_headers, bytes):
            return None

        msg = email_module.message_from_bytes(raw_headers)
        subject = _decode_header(msg.get("subject", "(no subject)"))
        sender_hdr = msg.get("from", "")
        date_hdr = msg.get("date", "")
        sender_email = _extract_address(sender_hdr).lower()

        # Determine filter match.
        matches = (
            not allowed_senders
            or any(sender_email == s.lower() for s in allowed_senders)
        )

        # Fetch BODYSTRUCTURE to count PDF parts without downloading payloads.
        has_pdf = False
        pdf_count = 0
        status2, bdata = mail.uid("fetch", uid, "(BODYSTRUCTURE)")
        if status2 == "OK" and bdata and bdata[0] is not None:
            body_raw = bdata[0][1] if isinstance(bdata[0], tuple) else bdata[0]
            body_str = (
                body_raw.decode(errors="replace")
                if isinstance(body_raw, bytes)
                else str(bdata)
            ).lower()
            pdf_count = body_str.count("application/pdf")
            has_pdf = pdf_count > 0

        return EmailPreview(
            uid=uid,
            subject=subject,
            sender=sender_hdr,
            date=date_hdr,
            folder=folder,
            has_pdf=has_pdf,
            pdf_count=pdf_count,
            matches_filter=matches,
        )

    except Exception:
        logger.warning("Could not build preview for uid=%s", uid, exc_info=True)
        return None


def _extract_address(header: str) -> str:
    """Return the bare email address from a 'Name <addr>' header value."""
    if "<" in header and ">" in header:
        return header.split("<")[1].split(">")[0].strip()
    return header.strip()


def _decode_header(value: str) -> str:
    """Decode RFC2047-encoded header value to plain text."""
    try:
        from email.header import decode_header
        parts = decode_header(value)
        return "".join(
            (
                p.decode(enc or "utf-8", errors="replace")
                if isinstance(p, bytes)
                else p
            )
            for p, enc in parts
        )
    except Exception:
        return value
