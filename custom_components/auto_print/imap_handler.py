"""IMAP attachment retrieval for the Auto Print integration.

All public functions are pure I/O wrappers with no HA dependencies — they are
intended to be called via ``hass.async_add_executor_job``.
"""

from __future__ import annotations

import email as email_module
import imaplib
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class PdfAttachment:
    """Metadata + raw bytes for a single PDF attachment."""

    uid: str
    filename: str
    data: bytes


def fetch_pdf_attachments(
    server: str,
    port: int,
    use_ssl: bool,
    username: str,
    password: str,
    folder: str,
    allowed_senders: list[str],
) -> list[PdfAttachment]:
    """Connect to IMAP and return all unread PDFs from allowed senders.

    Messages that yield at least one PDF attachment are marked as SEEN so they
    are not reprocessed on the next poll.

    This function is blocking and must be run in an executor thread.
    """
    mail: imaplib.IMAP4 | imaplib.IMAP4_SSL | None = None
    results: list[PdfAttachment] = []

    try:
        mail = imaplib.IMAP4_SSL(server, port) if use_ssl else imaplib.IMAP4(server, port)
        status, _ = mail.login(username, password)
        if status != "OK":
            logger.error("IMAP login failed for %s@%s", username, server)
            return results

        status, _ = mail.select(folder)
        if status != "OK":
            logger.error("Cannot select IMAP folder '%s'", folder)
            return results

        for sender in allowed_senders:
            uids = _search_unseen_from(mail, sender)
            for uid in uids:
                attachments = _fetch_pdf_parts(mail, uid)
                if attachments:
                    results.extend(attachments)
                    _mark_seen(mail, uid)

    except (imaplib.IMAP4.error, OSError):
        logger.exception("IMAP error while fetching attachments from %s", server)
    finally:
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass

    return results


def _search_unseen_from(mail: imaplib.IMAP4 | imaplib.IMAP4_SSL, sender: str) -> list[str]:
    """Return UIDs of unseen messages from *sender*."""
    status, data = mail.uid("search", None, f'(UNSEEN FROM "{sender}")')
    if status != "OK" or not data or not data[0]:
        return []
    return data[0].decode().split()


def _fetch_pdf_parts(
    mail: imaplib.IMAP4 | imaplib.IMAP4_SSL, uid: str
) -> list[PdfAttachment]:
    """Fetch a message by UID and return all PDF attachments found in it."""
    status, data = mail.uid("fetch", uid, "(RFC822)")
    if status != "OK" or not data or data[0] is None:
        logger.warning("Could not fetch message uid=%s", uid)
        return []

    raw_message = data[0][1]  # type: ignore[index]
    if not isinstance(raw_message, bytes):
        return []

    msg = email_module.message_from_bytes(raw_message)
    attachments: list[PdfAttachment] = []

    for part in msg.walk():
        content_type = part.get_content_type()
        filename = part.get_filename()

        if content_type != "application/pdf" or not filename:
            continue

        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes) or not payload:
            logger.warning("Empty PDF payload for '%s' in uid=%s", filename, uid)
            continue

        attachments.append(PdfAttachment(uid=uid, filename=filename, data=payload))

    return attachments


def _mark_seen(mail: imaplib.IMAP4 | imaplib.IMAP4_SSL, uid: str) -> None:
    """Add the \\Seen flag to a message so it is not reprocessed."""
    try:
        mail.uid("store", uid, "+FLAGS", "\\Seen")
    except Exception:
        logger.warning("Could not mark uid=%s as seen", uid)
