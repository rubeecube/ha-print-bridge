"""Download a single PDF attachment from an IMAP server.

Usage:
    python3 pdf_downloader.py <uid> <filename>

Exits with code 0 and prints the saved file path on success.
Exits with code 1 and prints nothing on failure.
"""

import imaplib
import email as email_module
import logging
import os
import sys
import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = "/config"
IMAP_SERVER = "ssl0.ovh.net"
SAVE_PATH = "/media/print_queue"


def _get_secret(key: str) -> str:
    secrets_path = os.path.join(CONFIG_PATH, "secrets.yaml")
    with open(secrets_path) as f:
        secrets: dict = yaml.safe_load(f)
    value = secrets.get(key)
    if value is None:
        raise KeyError(f"Secret '{key}' not found in {secrets_path}")
    return str(value)


def download_attachment(target_uid: str, target_filename: str) -> str | None:
    """Connect to IMAP and save the named attachment; return its local path."""
    # FIX: mail is initialised to None so the finally block is always safe.
    mail: imaplib.IMAP4_SSL | None = None
    try:
        user = _get_secret("email_user")
        password = _get_secret("email_password")

        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(user, password)
        mail.select("INBOX")

        status, data = mail.uid("fetch", target_uid, "(RFC822)")
        if status != "OK" or not data or data[0] is None:
            logger.error("IMAP fetch failed for uid=%s: status=%s", target_uid, status)
            return None

        msg = email_module.message_from_bytes(data[0][1])  # type: ignore[index]

        for part in msg.walk():
            filename = part.get_filename()
            if filename == target_filename:
                os.makedirs(SAVE_PATH, exist_ok=True)
                filepath = os.path.join(SAVE_PATH, filename)
                payload = part.get_payload(decode=True)
                if payload is None:
                    logger.error("Empty payload for attachment '%s'", filename)
                    return None
                with open(filepath, "wb") as f:
                    f.write(payload)
                return filepath

        logger.error("Attachment '%s' not found in message uid=%s", target_filename, target_uid)
        return None

    except Exception:
        logger.exception("Error downloading attachment uid=%s filename=%s", target_uid, target_filename)
        return None

    finally:
        # FIX: guard against mail being unassigned if the connection itself failed.
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR, stream=sys.stderr)
    if len(sys.argv) != 3:
        print(
            f"Usage: {sys.argv[0]} <uid> <filename>",
            file=sys.stderr,
        )
        sys.exit(1)

    path = download_attachment(sys.argv[1], sys.argv[2])
    if path:
        print(path)
    else:
        sys.exit(1)
