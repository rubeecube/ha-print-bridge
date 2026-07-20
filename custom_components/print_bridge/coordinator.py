"""DataUpdateCoordinator for the Print Bridge integration.

Responsibilities:
  - Listen for imap_content events fired by HA's built-in IMAP integration.
  - Print supported files dropped into the configured queue folder.
  - Convert supported non-PDF inputs to internal PDF.
  - Optionally reorder pages for booklet printing.
  - Send the print job to CUPS via a raw IPP/2.0 request (aiohttp).
  - Fire print_bridge_job_completed events → HA Logbook audit trail.
  - Periodically check printer reachability and process queued files.
"""

from __future__ import annotations

import asyncio
import base64
from email.message import EmailMessage
import io
import logging
import os
import re
import secrets
import smtplib
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from email.utils import parseaddr
from functools import partial
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import TemplateError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.template import Template
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .booklet_maker import create_booklet
from .document_converter import (
    AttachmentConversionSummary,
    DocumentConversionError,
    UnsupportedDocumentError,
    convert_document_to_pdf,
    extension_for_document,
    is_printable_attachment,
    merge_pdf_documents,
    printable_type,
)
from .const import (
    CONF_ALLOWED_SENDERS,
    CONF_AUTO_DELETE,
    CONF_BOOKLET_PATTERNS,
    CONF_COLLATE,
    CONF_CUPS_URL,
    CONF_DIRECT_PRINTER_URL,
    CONF_DEFAULT_PRINT_TYPE,
    CONF_DUPLEX_MODE,
    CONF_EMAIL_ACTION,
    CONF_EMAIL_ARCHIVE_FOLDER,
    CONF_FOLDER_FILTER,
    CONF_NOTIFY_ON_FAILURE,
    CONF_NOTIFY_ON_SUCCESS,
    CONF_PRINTER_NAME,
    CONF_PRINT_TYPES,
    CONF_QUEUE_FOLDER,
    CONF_RASTER_DPI,
    CONF_REVERSE_ORDER,
    CONF_SIGNAL_ACCOUNT,
    CONF_SIGNAL_ALLOWED_GROUP_IDS,
    CONF_SIGNAL_ALLOWED_SENDERS,
    CONF_SIGNAL_CONFIRMATION_MODE,
    CONF_SIGNAL_CONFIRMATION_TTL_HOURS,
    CONF_SIGNAL_ENABLED,
    CONF_SIGNAL_MODULE_ID,
    CONF_SIGNAL_REST_URL,
    CONF_AUTO_PRINT_ENABLED,
    CONF_SELECTED_IMAP_ENTRY_ID,
    CONF_SELECTED_PRINTER_ENTRY_ID,
    CONF_SCHEDULE_ENABLED,
    CONF_SCHEDULE_DAYS,
    CONF_SCHEDULE_END,
    CONF_SCHEDULE_START,
    CONF_SCHEDULE_TEMPLATE,
    CONF_STATUS_REPLY_ENABLED,
    CONF_STATUS_REPLY_NOTIFY_SERVICE,
    DEFAULT_AUTO_DELETE,
    DEFAULT_COLLATE,
    DEFAULT_PRINT_TYPE,
    DEFAULT_PRINT_TYPES,
    DEFAULT_DUPLEX_MODE,
    DEFAULT_EMAIL_ACTION,
    DEFAULT_EMAIL_ARCHIVE_FOLDER,
    DEFAULT_NOTIFY_ON_FAILURE,
    DEFAULT_NOTIFY_ON_SUCCESS,
    DEFAULT_QUEUE_FOLDER,
    DEFAULT_RASTER_DPI,
    DEFAULT_REVERSE_ORDER,
    DEFAULT_SIGNAL_ACCOUNT,
    DEFAULT_SIGNAL_ALLOWED_GROUP_IDS,
    DEFAULT_SIGNAL_ALLOWED_SENDERS,
    DEFAULT_SIGNAL_CONFIRMATION_MODE,
    DEFAULT_SIGNAL_CONFIRMATION_TTL_HOURS,
    DEFAULT_SIGNAL_ENABLED,
    DEFAULT_SIGNAL_MODULE_ID,
    DEFAULT_SIGNAL_REST_URL,
    DEFAULT_AUTO_PRINT_ENABLED,
    DEFAULT_SCHEDULE_ENABLED,
    DEFAULT_SCHEDULE_DAYS,
    DEFAULT_SCHEDULE_END,
    DEFAULT_SCHEDULE_START,
    DEFAULT_SCHEDULE_TEMPLATE,
    DEFAULT_STATUS_REPLY_ENABLED,
    DEFAULT_STATUS_REPLY_NOTIFY_SERVICE,
    DOMAIN,
    EVENT_JOB_COMPLETED,
    SCHEDULE_DAYS,
    SIGNAL_REST_COMPONENTS,
    SIGNAL_REST_INTEGRATION_DOMAIN,
)
from .imap_checker import EmailPreview, preview_mailbox
from .mail_params import MailPrintParameters, parse_mail_print_parameters
from .page_order import reverse_pdf_pages
from .print_profiles import (
    PrintProfile,
    merge_print_parameters,
    normalize_print_type,
    parse_print_profiles,
    resolve_default_print_type,
)
from .print_handler import (
    build_ipp_packet,
    build_get_printer_attributes_packet,
    cups_printer_uri,
    determine_sides,
    http_url_to_ipp_uri,
    ipp_response_succeeded,
    is_booklet_job,
    parse_ipp_attributes,
    parse_ipp_response_status,
    sanitize_ipp_job_name,
)
from .raster_converter import convert_pdf_to_jpeg, convert_pdf_to_pwg_raster
from .signal_client import (
    SignalAttachment,
    SignalMessage,
    SignalRestClient,
    parse_signal_messages,
)

logger = logging.getLogger(__name__)

_STATUS_INTERVAL = timedelta(minutes=5)
_CAPABILITIES_TTL = timedelta(hours=1)
_PRINT_JOB_TIMEOUT_SECONDS = 300
_PRINTER_BUSY_POLL_INTERVAL_SECONDS = 15
_PRINTER_BUSY_QUEUE_MAX_ATTEMPTS = 120
_PRINTER_BUSY_STATUS_CODES = {0x0506, 0x0507}
_SIGNAL_POLL_INTERVAL_SECONDS = 30
_QUEUE_FILE_STABLE_SECONDS = 2
_QUEUE_IGNORED_SUFFIXES = (".tmp", ".part", ".crdownload")
_TEXT_FILTER_SPLIT_RE = re.compile(r"[,;\n]+")
_EXTENSION_FILTER_SPLIT_RE = re.compile(r"[\s,;]+")
_FILTER_SKIP_MARKERS = (
    "does not match filter",
    "matches ignored filename filter",
    "has ignored extension",
    "is not in allowed extensions",
)
_SCHEDULE_DAY_ALIASES = {
    "mon": "mon",
    "monday": "mon",
    "1": "mon",
    "tue": "tue",
    "tues": "tue",
    "tuesday": "tue",
    "2": "tue",
    "wed": "wed",
    "wednesday": "wed",
    "3": "wed",
    "thu": "thu",
    "thur": "thu",
    "thurs": "thu",
    "thursday": "thu",
    "4": "thu",
    "fri": "fri",
    "friday": "fri",
    "5": "fri",
    "sat": "sat",
    "saturday": "sat",
    "6": "sat",
    "sun": "sun",
    "sunday": "sun",
    "7": "sun",
}
_FALSE_TEMPLATE_VALUES = {"", "0", "false", "no", "off", "none", "unknown", "unavailable"}
_ORIENTATION_ENUMS = {"portrait": 3, "landscape": 4}
_MEDIA_BY_POINTS = (
    ("iso_a4_210x297mm", 595, 842),
    ("na_letter_8.5x11in", 612, 792),
    ("na_legal_8.5x14in", 612, 1008),
)
_SIGNAL_CONFIRM_RE = re.compile(
    r"(?i)^\s*(?:print|confirm|yes)\s+(?P<token>[a-z0-9_-]{4,64})\b(?P<settings>.*)$"
)
_SIGNAL_CANCEL_RE = re.compile(
    r"(?i)^\s*(?:cancel|stop|no)\s+(?P<token>[a-z0-9_-]{4,64})\b"
)
_SIGNAL_SET_RE = re.compile(
    r"(?i)^\s*(?:set|settings)\s+(?P<token>[a-z0-9_-]{4,64})\b(?P<settings>.*)$"
)
_SIGNAL_OPTION_KEYS = {
    CONF_SIGNAL_ENABLED,
    CONF_SIGNAL_MODULE_ID,
    CONF_SIGNAL_REST_URL,
    CONF_SIGNAL_ACCOUNT,
    CONF_SIGNAL_ALLOWED_SENDERS,
    CONF_SIGNAL_ALLOWED_GROUP_IDS,
    CONF_SIGNAL_CONFIRMATION_MODE,
    CONF_SIGNAL_CONFIRMATION_TTL_HOURS,
}


def _decode_mime_filename(value: str) -> str:
    """Decode and clean an RFC 2047 MIME-encoded filename.

    Email clients encode non-ASCII filenames in attachment headers, e.g.:
    ``=?utf-8?B?QXUgUHVpdHM...?= =?utf-8?Q?m_5786_A4.pdf?=``
    Falls back to the raw string if decoding fails.
    """
    if "=?" not in value:
        return sanitize_ipp_job_name(value)
    try:
        from email.header import decode_header as _dh
        parts = _dh(value)
        decoded = "".join(
            p.decode(enc or "utf-8", errors="replace") if isinstance(p, bytes) else p
            for p, enc in parts
        )
        return sanitize_ipp_job_name(decoded)
    except Exception:
        return sanitize_ipp_job_name(value)


def _describe_exception(exc: BaseException) -> str:
    """Return useful text even for exceptions whose ``str()`` is empty."""
    message = str(exc).strip()
    if message:
        return f"{type(exc).__name__}: {message}"
    return type(exc).__name__


def _normalise_email_address(value: str) -> str:
    """Return a lower-case bare email address from an IMAP sender string."""
    _name, address = parseaddr(value or "")
    return (address or value or "").strip().lower()


def _normalise_signal_identity(value: str) -> str:
    """Return a stable comparison key for Signal numbers and UUIDs."""
    return str(value or "").strip().lower()


def _normalise_signal_group_id(value: str) -> str:
    """Return group IDs in the signal-cli-rest-api recipient format."""
    group_id = str(value or "").strip()
    if not group_id:
        return ""
    if group_id.lower().startswith("group."):
        return "group." + group_id.split(".", 1)[1]
    return f"group.{group_id}"


def _normalise_signal_group_name(value: str) -> str:
    """Return a stable comparison key for Signal display group names."""
    return " ".join(str(value or "").strip().split()).casefold()


def _looks_like_signal_group_id(value: str) -> bool:
    """Return True for explicit Signal group recipient IDs."""
    return str(value or "").strip().casefold().startswith("group.")


def _split_notify_service(value: str) -> tuple[str, str]:
    """Return (domain, service) for a notify service reference."""
    service_ref = value.strip()
    if "." in service_ref:
        domain, service = service_ref.split(".", 1)
        return domain.strip(), service.strip()
    return "notify", service_ref


def _send_status_email_smtp(
    *,
    server: str,
    port: int,
    use_ssl: bool,
    username: str,
    password: str,
    sender: str,
    recipient: str,
    subject: str,
    message: str,
) -> None:
    """Send a plain-text status email through the IMAP account's SMTP server."""
    email = EmailMessage()
    email["From"] = sender
    email["To"] = recipient
    email["Subject"] = subject
    email.set_content(message)

    if use_ssl:
        with smtplib.SMTP_SSL(server, port, timeout=30) as smtp:
            smtp.login(username, password)
            smtp.send_message(email)
        return

    with smtplib.SMTP(server, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(username, password)
        smtp.send_message(email)


def _part_filename(part_info: dict[str, Any]) -> str:
    """Return the decoded filename declared by an IMAP part, if any."""
    filename = part_info.get("filename") or part_info.get("file_name")
    if not filename:
        return ""
    return _decode_mime_filename(str(filename))


def _attachment_filename(part_key: str, part_info: dict[str, Any]) -> str:
    """Return a cleaned attachment filename from IMAP part metadata."""
    return _part_filename(part_info) or f"attachment_{part_key}"


def _is_printable_part(part_key: str, part_info: dict[str, Any]) -> bool:
    """Return True for supported or known unsupported printable attachments."""
    filename = _part_filename(part_info)
    if not filename:
        return False
    return is_printable_attachment(
        filename,
        str(part_info.get("content_type", "")),
        include_unsupported=True,
    )


def _normalise_text_filters(value: Any) -> tuple[str, ...]:
    """Return case-insensitive filename substring filters."""
    if not value:
        return ()
    if isinstance(value, str):
        raw_values = _TEXT_FILTER_SPLIT_RE.split(value)
    elif isinstance(value, (list, tuple, set)):
        raw_values = [str(item) for item in value]
    else:
        raw_values = [str(value)]

    filters: list[str] = []
    for raw_value in raw_values:
        item = raw_value.strip().lower()
        if item and item not in filters:
            filters.append(item)
    return tuple(filters)


def _normalise_extension_filters(value: Any) -> tuple[str, ...]:
    """Return normalized extensions, accepting pdf, .pdf, or *.pdf tokens."""
    if not value:
        return ()
    if isinstance(value, str):
        raw_values = _EXTENSION_FILTER_SPLIT_RE.split(value)
    elif isinstance(value, (list, tuple, set)):
        raw_values = [str(item) for item in value]
    else:
        raw_values = [str(value)]

    extensions: list[str] = []
    for raw_value in raw_values:
        token = raw_value.strip().lower()
        if not token:
            continue
        if token.startswith("*."):
            token = token[2:]
        token = token.removeprefix(".")
        if not token:
            continue
        extension = f".{token}"
        if extension not in extensions:
            extensions.append(extension)
    return tuple(extensions)


def _filter_skip_reason(
    filename: str,
    content_type: str,
    *,
    attachment_filter: str | None,
    attachment_ignore_filter: Any = None,
    allowed_extensions: Any = None,
    ignored_extensions: Any = None,
) -> str | None:
    """Return a skip reason when attachment filters exclude a filename."""
    lower_filename = filename.lower()
    if attachment_filter and attachment_filter.strip().lower() not in lower_filename:
        return f"does not match filter '{attachment_filter}'"

    for text_filter in _normalise_text_filters(attachment_ignore_filter):
        if text_filter in lower_filename:
            return f"matches ignored filename filter '{text_filter}'"

    extension = extension_for_document(filename, content_type)
    ignored_exts = _normalise_extension_filters(ignored_extensions)
    if extension and extension in ignored_exts:
        return f"has ignored extension '{extension}'"

    allowed_exts = _normalise_extension_filters(allowed_extensions)
    if allowed_exts and extension not in allowed_exts:
        displayed = extension or "unknown"
        return f"extension '{displayed}' is not in allowed extensions"

    return None


def _is_filter_skip(reason: str) -> bool:
    return any(marker in reason for marker in _FILTER_SKIP_MARKERS)


def _decode_part_payload(response: dict[str, Any]) -> bytes:
    """Decode an IMAP fetch_part service response into raw bytes."""
    raw = response["part_data"]
    encoding = str(response.get("content_transfer_encoding", "base64")).lower()
    if encoding == "base64":
        return base64.b64decode(raw)
    if isinstance(raw, bytes):
        return raw
    return str(raw).encode("latin-1")


def _normalise_schedule_days(value: Any) -> list[str]:
    """Return canonical weekday tokens from stored options."""
    if value is None:
        return []
    if isinstance(value, str):
        raw_days = _re_split_days(value)
    elif isinstance(value, (list, tuple, set)):
        raw_days = [str(day).strip().lower() for day in value if str(day).strip()]
    else:
        return []

    days: list[str] = []
    for raw_day in raw_days:
        day = _SCHEDULE_DAY_ALIASES.get(raw_day)
        if day and day not in days:
            days.append(day)
    return days


def _re_split_days(value: str) -> list[str]:
    return [part.strip().lower() for part in re.split(r"[\s,;]+", value) if part.strip()]


def _template_result_is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in _FALSE_TEMPLATE_VALUES
    return bool(value)


def _is_queue_file_name(name: str) -> bool:
    """Return True when a queue-folder filename should be considered."""
    lower = name.lower()
    if name.startswith(".") or lower.endswith(_QUEUE_IGNORED_SUFFIXES):
        return False
    return is_printable_attachment(name, "", include_unsupported=False)


def _first_or_none(values: list[str]) -> str | None:
    return values[0] if values else None


def _first_bool(values: list[str]) -> bool | None:
    value = _first_or_none(values)
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    return None


def _first_int(values: list[str]) -> int | None:
    value = _first_or_none(values)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _orientation_for_job(booklet: bool, orientation: str | None) -> str | None:
    """Return the effective job orientation keyword."""
    if booklet:
        return "landscape"
    if orientation in _ORIENTATION_ENUMS:
        return orientation
    return None


def _media_for_pdf_page_size(pdf_data: bytes) -> str | None:
    """Return an IPP media keyword when the first PDF page matches a common size."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(pdf_data))
        if not reader.pages:
            return None
        page = reader.pages[0]
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
    except Exception:
        return None

    for media, expected_width, expected_height in _MEDIA_BY_POINTS:
        if (
            abs(width - expected_width) <= 4
            and abs(height - expected_height) <= 4
        ) or (
            abs(width - expected_height) <= 4
            and abs(height - expected_width) <= 4
        ):
            return media
    return None


def _normalise_raster_dpi(value: Any) -> int:
    try:
        dpi = int(value)
    except (TypeError, ValueError):
        return DEFAULT_RASTER_DPI
    return max(72, min(600, dpi))


@dataclass
class PrintJobResult:
    """Outcome of a single print attempt, including audit metadata."""

    filename: str
    success: bool
    error: str | None = None
    sender: str | None = None
    duplex: str | None = None
    booklet: bool = False
    copies: int | None = None
    collate: bool | None = None
    orientation: str | None = None
    media: str | None = None
    raster_dpi: int | None = None
    reverse_order: bool | None = None
    reverse_order_applied: bool = False
    source_format: str | None = None
    converted_format: str | None = None
    attachments: list[str] = field(default_factory=list)
    skipped_attachments: list[str] = field(default_factory=list)
    merged_attachment_count: int | None = None
    sides: str | None = None
    document_format: str | None = None
    status_code: str | None = None
    status: str | None = None
    status_reply_recipient: str | None = None
    status_reply_subject: str | None = None
    status_reply_message: str | None = None
    status_reply_delivery: str | None = None
    source: str | None = None
    signal_job_id: str | None = None
    signal_group_id: str | None = None
    signal_group_name: str | None = None
    timestamp: str = field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds")
    )
    # IMAP identifiers needed to re-fetch and retry this job
    imap_entry_id: str | None = None
    imap_uid: str | None = None
    imap_part_key: str | None = None

    @property
    def can_retry(self) -> bool:
        """True if enough IMAP metadata is stored to re-fetch this attachment."""
        return bool(self.imap_entry_id and self.imap_uid and self.imap_part_key)


@dataclass
class FilterPreviewResult:
    """Outcome of a filter-preview check."""

    checked_at: str
    imap_account: str               # username@server shown in the UI
    imap_entry_id: str               # HA IMAP config entry used for fetch/print
    total_found: int                # total messages inspected
    matching: int                   # messages matching the sender filter
    with_pdf: int                   # matching messages that have a PDF attachment
    with_printable: int | None = None  # matching messages with printable attachments
    emails: list[EmailPreview] = field(default_factory=list)


@dataclass
class PrinterCapabilities:
    """IPP capability snapshot for the configured printer endpoint."""

    checked_at: str
    endpoint: str
    printer_uri: str
    document_formats: list[str] = field(default_factory=list)
    document_format_default: str | None = None
    pdf_versions: list[str] = field(default_factory=list)
    pwg_raster_types: list[str] = field(default_factory=list)
    pwg_raster_resolutions: list[str] = field(default_factory=list)
    pwg_sheet_back: str | None = None
    sides_supported: list[str] = field(default_factory=list)
    printer_is_accepting_jobs: bool | None = None
    printer_state: str | None = None
    printer_state_reasons: list[str] = field(default_factory=list)
    queued_job_count: int | None = None
    selected_document_format: str | None = None
    conversion_required: bool = False
    error: str | None = None

    @property
    def pdf_supported(self) -> bool:
        return "application/pdf" in self.document_formats

    @property
    def pwg_supported(self) -> bool:
        return "image/pwg-raster" in self.document_formats

    def as_dict(self) -> dict:
        return {
            "checked_at": self.checked_at,
            "endpoint": self.endpoint,
            "printer_uri": self.printer_uri,
            "document_formats": list(self.document_formats),
            "document_format_default": self.document_format_default,
            "pdf_versions": list(self.pdf_versions),
            "pwg_raster_types": list(self.pwg_raster_types),
            "pwg_raster_resolutions": list(self.pwg_raster_resolutions),
            "pwg_sheet_back": self.pwg_sheet_back,
            "sides_supported": list(self.sides_supported),
            "printer_is_accepting_jobs": self.printer_is_accepting_jobs,
            "printer_state": self.printer_state,
            "printer_state_reasons": list(self.printer_state_reasons),
            "queued_job_count": self.queued_job_count,
            "selected_document_format": self.selected_document_format,
            "conversion_required": self.conversion_required,
            "pdf_supported": self.pdf_supported,
            "pwg_supported": self.pwg_supported,
            "error": self.error,
        }


@dataclass
class PendingJob:
    """A print job held in the schedule queue (outside allowed print hours)."""

    entry_id: str
    uid: str
    part_key: str | None
    filename: str
    sender: str | None = None
    duplex_override: str | None = None
    booklet_override: bool | None = None
    copies: int | None = None
    collate: bool | None = None
    orientation: str | None = None
    media: str | None = None
    raster_dpi: int | None = None
    reverse_order: bool | None = None
    mail_text: str = ""
    mail_subject: str = ""
    mail_params: MailPrintParameters = field(default_factory=MailPrintParameters)
    message_level: bool = False
    queued_at: str = field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds")
    )

    def as_dict(self) -> dict:
        return {
            "filename": self.filename,
            "sender": self.sender,
            "queued_at": self.queued_at,
            "uid": self.uid,
            "part_key": self.part_key,
            "duplex": self.duplex_override,
            "booklet": self.booklet_override,
            "copies": self.copies,
            "collate": self.collate,
            "orientation": self.orientation,
            "media": self.media,
            "raster_dpi": self.raster_dpi,
            "reverse_order": self.reverse_order,
            "message_level": self.message_level,
        }


@dataclass
class BusyPrintJob:
    """A print payload waiting for the printer to stop reporting busy."""

    filename: str
    pdf_data: bytes
    duplex_mode: str
    booklet: bool
    copies: int | None = None
    collate: bool | None = None
    orientation: str | None = None
    media: str | None = None
    raster_dpi: int | None = None
    reverse_order: bool | None = None
    attempts: int = 0
    queued_at: str = field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds")
    )

    def as_dict(self) -> dict:
        return {
            "filename": self.filename,
            "queued_at": self.queued_at,
            "queue_type": "printer_busy",
            "duplex": self.duplex_mode,
            "booklet": self.booklet,
            "copies": self.copies,
            "collate": self.collate,
            "orientation": self.orientation,
            "media": self.media,
            "raster_dpi": self.raster_dpi,
            "reverse_order": self.reverse_order,
            "attempts": self.attempts,
        }


@dataclass
class SignalPendingJob:
    """A received Signal document job waiting for explicit confirmation."""

    job_id: str
    token: str
    message_id: str
    sender: str
    sender_name: str | None
    sender_uuid: str | None
    group_id: str | None
    group_name: str | None
    text: str
    attachments: tuple[SignalAttachment, ...]
    print_type: str = DEFAULT_PRINT_TYPE
    skipped_attachments: list[str] = field(default_factory=list)
    duplex_override: str | None = None
    booklet_override: bool | None = None
    copies: int | None = None
    collate: bool | None = None
    orientation: str | None = None
    media: str | None = None
    raster_dpi: int | None = None
    reverse_order: bool | None = None
    effective_settings: dict[str, Any] = field(default_factory=dict)
    received_at: str = field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds")
    )
    expires_at: str = ""

    @property
    def filename(self) -> str:
        if len(self.attachments) == 1:
            return self.attachments[0].filename
        return f"signal_{self.message_id[:24]}_attachments.pdf"

    @property
    def reply_recipient(self) -> str | None:
        return self.group_id or self.sender or self.sender_uuid

    def as_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "token": self.token,
            "filename": self.filename,
            "sender": self.sender,
            "sender_name": self.sender_name,
            "sender_uuid": self.sender_uuid,
            "group_id": self.group_id,
            "group_name": self.group_name,
            "message_id": self.message_id,
            "received_at": self.received_at,
            "expires_at": self.expires_at,
            "print_type": self.print_type,
            "duplex": self.duplex_override,
            "booklet": self.booklet_override,
            "copies": self.copies,
            "collate": self.collate,
            "orientation": self.orientation,
            "media": self.media,
            "raster_dpi": self.raster_dpi,
            "reverse_order": self.reverse_order,
            "effective_settings": dict(self.effective_settings),
            "attachments": [
                {
                    "filename": item.filename,
                    "content_type": item.content_type,
                    "attachment_id": item.attachment_id,
                }
                for item in self.attachments
            ],
            "skipped_attachments": list(self.skipped_attachments),
        }


@dataclass(frozen=True)
class QueueFolderFile:
    """A stable supported file found in the configured queue folder."""

    path: str
    filename: str
    mtime: float


@dataclass
class AutoPrintData:
    """Snapshot of integration state exposed to entities."""

    queue_depth: int = 0
    printer_online: bool = False
    last_job: PrintJobResult | None = None
    job_history: list[PrintJobResult] = field(default_factory=list)
    total_jobs_sent: int = 0
    filter_preview: FilterPreviewResult | None = None
    printer_capabilities: PrinterCapabilities | None = None
    pending_jobs: list[PendingJob] = field(default_factory=list)
    printer_busy_jobs: list[BusyPrintJob] = field(default_factory=list)
    signal_pending_jobs: list[SignalPendingJob] = field(default_factory=list)
    signal_groups: list[dict[str, Any]] = field(default_factory=list)
    signal_groups_checked_at: str | None = None


class AutoPrintCoordinator(DataUpdateCoordinator[AutoPrintData]):
    """Manages event-driven printing and periodic printer-status checks."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            logger,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=_STATUS_INTERVAL,
        )
        self._entry = entry
        self._job_history: list[PrintJobResult] = []
        self._total_jobs_sent: int = 0
        self._filter_preview: FilterPreviewResult | None = None
        self._printer_capabilities: PrinterCapabilities | None = None
        self._capabilities_checked_at: datetime | None = None
        self._pending_jobs: list[PendingJob] = []
        self._printer_busy_jobs: list[BusyPrintJob] = []
        self._printer_busy_queue_task: asyncio.Task | None = None
        self._signal_receiver_task: asyncio.Task | None = None
        self._processing_queue_files: set[str] = set()
        self._processed_queue_files: dict[str, float] = {}
        self._signal_pending_jobs: list[SignalPendingJob] = []
        self._signal_seen_keys: set[str] = set()
        self._signal_groups: list[dict[str, Any]] = []
        self._signal_groups_checked_at: str | None = None
        self._signal_group_resolution_error: str | None = None
        self._signal_rest_detected: bool = False
        self._signal_rest_checked_at: str | None = None
        self._signal_rest_error: str | None = None
        self._last_schedule_state: bool | None = None  # track open↔closed transitions
        # Deduplication: maps "uid:part_key" → datetime of last print to prevent
        # multiple blueprints firing on the same attachment from double-printing.
        self._recently_printed: dict[str, datetime] = {}

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def _cups_url(self) -> str:
        return self._entry.data.get(CONF_CUPS_URL, "").rstrip("/")

    @property
    def _printer_name(self) -> str:
        return self._entry.data.get(CONF_PRINTER_NAME, "")

    @property
    def _direct_printer_url(self) -> str:
        """Return the direct IPP URL if configured (empty string = CUPS mode)."""
        return self._entry.data.get(CONF_DIRECT_PRINTER_URL, "").strip()

    @property
    def _is_direct_mode(self) -> bool:
        """True when printing directly to the printer without CUPS."""
        return bool(self._direct_printer_url)

    @property
    def _ipp_endpoint(self) -> str:
        """HTTP(S) URL to POST the IPP Print-Job request to."""
        if self._is_direct_mode:
            url = self._direct_printer_url
            # Normalise ipp:// → http:// for the aiohttp POST call
            if url.startswith("ipp://"):
                return "http://" + url[len("ipp://"):]
            if url.startswith("ipps://"):
                return "https://" + url[len("ipps://"):]
            return url
        return f"{self._cups_url}/printers/{self._printer_name}"

    @property
    def _printer_uri(self) -> str:
        """IPP printer-uri attribute value for the Print-Job request."""
        if self._is_direct_mode:
            return http_url_to_ipp_uri(self._direct_printer_url)
        return cups_printer_uri(self._cups_url, self._printer_name)

    @property
    def _duplex_mode(self) -> str:
        return self._entry.options.get(CONF_DUPLEX_MODE, DEFAULT_DUPLEX_MODE)

    @property
    def _booklet_patterns(self) -> list[str]:
        return self._entry.options.get(CONF_BOOKLET_PATTERNS, [])

    @property
    def _auto_delete(self) -> bool:
        return self._entry.options.get(CONF_AUTO_DELETE, DEFAULT_AUTO_DELETE)

    @property
    def _queue_folder(self) -> str:
        return self._entry.options.get(CONF_QUEUE_FOLDER, DEFAULT_QUEUE_FOLDER)

    @property
    def _raster_dpi(self) -> int:
        """DPI used when direct IPP printing requires raster conversion."""
        return _normalise_raster_dpi(
            self._entry.options.get(CONF_RASTER_DPI, DEFAULT_RASTER_DPI)
        )

    @property
    def _reverse_order(self) -> bool:
        """True when one-sided jobs should print last page first by default."""
        return bool(
            self._entry.options.get(CONF_REVERSE_ORDER, DEFAULT_REVERSE_ORDER)
        )

    def _resolve_reverse_order(self, reverse_order: bool | None) -> bool:
        return self._reverse_order if reverse_order is None else bool(reverse_order)

    @property
    def _collate(self) -> bool:
        """True when multi-copy jobs should be collated by default."""
        return bool(self._entry.options.get(CONF_COLLATE, DEFAULT_COLLATE))

    def _resolve_collate(self, collate: bool | None) -> bool:
        return self._collate if collate is None else bool(collate)

    @property
    def _print_profiles(self) -> dict[str, PrintProfile]:
        profiles, errors = parse_print_profiles(
            self._entry.options.get(CONF_PRINT_TYPES, DEFAULT_PRINT_TYPES)
        )
        if errors:
            logger.warning(
                "Ignoring invalid Print Bridge print profile configuration: %s",
                "; ".join(errors),
            )
        return profiles

    @property
    def _default_print_type(self) -> str:
        profiles = self._print_profiles
        return resolve_default_print_type(
            self._entry.options.get(CONF_DEFAULT_PRINT_TYPE, DEFAULT_PRINT_TYPE),
            profiles,
        )

    def print_profile_names(self) -> list[str]:
        """Return valid print profile names in UI display order."""
        profiles = self._print_profiles
        builtins = ["normal", "simplex", "duplex", "booklet", "draft"]
        return [
            *[name for name in builtins if name in profiles],
            *sorted(name for name in profiles if name not in builtins),
        ]

    @property
    def _allowed_senders(self) -> list[str]:
        senders: list[str] = []
        for sender in self._entry.options.get(CONF_ALLOWED_SENDERS, []):
            normalised = _normalise_email_address(sender)
            if normalised:
                senders.append(normalised)
        return senders

    def _sender_can_use_mail_config(self, sender: str | None) -> bool:
        """Return True only for explicitly configured allowed senders."""
        normalised = _normalise_email_address(sender or "")
        allowed = self._allowed_senders
        return bool(normalised and allowed and normalised in allowed)

    def _mail_params_for_sender(
        self,
        subject: str,
        body: str,
        sender: str | None,
        mail_config_allowed: bool | None = None,
    ) -> MailPrintParameters:
        """Parse per-mail parameters only when the sender is explicitly trusted."""
        params = parse_mail_print_parameters(subject, body)
        allowed = (
            bool(mail_config_allowed)
            if mail_config_allowed is not None
            else self._sender_can_use_mail_config(sender)
        )
        if allowed:
            return params
        if params.has_values or params.config_request or params.errors:
            logger.info(
                "Ignoring Print Bridge mail parameters from %s because the sender "
                "is not listed in Allowed Senders",
                sender or "unknown sender",
            )
        return MailPrintParameters()

    @property
    def _folder_filter(self) -> list[str]:
        """IMAP folder names to accept; empty list means accept all folders."""
        return [f.strip() for f in self._entry.options.get(CONF_FOLDER_FILTER, []) if f.strip()]

    @property
    def _auto_print_enabled(self) -> bool:
        """True if the coordinator should automatically print on imap_content events."""
        return bool(self._entry.options.get(CONF_AUTO_PRINT_ENABLED, DEFAULT_AUTO_PRINT_ENABLED))

    @property
    def _status_reply_enabled(self) -> bool:
        return bool(
            self._entry.options.get(
                CONF_STATUS_REPLY_ENABLED, DEFAULT_STATUS_REPLY_ENABLED
            )
        )

    @property
    def _status_reply_notify_service(self) -> str:
        return str(
            self._entry.options.get(
                CONF_STATUS_REPLY_NOTIFY_SERVICE,
                DEFAULT_STATUS_REPLY_NOTIFY_SERVICE,
            )
            or ""
        ).strip()

    @property
    def _schedule_enabled(self) -> bool:
        return bool(self._entry.options.get(CONF_SCHEDULE_ENABLED, DEFAULT_SCHEDULE_ENABLED))

    @property
    def _schedule_start(self) -> str:
        return self._entry.options.get(CONF_SCHEDULE_START, DEFAULT_SCHEDULE_START)

    @property
    def _schedule_end(self) -> str:
        return self._entry.options.get(CONF_SCHEDULE_END, DEFAULT_SCHEDULE_END)

    @property
    def selected_imap_entry_id(self) -> str | None:
        """Selected IMAP account for previews and on-demand email printing."""
        imap_entries = self.hass.config_entries.async_entries("imap")
        if not imap_entries:
            return None
        configured = self._entry.options.get(CONF_SELECTED_IMAP_ENTRY_ID)
        if configured in {entry.entry_id for entry in imap_entries}:
            return configured
        return imap_entries[0].entry_id

    @property
    def selected_printer_entry_id(self) -> str:
        """Selected Print Bridge entry used as the dashboard print target."""
        print_entries = self.hass.config_entries.async_entries(DOMAIN)
        configured = self._entry.options.get(CONF_SELECTED_PRINTER_ENTRY_ID)
        if configured in {entry.entry_id for entry in print_entries}:
            return configured
        return self._entry.entry_id

    @property
    def selected_printer_coordinator(self) -> AutoPrintCoordinator:
        """Coordinator for the selected target printer, or this one as fallback."""
        selected_entry_id = self.selected_printer_entry_id
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.entry_id == selected_entry_id and entry.runtime_data is not None:
                return entry.runtime_data  # type: ignore[return-value]
        return self

    def set_selected_imap_entry_id(self, entry_id: str) -> None:
        """Persist the selected IMAP account for this Print Bridge entry."""
        self.set_option(CONF_SELECTED_IMAP_ENTRY_ID, entry_id)

    def set_selected_printer_entry_id(self, entry_id: str) -> None:
        """Persist the selected target printer for this Print Bridge entry."""
        self.set_option(CONF_SELECTED_PRINTER_ENTRY_ID, entry_id)

    def set_option(self, key: str, value: Any) -> None:
        """Persist one option and refresh coordinator-backed entities."""
        options = dict(self._entry.options)
        options[key] = value
        self.hass.config_entries.async_update_entry(self._entry, options=options)
        if self.data is not None:
            self.async_set_updated_data(self.data)
        if key in _SIGNAL_OPTION_KEYS:
            self.hass.async_create_task(self.async_sync_signal_receiver())

    @property
    def _signal_enabled(self) -> bool:
        return self._signal_enabled_configured and self.signal_rest_integration_detected

    @property
    def _signal_enabled_configured(self) -> bool:
        configured = bool(
            self._entry.options.get(CONF_SIGNAL_ENABLED, DEFAULT_SIGNAL_ENABLED)
        )
        return configured

    @property
    def signal_rest_configured(self) -> bool:
        """True when the options contain enough data to probe Signal REST."""
        return bool(self._signal_rest_url and self._signal_account)

    @property
    def signal_rest_integration_detected(self) -> bool:
        """True after the configured Signal REST module has answered a probe."""
        return self._signal_rest_detected

    @property
    def signal_messenger_integration_detected(self) -> bool:
        """True when HA has the legacy Signal Messenger integration loaded."""
        return bool(
            self.hass.config_entries.async_entries(SIGNAL_REST_INTEGRATION_DOMAIN)
            or any(component in self.hass.config.components for component in SIGNAL_REST_COMPONENTS)
        )

    @property
    def signal_rest_checked_at(self) -> str | None:
        return self._signal_rest_checked_at

    @property
    def signal_rest_error(self) -> str | None:
        return self._signal_rest_error

    @property
    def _signal_module_id(self) -> str:
        return str(
            self._entry.options.get(CONF_SIGNAL_MODULE_ID, DEFAULT_SIGNAL_MODULE_ID)
            or DEFAULT_SIGNAL_MODULE_ID
        ).strip()

    @property
    def _signal_rest_url(self) -> str:
        return str(
            self._entry.options.get(CONF_SIGNAL_REST_URL, DEFAULT_SIGNAL_REST_URL)
            or ""
        ).strip().rstrip("/")

    @property
    def _signal_account(self) -> str:
        return str(
            self._entry.options.get(CONF_SIGNAL_ACCOUNT, DEFAULT_SIGNAL_ACCOUNT)
            or ""
        ).strip()

    @property
    def _signal_allowed_senders(self) -> set[str]:
        values = self._entry.options.get(
            CONF_SIGNAL_ALLOWED_SENDERS, DEFAULT_SIGNAL_ALLOWED_SENDERS
        )
        return {
            _normalise_signal_identity(str(value))
            for value in values
            if _normalise_signal_identity(str(value))
        }

    @property
    def _signal_allowed_group_values(self) -> list[str]:
        values = self._entry.options.get(
            CONF_SIGNAL_ALLOWED_GROUP_IDS, DEFAULT_SIGNAL_ALLOWED_GROUP_IDS
        )
        return [str(value).strip() for value in values if str(value).strip()]

    @property
    def _signal_allowed_group_ids(self) -> set[str]:
        return {
            _normalise_signal_group_id(value)
            for value in self._signal_allowed_group_values
            if _normalise_signal_group_id(value)
        }

    @property
    def _signal_allowed_group_names(self) -> set[str]:
        return {
            _normalise_signal_group_name(value)
            for value in self._signal_allowed_group_values
            if _normalise_signal_group_name(value)
            and not _looks_like_signal_group_id(value)
        }

    def _signal_resolved_group_names(self) -> dict[str, list[str]]:
        """Return configured group-name matches found in discovered Signal groups."""
        allowed_names = self._signal_allowed_group_names
        matches: dict[str, set[str]] = {name: set() for name in allowed_names}
        for group in self._signal_groups:
            group_name = _normalise_signal_group_name(str(group.get("name", "")))
            if group_name not in allowed_names:
                continue
            group_id = _normalise_signal_group_id(str(group.get("id", "")))
            if group_id:
                matches[group_name].add(group_id)
        return {name: sorted(group_ids) for name, group_ids in matches.items()}

    def _signal_resolved_allowed_group_ids(self) -> set[str]:
        """Return group IDs allowed directly or through a unique configured name."""
        group_ids = set(self._signal_allowed_group_ids)
        for matches in self._signal_resolved_group_names().values():
            if len(matches) == 1:
                group_ids.add(matches[0])
        return group_ids

    def _signal_ambiguous_allowed_group_names(self) -> list[str]:
        return sorted(
            name
            for name, matches in self._signal_resolved_group_names().items()
            if len(matches) > 1
        )

    def _signal_unresolved_allowed_group_names(self) -> list[str]:
        known_group_ids = {
            _normalise_signal_group_id(str(group.get("id", "")))
            for group in self._signal_groups
        }
        matches = self._signal_resolved_group_names()
        unresolved: list[str] = []
        for value in self._signal_allowed_group_values:
            if _looks_like_signal_group_id(value):
                continue
            group_id = _normalise_signal_group_id(value)
            group_name = _normalise_signal_group_name(value)
            if group_id in known_group_ids or matches.get(group_name):
                continue
            unresolved.append(value)
        return sorted(unresolved, key=str.casefold)

    @property
    def _signal_confirmation_mode(self) -> str:
        return str(
            self._entry.options.get(
                CONF_SIGNAL_CONFIRMATION_MODE, DEFAULT_SIGNAL_CONFIRMATION_MODE
            )
            or DEFAULT_SIGNAL_CONFIRMATION_MODE
        )

    @property
    def _signal_confirmation_ttl(self) -> timedelta:
        try:
            hours = int(
                self._entry.options.get(
                    CONF_SIGNAL_CONFIRMATION_TTL_HOURS,
                    DEFAULT_SIGNAL_CONFIRMATION_TTL_HOURS,
                )
            )
        except (TypeError, ValueError):
            hours = DEFAULT_SIGNAL_CONFIRMATION_TTL_HOURS
        return timedelta(hours=max(1, min(168, hours)))

    @property
    def _signal_reply_enabled(self) -> bool:
        return self._signal_confirmation_mode in {"ha_and_signal", "signal_only"}

    def _signal_client(self) -> SignalRestClient:
        if not self._signal_rest_url or not self._signal_account:
            raise RuntimeError(
                "Signal REST URL and Signal account must be configured first."
            )
        return SignalRestClient(
            async_get_clientsession(self.hass),
            base_url=self._signal_rest_url,
            account=self._signal_account,
        )

    def _set_signal_rest_status(self, detected: bool, error: str | None = None) -> None:
        """Store the latest Signal REST detection result."""
        self._signal_rest_detected = detected
        self._signal_rest_error = error
        self._signal_rest_checked_at = datetime.now().isoformat(timespec="seconds")

    def _refresh_signal_entities(self) -> None:
        """Push Signal-only state changes to coordinator-backed entities."""
        if self.data is None:
            return
        self.async_set_updated_data(
            replace(
                self.data,
                signal_pending_jobs=list(self._signal_pending_jobs),
                signal_groups=list(self._signal_groups),
                signal_groups_checked_at=self._signal_groups_checked_at,
            )
        )

    async def async_check_signal_rest(self) -> bool:
        """Probe the configured Signal REST module and update detection state."""
        if not self.signal_rest_configured:
            self._set_signal_rest_status(
                False,
                "Signal REST URL and Signal account must be configured first.",
            )
            self._refresh_signal_entities()
            return False
        try:
            client = self._signal_client()
            await client.check_health()
        except Exception as exc:
            self._set_signal_rest_status(False, _describe_exception(exc))
            self._refresh_signal_entities()
            return False
        await self._async_refresh_signal_groups(client, raise_on_error=False)
        self._set_signal_rest_status(True, None)
        self._refresh_signal_entities()
        return True

    @property
    def _schedule_days(self) -> list[str]:
        return _normalise_schedule_days(
            self._entry.options.get(CONF_SCHEDULE_DAYS, DEFAULT_SCHEDULE_DAYS)
        )

    @property
    def _schedule_template(self) -> str:
        return str(
            self._entry.options.get(CONF_SCHEDULE_TEMPLATE, DEFAULT_SCHEDULE_TEMPLATE)
            or ""
        ).strip()

    def _schedule_template_allows_printing(
        self, now: datetime, schedule_window_day: str
    ) -> bool:
        """Return True when the optional HA template renders truthy."""
        template_text = self._schedule_template
        if not template_text:
            return True

        weekday = SCHEDULE_DAYS[now.weekday()]
        try:
            rendered = Template(template_text, self.hass).async_render(
                {
                    "now": now,
                    "schedule_time": now.time(),
                    "schedule_weekday": weekday,
                    "schedule_window_day": schedule_window_day,
                    "schedule_days": self._schedule_days,
                    "schedule_start": self._schedule_start,
                    "schedule_end": self._schedule_end,
                    "printer_name": self._printer_name,
                },
                parse_result=True,
            )
        except TemplateError as err:
            logger.warning(
                "Print schedule template failed; allowing job through: %s", err
            )
            return True
        return _template_result_is_truthy(rendered)

    def _is_within_schedule(self) -> bool:
        """Return True if local day, time, and template allow printing."""
        if not self._schedule_enabled:
            return True

        from homeassistant.util import dt as dt_util

        now = dt_util.now()
        time_open = True
        schedule_window_day = SCHEDULE_DAYS[now.weekday()]
        try:
            start = datetime.strptime(self._schedule_start, "%H:%M").time()
            end = datetime.strptime(self._schedule_end, "%H:%M").time()
            current_time = now.time()
            if start <= end:
                time_open = start <= current_time <= end
            else:
                # Window wraps midnight (e.g. 22:00 → 07:00)
                if current_time >= start:
                    time_open = True
                elif current_time <= end:
                    time_open = True
                    schedule_window_day = SCHEDULE_DAYS[(now.weekday() - 1) % 7]
                else:
                    time_open = False
        except ValueError:
            time_open = True  # bad time config → leave time unrestricted

        if not time_open:
            return False

        schedule_days = self._schedule_days
        if schedule_days and schedule_window_day not in schedule_days:
            return False

        return self._schedule_template_allows_printing(now, schedule_window_day)

    @property
    def _email_action(self) -> str:
        return self._entry.options.get(CONF_EMAIL_ACTION, DEFAULT_EMAIL_ACTION)

    @property
    def _email_archive_folder(self) -> str:
        return self._entry.options.get(CONF_EMAIL_ARCHIVE_FOLDER, DEFAULT_EMAIL_ARCHIVE_FOLDER)

    @property
    def _notify_on_failure(self) -> bool:
        return bool(self._entry.options.get(CONF_NOTIFY_ON_FAILURE, DEFAULT_NOTIFY_ON_FAILURE))

    @property
    def _notify_on_success(self) -> bool:
        return bool(self._entry.options.get(CONF_NOTIFY_ON_SUCCESS, DEFAULT_NOTIFY_ON_SUCCESS))

    # ------------------------------------------------------------------
    # Signal intake
    # ------------------------------------------------------------------

    async def async_sync_signal_receiver(self) -> None:
        """Start or stop the Signal receive loop based on current options."""
        configured = bool(self._signal_enabled_configured and self.signal_rest_configured)
        if configured and not self.signal_rest_integration_detected:
            configured = await self.async_check_signal_rest()
        else:
            configured = configured and self.signal_rest_integration_detected
        task_running = (
            self._signal_receiver_task is not None
            and not self._signal_receiver_task.done()
        )
        if configured and not task_running:
            self._signal_receiver_task = self.hass.async_create_task(
                self._async_signal_receiver_loop()
            )
            logger.info(
                "Started Signal receiver for module %s at %s",
                self._signal_module_id,
                self._signal_rest_url,
            )
        elif not configured and task_running:
            await self.async_cancel_signal_receiver_task()

    async def async_cancel_signal_receiver_task(self) -> None:
        """Cancel the background Signal receive loop during unload/reconfigure."""
        if (
            self._signal_receiver_task is None
            or self._signal_receiver_task.done()
        ):
            return
        self._signal_receiver_task.cancel()
        try:
            await self._signal_receiver_task
        except asyncio.CancelledError:
            pass
        finally:
            self._signal_receiver_task = None

    async def _async_signal_receiver_loop(self) -> None:
        """Poll/stream the Signal REST module and queue documents for confirmation."""
        while self._signal_enabled and self._signal_rest_url and self._signal_account:
            try:
                client = self._signal_client()
                payload = await client.receive()
                if self._signal_rest_error:
                    self._set_signal_rest_status(True, None)
                    self._refresh_signal_entities()
                await self.async_process_signal_payload(payload, client=client)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._signal_rest_error = f"Receive failed: {_describe_exception(exc)}"
                self._refresh_signal_entities()
                logger.warning("Signal receive failed: %s", exc)
            await asyncio.sleep(_SIGNAL_POLL_INTERVAL_SECONDS)

    async def async_process_signal_payload(
        self,
        payload: Any,
        *,
        client: SignalRestClient | None = None,
    ) -> int:
        """Process a raw Signal receive payload and return pending/command count."""
        if not self._signal_enabled:
            return 0
        self._expire_signal_pending_jobs()
        signal_client = client or self._signal_client()
        handled = 0
        for message in parse_signal_messages(payload):
            if await self._async_handle_signal_command(message, signal_client):
                handled += 1
                continue
            if await self._async_create_signal_pending_job(message, signal_client):
                handled += 1
        if handled:
            await self.async_request_refresh()
        return handled

    def _signal_message_allowed(self, message: SignalMessage) -> bool:
        """Return True when a Signal message is from a trusted source/group."""
        allowed_senders = self._signal_allowed_senders
        allowed_groups = self._signal_resolved_allowed_group_ids()
        ambiguous_group_names = set(self._signal_ambiguous_allowed_group_names())
        sender_keys = {
            _normalise_signal_identity(message.sender),
            _normalise_signal_identity(message.sender_uuid or ""),
        }
        sender_allowed = bool(allowed_senders & sender_keys)
        group_allowed = bool(
            message.group_id
            and _normalise_signal_group_id(message.group_id) in allowed_groups
        )
        group_name = _normalise_signal_group_name(message.group_name or "")
        if (
            not group_allowed
            and group_name
            and group_name in self._signal_allowed_group_names
            and group_name not in ambiguous_group_names
        ):
            group_allowed = True
        return sender_allowed or group_allowed

    async def _async_handle_signal_command(
        self,
        message: SignalMessage,
        client: SignalRestClient,
    ) -> bool:
        """Handle `set <token>`, `print <token>`, or `cancel <token>` replies."""
        text = message.text or ""
        update = _SIGNAL_SET_RE.match(text)
        confirm = _SIGNAL_CONFIRM_RE.match(text)
        cancel = _SIGNAL_CANCEL_RE.match(text)
        if not update and not confirm and not cancel:
            return False
        if not self._signal_message_allowed(message):
            logger.info(
                "Ignoring Signal confirmation command from untrusted source %s group=%s",
                message.sender or message.sender_uuid,
                message.group_id,
            )
            return True
        command_match = update or confirm or cancel
        token = command_match.group("token")  # type: ignore[union-attr]
        job = self._find_signal_pending_job(token=token)
        if job is None:
            await self._async_send_signal_message(
                client,
                [message.group_id or message.sender],
                f"Print Bridge: no pending Signal job matches token {token}.",
            )
            return True
        if not self._signal_command_matches_job(message, job):
            await self._async_send_signal_message(
                client,
                [message.group_id or message.sender],
                "Print Bridge: this token belongs to another Signal sender or group.",
            )
            return True
        if update or confirm:
            settings_text = (command_match.groupdict().get("settings") or "").strip()
            if settings_text:
                params = parse_mail_print_parameters("", f"PB: {settings_text}")
                errors = self._signal_print_param_errors(params)
                if errors:
                    await self._async_send_signal_message(
                        client,
                        [message.group_id or message.sender],
                        "Print Bridge: invalid print settings:\n"
                        + "\n".join(f"- {error}" for error in errors),
                    )
                    return True
                self._apply_signal_print_params(job, params)
            if update:
                await self.async_request_refresh()
                await self._async_send_signal_message(
                    client,
                    [job.reply_recipient or ""],
                    self._format_signal_pending_reply(job),
                )
                return True
        if cancel:
            await self.async_cancel_signal_job(token=token, client=client)
        else:
            await self.async_confirm_signal_job(token=token, client=client)
        return True

    def _signal_command_matches_job(
        self,
        message: SignalMessage,
        job: SignalPendingJob,
    ) -> bool:
        if job.group_id:
            return _normalise_signal_group_id(message.group_id or "") == (
                _normalise_signal_group_id(job.group_id)
            )
        message_senders = {
            _normalise_signal_identity(message.sender),
            _normalise_signal_identity(message.sender_uuid or ""),
        }
        job_senders = {
            _normalise_signal_identity(job.sender),
            _normalise_signal_identity(job.sender_uuid or ""),
        }
        return bool(message_senders & job_senders)

    def _signal_print_param_errors(self, params: MailPrintParameters) -> list[str]:
        """Return validation errors for Signal print settings."""
        errors = list(params.errors)
        if params.print_type and params.print_type not in self._print_profiles:
            errors.append(
                f"Unknown print type '{params.print_type}'. Valid types: "
                + ", ".join(self.print_profile_names())
            )
        return errors

    def _apply_signal_print_params(
        self,
        job: SignalPendingJob,
        params: MailPrintParameters,
    ) -> None:
        """Patch a pending Signal job with explicit print setting overrides."""
        if params.print_type:
            job.print_type = normalize_print_type(params.print_type)
        if params.duplex is not None:
            job.duplex_override = params.duplex
        if params.booklet is not None:
            job.booklet_override = params.booklet
        if params.copies is not None:
            job.copies = params.copies
        if params.collate is not None:
            job.collate = params.collate
        if params.orientation is not None:
            job.orientation = params.orientation
        if params.media is not None:
            job.media = params.media
        if params.raster_dpi is not None:
            job.raster_dpi = params.raster_dpi
        if params.reverse_order is not None:
            job.reverse_order = params.reverse_order
        self._refresh_signal_job_effective_settings(job)

    def _signal_job_override_params(self, job: SignalPendingJob) -> MailPrintParameters:
        """Return explicit pending-job overrides as MailPrintParameters."""
        return MailPrintParameters(
            print_type=job.print_type,
            duplex=job.duplex_override,
            booklet=job.booklet_override,
            copies=job.copies,
            collate=job.collate,
            orientation=job.orientation,
            media=job.media,
            raster_dpi=job.raster_dpi,
            reverse_order=job.reverse_order,
        )

    def _signal_job_print_params(self, job: SignalPendingJob) -> MailPrintParameters:
        """Return profile settings merged with pending-job overrides."""
        profiles = self._print_profiles
        profile = profiles.get(job.print_type) or profiles[self._default_print_type]
        return merge_print_parameters(
            profile.params,
            self._signal_job_override_params(job),
        )

    def _signal_job_effective_settings(self, job: SignalPendingJob) -> dict[str, Any]:
        """Return resolved settings that would be used if the job printed now."""
        params = self._signal_job_print_params(job)
        booklet = (
            params.booklet
            if params.booklet is not None
            else any(
                is_booklet_job(item.filename, self._booklet_patterns)
                for item in job.attachments
            )
        )
        orientation = _orientation_for_job(bool(booklet), params.orientation)
        raster_dpi = _normalise_raster_dpi(params.raster_dpi or self._raster_dpi)
        duplex = params.duplex or self._duplex_mode
        if booklet:
            duplex = "two-sided-short-edge"
        return {
            "print_type": job.print_type,
            "duplex": duplex,
            "booklet": bool(booklet),
            "copies": params.copies or 1,
            "collate": self._resolve_collate(params.collate),
            "orientation": orientation or "default",
            "media": params.media or "default",
            "raster_dpi": raster_dpi,
            "reverse_order": self._resolve_reverse_order(params.reverse_order),
        }

    def _refresh_signal_job_effective_settings(self, job: SignalPendingJob) -> None:
        job.effective_settings = self._signal_job_effective_settings(job)

    def _format_signal_settings_lines(self, job: SignalPendingJob) -> list[str]:
        settings = self._signal_job_effective_settings(job)
        return [
            f"Type: {settings['print_type']}",
            f"Duplex: {settings['duplex']}",
            f"Booklet: {'yes' if settings['booklet'] else 'no'}",
            f"Copies: {settings['copies']}",
            f"Collate: {'yes' if settings['collate'] else 'no'}",
            f"Orientation: {settings['orientation']}",
            f"Media: {settings['media']}",
            f"Raster DPI: {settings['raster_dpi']}",
            f"Reverse order: {'yes' if settings['reverse_order'] else 'no'}",
        ]

    async def _async_create_signal_pending_job(
        self,
        message: SignalMessage,
        client: SignalRestClient,
    ) -> bool:
        """Create a pending confirmation job for a Signal message with attachments."""
        if not message.attachments:
            return False
        if not self._signal_message_allowed(message):
            logger.info(
                "Ignoring Signal document from untrusted source %s group=%s",
                message.sender or message.sender_uuid,
                message.group_id,
            )
            return False

        mail_params = parse_mail_print_parameters("", message.text or "")
        param_errors = self._signal_print_param_errors(mail_params)
        if param_errors:
            await self._async_send_signal_message(
                client,
                [message.group_id or message.sender],
                "Print Bridge: invalid print parameters: "
                + "; ".join(param_errors),
            )
            return False

        attachments: list[SignalAttachment] = []
        skipped: list[str] = []
        dedupe_parts: list[str] = []
        for attachment in message.attachments:
            filename = sanitize_ipp_job_name(attachment.filename)
            content_type = attachment.content_type or ""
            dedupe_key = (
                f"{message.message_id}:"
                f"{attachment.attachment_id or filename}:{content_type}"
            )
            dedupe_parts.append(dedupe_key)
            if dedupe_key in self._signal_seen_keys:
                skipped.append(f"{filename}: duplicate Signal attachment")
                continue
            resolved = printable_type(filename, content_type)
            if resolved is None:
                skipped.append(f"{filename}: unsupported file type")
                continue
            if not resolved.supported:
                skipped.append(f"{filename}: {resolved.reason or 'unsupported file type'}")
                continue
            try:
                data = attachment.data
                if data is None and attachment.attachment_id:
                    data = await client.get_attachment(attachment.attachment_id)
                if not data:
                    raise RuntimeError("attachment data is not available")
            except Exception as exc:
                skipped.append(f"{filename}: {_describe_exception(exc)}")
                continue
            attachments.append(
                SignalAttachment(
                    filename=filename,
                    content_type=content_type,
                    attachment_id=attachment.attachment_id,
                    data=data,
                )
            )

        if not attachments:
            if skipped and self._signal_reply_enabled:
                await self._async_send_signal_message(
                    client,
                    [message.group_id or message.sender],
                    "Print Bridge: no printable Signal attachments were queued.\n"
                    + "\n".join(f"- {item}" for item in skipped[:10]),
                )
            return False

        job_id = secrets.token_hex(8)
        token = secrets.token_hex(3)
        now = datetime.now()
        expires_at = (now + self._signal_confirmation_ttl).isoformat(
            timespec="seconds"
        )
        job = SignalPendingJob(
            job_id=job_id,
            token=token,
            message_id=message.message_id,
            sender=message.sender,
            sender_name=message.sender_name,
            sender_uuid=message.sender_uuid,
            group_id=message.group_id,
            group_name=message.group_name,
            text=message.text,
            attachments=tuple(attachments),
            print_type=mail_params.print_type or self._default_print_type,
            skipped_attachments=skipped,
            duplex_override=mail_params.duplex,
            booklet_override=mail_params.booklet,
            copies=mail_params.copies,
            collate=mail_params.collate,
            orientation=mail_params.orientation,
            media=mail_params.media,
            raster_dpi=mail_params.raster_dpi,
            reverse_order=mail_params.reverse_order,
            expires_at=expires_at,
        )
        self._refresh_signal_job_effective_settings(job)
        self._signal_pending_jobs.append(job)
        self._signal_seen_keys.update(dedupe_parts)
        logger.info(
            "Queued Signal document confirmation job %s from %s group=%s",
            job.job_id,
            message.sender or message.sender_uuid,
            message.group_id,
        )
        await self._async_notify_signal_pending_job(job)
        if self._signal_reply_enabled:
            await self._async_send_signal_message(
                client,
                [job.reply_recipient or ""],
                self._format_signal_pending_reply(job),
            )
        return True

    async def _async_notify_signal_pending_job(self, job: SignalPendingJob) -> None:
        """Create a HA notification for a pending Signal document."""
        try:
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Print Bridge — Signal document waiting",
                    "message": (
                        f"Signal document **{job.filename}** is waiting for confirmation.\n\n"
                        f"Sender: {job.sender or job.sender_uuid or 'unknown'}\n"
                        f"Group: {job.group_name or job.group_id or 'direct'}\n"
                        f"Token: `{job.token}`\n"
                        + "\n".join(self._format_signal_settings_lines(job))
                        + "\n"
                        f"Expires: {job.expires_at}\n\n"
                        "Use service `print_bridge.confirm_signal_job` or reply "
                        f"`print {job.token}` in Signal."
                    ),
                    "notification_id": f"print_bridge_signal_{job.job_id}",
                },
            )
        except Exception:
            logger.debug("Could not create Signal pending notification", exc_info=True)

    def _format_signal_pending_reply(self, job: SignalPendingJob) -> str:
        attachments = "\n".join(f"- {item.filename}" for item in job.attachments)
        skipped = ""
        if job.skipped_attachments:
            skipped = "\n\nSkipped:\n" + "\n".join(
                f"- {item}" for item in job.skipped_attachments[:10]
            )
        settings = "\n".join(self._format_signal_settings_lines(job))
        print_types = ", ".join(self.print_profile_names())
        return (
            "Print Bridge received document(s) waiting for confirmation.\n\n"
            f"Token: {job.token}\n"
            f"Expires: {job.expires_at}\n\n"
            f"Current settings:\n{settings}\n\n"
            "Reply with:\n"
            f"set {job.token} type=booklet copies=2\n"
            f"print {job.token}\n"
            f"print {job.token} type=booklet copies=2\n"
            f"cancel {job.token}\n\n"
            f"Print types: {print_types}\n\n"
            f"Attachments:\n{attachments}{skipped}"
        )

    async def async_confirm_signal_job(
        self,
        *,
        job_id: str | None = None,
        token: str | None = None,
        client: SignalRestClient | None = None,
    ) -> PrintJobResult:
        """Confirm and print a pending Signal document job."""
        self._expire_signal_pending_jobs()
        job = self._find_signal_pending_job(job_id=job_id, token=token)
        if job is None:
            from homeassistant.exceptions import HomeAssistantError

            raise HomeAssistantError("No matching pending Signal job found.")
        self._signal_pending_jobs.remove(job)

        summary = await self._async_convert_signal_attachments(job)
        params = self._signal_job_print_params(job)
        effective_settings = self._signal_job_effective_settings(job)
        effective_duplex = str(effective_settings["duplex"])
        effective_booklet = bool(effective_settings["booklet"])

        if not summary.converted:
            error = "No Signal attachments could be converted"
            result = PrintJobResult(
                filename=job.filename,
                success=False,
                error=error,
                sender=job.sender,
                duplex=effective_duplex,
                booklet=effective_booklet,
                copies=int(effective_settings["copies"]),
                collate=bool(effective_settings["collate"]),
                orientation=str(effective_settings["orientation"]),
                media=str(effective_settings["media"]),
                raster_dpi=int(effective_settings["raster_dpi"]),
                reverse_order=bool(effective_settings["reverse_order"]),
                attachments=[],
                skipped_attachments=[*job.skipped_attachments, *summary.skipped],
                merged_attachment_count=0,
                status=error,
                source="signal",
                signal_job_id=job.job_id,
                signal_group_id=job.group_id,
                signal_group_name=job.group_name,
            )
        else:
            merged_pdf = await self.hass.async_add_executor_job(
                merge_pdf_documents,
                summary.converted,
            )
            result = await self.async_send_print_job(
                job.filename,
                merged_pdf,
                effective_duplex,
                effective_booklet,
                copies=params.copies,
                collate=params.collate,
                orientation=params.orientation,
                media=params.media,
                raster_dpi=params.raster_dpi,
                reverse_order=params.reverse_order,
            )
            result.sender = job.sender
            result.source = "signal"
            result.signal_job_id = job.job_id
            result.signal_group_id = job.group_id
            result.signal_group_name = job.group_name
            result.source_format = (
                "mixed" if len(summary.converted) > 1 else summary.converted[0].source_format
            )
            result.converted_format = "application/pdf"
            result.attachments = summary.filenames
            result.skipped_attachments = [*job.skipped_attachments, *summary.skipped]
            result.merged_attachment_count = len(summary.converted)

        signal_client = client or (self._signal_client() if self._signal_reply_enabled else None)
        if signal_client and self._signal_reply_enabled:
            await self._async_send_signal_job_status(signal_client, job, result)
        self._record_job(result)
        await self._async_notify_job(result)
        await self.async_request_refresh()
        return result

    async def async_cancel_signal_job(
        self,
        *,
        job_id: str | None = None,
        token: str | None = None,
        client: SignalRestClient | None = None,
    ) -> dict[str, Any]:
        """Cancel a pending Signal document job."""
        job = self._find_signal_pending_job(job_id=job_id, token=token)
        if job is None:
            from homeassistant.exceptions import HomeAssistantError

            raise HomeAssistantError("No matching pending Signal job found.")
        self._signal_pending_jobs.remove(job)
        signal_client = client or (self._signal_client() if self._signal_reply_enabled else None)
        if signal_client and self._signal_reply_enabled:
            await self._async_send_signal_message(
                signal_client,
                [job.reply_recipient or ""],
                f"Print Bridge cancelled Signal job {job.job_id}.",
            )
        await self.async_request_refresh()
        return {"job_id": job.job_id, "cancelled": True}

    async def _async_convert_signal_attachments(
        self,
        job: SignalPendingJob,
    ) -> AttachmentConversionSummary:
        """Convert all pending Signal attachments to PDF."""
        summary = AttachmentConversionSummary()
        for attachment in job.attachments:
            try:
                if not attachment.data:
                    raise RuntimeError("attachment data is missing")
                converted = await self.hass.async_add_executor_job(
                    convert_document_to_pdf,
                    attachment.data,
                    attachment.filename,
                    attachment.content_type,
                )
            except Exception as exc:
                summary.skipped.append(
                    f"{attachment.filename}: {_describe_exception(exc)}"
                )
                continue
            summary.converted.append(converted)
        return summary

    def _find_signal_pending_job(
        self,
        *,
        job_id: str | None = None,
        token: str | None = None,
    ) -> SignalPendingJob | None:
        for job in self._signal_pending_jobs:
            if job_id and job.job_id == job_id:
                return job
            if token and job.token.lower() == token.lower():
                return job
        return None

    def _expire_signal_pending_jobs(self) -> None:
        """Discard expired Signal jobs."""
        if not self._signal_pending_jobs:
            return
        now = datetime.now()
        active: list[SignalPendingJob] = []
        for job in self._signal_pending_jobs:
            try:
                expires_at = datetime.fromisoformat(job.expires_at)
            except ValueError:
                expires_at = now
            if expires_at >= now:
                active.append(job)
            else:
                logger.info("Expired pending Signal job %s", job.job_id)
        self._signal_pending_jobs = active

    async def async_check_signal_groups(self) -> list[dict[str, Any]]:
        """Fetch Signal groups from the configured Signal REST module."""
        from homeassistant.exceptions import HomeAssistantError

        if not self._signal_rest_url or not self._signal_account:
            raise HomeAssistantError(
                "Configure Signal REST URL and Signal account before checking groups."
            )
        if not await self.async_check_signal_rest():
            raise HomeAssistantError(
                f"Signal REST module is not reachable: {self._signal_rest_error}"
            )
        groups = await self._async_refresh_signal_groups(
            self._signal_client(),
            raise_on_error=True,
        )
        await self.async_request_refresh()
        return groups

    async def _async_refresh_signal_groups(
        self,
        client: SignalRestClient,
        *,
        raise_on_error: bool,
    ) -> list[dict[str, Any]]:
        """Refresh discovered Signal groups for ID and name allow-list matching."""
        try:
            groups = await client.list_groups()
        except Exception as exc:
            self._signal_group_resolution_error = _describe_exception(exc)
            if raise_on_error:
                from homeassistant.exceptions import HomeAssistantError

                raise HomeAssistantError(
                    f"Signal group check failed: {self._signal_group_resolution_error}"
                ) from exc
            return []
        self._signal_groups = groups
        self._signal_groups_checked_at = datetime.now().isoformat(timespec="seconds")
        self._signal_group_resolution_error = None
        return groups

    async def _async_send_signal_job_status(
        self,
        client: SignalRestClient,
        job: SignalPendingJob,
        result: PrintJobResult,
    ) -> None:
        """Send a compact Signal status reply for a confirmed job."""
        status = "queued" if result.status_code == "queued-busy" else (
            "success" if result.success else "failed"
        )
        lines = [
            "Print Bridge status",
            f"Job: {result.filename}",
            f"Result: {status}",
            f"Status code: {result.status_code or 'n/a'}",
            f"Status: {result.status or result.error or 'accepted'}",
            f"Print type: {job.print_type}",
            f"Duplex: {result.duplex or 'default'}",
            f"Booklet: {'yes' if result.booklet else 'no'}",
            f"Copies: {result.copies or 1}",
            f"Collate: {'yes' if result.collate else 'no'}",
            f"Orientation: {result.orientation or 'default'}",
            f"Media: {result.media or 'default'}",
            f"Reverse order: {'yes' if result.reverse_order else 'no'}",
        ]
        if result.attachments:
            lines.append("Printed attachments:")
            lines.extend(f"- {item}" for item in result.attachments)
        if result.skipped_attachments:
            lines.append("Skipped attachments:")
            lines.extend(f"- {item}" for item in result.skipped_attachments[:10])
        await self._async_send_signal_message(
            client,
            [job.reply_recipient or ""],
            "\n".join(lines),
        )

    async def _async_send_signal_message(
        self,
        client: SignalRestClient,
        recipients: list[str | None],
        message: str,
    ) -> None:
        """Send a Signal message, logging failures without breaking printing."""
        filtered = [recipient for recipient in recipients if recipient]
        if not filtered:
            return
        try:
            await client.send_message(filtered, message)
        except Exception as exc:
            logger.warning("Signal status reply failed: %s", exc)

    # ------------------------------------------------------------------
    # IMAP event handler
    # ------------------------------------------------------------------

    async def async_handle_imap_event(self, event: Event) -> None:
        """Process an imap_content event from HA's built-in IMAP integration."""
        if not self._auto_print_enabled:
            logger.debug(
                "Auto-print disabled — ignoring imap_content event from %s "
                "(enable in Options or use the automation blueprint)",
                event.data.get("sender", "?"),
            )
            return

        sender: str = _normalise_email_address(event.data.get("sender", ""))
        allowed = self._allowed_senders
        if allowed and sender not in allowed:
            logger.debug("Skipping email from %s (not in allowed_senders)", sender)
            return

        ev_folder: str = event.data.get("folder", "")
        folder_filter = self._folder_filter
        if folder_filter and ev_folder not in folder_filter:
            logger.debug(
                "Skipping email in folder '%s' (not in folder_filter: %s)",
                ev_folder, folder_filter,
            )
            return

        parts: dict[str, dict[str, Any]] = event.data.get("parts", {})
        entry_id: str = event.data.get("entry_id", "")
        uid: str = str(event.data.get("uid", ""))
        subject: str = str(event.data.get("subject", ""))
        mail_config_allowed = self._sender_can_use_mail_config(sender)
        mail_params = self._mail_params_for_sender(
            subject,
            str(event.data.get("text", "")),
            sender,
            mail_config_allowed,
        )

        printable_parts = {
            part_key: part_info
            for part_key, part_info in parts.items()
            if _is_printable_part(str(part_key), part_info)
        }
        if not printable_parts:
            if mail_params.config_request or mail_params.errors:
                await self.async_process_imap_message(
                    entry_id=entry_id,
                    uid=uid,
                    parts=parts,
                    sender=sender,
                    attachment_filter=mail_params.attachment_filter,
                    attachment_ignore_filter=mail_params.attachment_ignore_filter,
                    allowed_extensions=mail_params.allowed_extensions,
                    ignored_extensions=mail_params.ignored_extensions,
                    duplex_override=mail_params.duplex,
                    booklet_override=mail_params.booklet,
                    copies=mail_params.copies,
                    collate=mail_params.collate,
                    orientation=mail_params.orientation,
                    media=mail_params.media,
                    raster_dpi=mail_params.raster_dpi,
                    reverse_order=mail_params.reverse_order,
                    mail_subject=subject,
                    mail_text=str(event.data.get("text", "")),
                    mail_config_allowed=mail_config_allowed,
                )
            elif parts:
                await self.async_request_refresh()
            return

        if not self._is_within_schedule():
            queued_filename = sanitize_ipp_job_name(subject or f"email_{uid}_attachments")
            if len(printable_parts) == 1:
                part_key, part_info = next(iter(printable_parts.items()))
                queued_filename = _attachment_filename(str(part_key), part_info)
            pending = PendingJob(
                entry_id=entry_id,
                uid=uid,
                part_key=None,
                filename=queued_filename,
                sender=sender,
                duplex_override=mail_params.duplex,
                booklet_override=mail_params.booklet,
                copies=mail_params.copies,
                collate=mail_params.collate,
                orientation=mail_params.orientation,
                media=mail_params.media,
                raster_dpi=mail_params.raster_dpi,
                reverse_order=mail_params.reverse_order,
                mail_subject=subject,
                mail_text=str(event.data.get("text", "")),
                mail_params=mail_params,
                message_level=True,
            )
            self._pending_jobs.append(pending)
            logger.info(
                "Email uid=%s queued with %d printable attachment(s) — outside print schedule (%s–%s)",
                uid,
                len(printable_parts),
                self._schedule_start,
                self._schedule_end,
            )
            try:
                await self.hass.services.async_call(
                    "persistent_notification", "create",
                    {
                        "title": "Print Bridge — Job queued",
                        "message": (
                            f"Email **{subject or uid}** from {sender or 'unknown'} "
                            f"has {len(printable_parts)} printable attachment(s), "
                            f"arrived outside the print schedule ({self._schedule_start}–"
                            f"{self._schedule_end}), and will be printed when the window opens."
                        ),
                        "notification_id": f"print_bridge_queued_{entry_id}_{uid}",
                    },
                )
            except Exception:
                pass
            await self.async_request_refresh()
            return

        result = await self.async_process_imap_message(
            entry_id=entry_id,
            uid=uid,
            parts=parts,
            sender=sender,
            attachment_filter=mail_params.attachment_filter,
            attachment_ignore_filter=mail_params.attachment_ignore_filter,
            allowed_extensions=mail_params.allowed_extensions,
            ignored_extensions=mail_params.ignored_extensions,
            duplex_override=mail_params.duplex,
            booklet_override=mail_params.booklet,
            copies=mail_params.copies,
            collate=mail_params.collate,
            orientation=mail_params.orientation,
            media=mail_params.media,
            raster_dpi=mail_params.raster_dpi,
            reverse_order=mail_params.reverse_order,
            mail_config_allowed=mail_config_allowed,
            mail_subject=subject,
            mail_text=str(event.data.get("text", "")),
        )

        # Only post-process the email after at least one attachment was printed.
        if result.success and (result.merged_attachment_count or 0) > 0:
            await self._async_post_process_email(entry_id, uid)

    async def _async_fetch_and_print(
        self,
        entry_id: str,
        uid: str,
        part_key: str,
        filename: str,
        duplex_override: str | None = None,
        booklet_override: bool | None = None,
        sender: str | None = None,
        copies: int | None = None,
        collate: bool | None = None,
        orientation: str | None = None,
        media: str | None = None,
        raster_dpi: int | None = None,
        content_type: str | None = None,
        reverse_order: bool | None = None,
    ) -> PrintJobResult:
        """Fetch one attachment via imap.fetch_part and print it.

        IMAP identifiers are always stored in the result so the job can be
        retried later via async_retry_job / print_bridge.retry_job.
        """
        try:
            response: dict[str, Any] = await self.hass.services.async_call(
                "imap",
                "fetch_part",
                {"entry": entry_id, "uid": uid, "part": part_key},
                blocking=True,
                return_response=True,
            )
        except Exception as exc:
            logger.error(
                "imap.fetch_part failed for uid=%s part=%s: %s", uid, part_key, exc
            )
            return PrintJobResult(
                filename=filename, success=False, error=str(exc),
                sender=sender,
                collate=self._resolve_collate(collate),
                raster_dpi=raster_dpi,
                reverse_order=self._resolve_reverse_order(reverse_order),
                imap_entry_id=entry_id, imap_uid=uid, imap_part_key=part_key,
            )

        try:
            raw_bytes = _decode_part_payload(response)
            effective_content_type = content_type or response.get("content_type")
        except Exception as exc:
            logger.error("Decoding attachment '%s' failed: %s", filename, exc)
            return PrintJobResult(
                filename=filename, success=False, error=str(exc),
                sender=sender,
                collate=self._resolve_collate(collate),
                raster_dpi=raster_dpi,
                reverse_order=self._resolve_reverse_order(reverse_order),
                imap_entry_id=entry_id, imap_uid=uid, imap_part_key=part_key,
            )

        try:
            converted = await self.hass.async_add_executor_job(
                convert_document_to_pdf,
                raw_bytes,
                filename,
                effective_content_type,
            )
        except (UnsupportedDocumentError, DocumentConversionError) as exc:
            error = _describe_exception(exc)
            logger.error("Converting attachment '%s' failed: %s", filename, error)
            return PrintJobResult(
                filename=filename, success=False, error=error,
                sender=sender,
                collate=self._resolve_collate(collate),
                raster_dpi=raster_dpi,
                reverse_order=self._resolve_reverse_order(reverse_order),
                source_format=(
                    printable_type(filename, effective_content_type).source_format
                    if printable_type(filename, effective_content_type)
                    else None
                ),
                skipped_attachments=[f"{filename}: {error}"],
                imap_entry_id=entry_id, imap_uid=uid, imap_part_key=part_key,
            )

        effective_duplex = duplex_override or self._duplex_mode
        effective_booklet = (
            booklet_override
            if booklet_override is not None
            else is_booklet_job(filename, self._booklet_patterns)
        )
        result = await self.async_send_print_job(
            filename,
            converted.pdf_data,
            effective_duplex,
            effective_booklet,
            copies=copies,
            collate=collate,
            orientation=orientation,
            media=media,
            raster_dpi=raster_dpi,
            reverse_order=reverse_order,
        )
        # Attach IMAP identifiers for future retry.
        result.sender = sender
        result.imap_entry_id = entry_id
        result.imap_uid = uid
        result.imap_part_key = part_key
        result.source_format = converted.source_format
        result.converted_format = converted.converted_format
        result.attachments = [filename]
        result.merged_attachment_count = 1
        return result

    # ------------------------------------------------------------------
    # Retry
    # ------------------------------------------------------------------

    async def async_retry_job(
        self,
        job: PrintJobResult,
        duplex_override: str | None = None,
        booklet_override: bool | None = None,
        collate_override: bool | None = None,
        reverse_order_override: bool | None = None,
    ) -> PrintJobResult:
        """Re-fetch and reprint a previously recorded job.

        Raises HomeAssistantError if the job has no IMAP retry metadata.
        """
        from homeassistant.exceptions import HomeAssistantError

        if not job.can_retry:
            raise HomeAssistantError(
                f"Cannot retry '{job.filename}': IMAP metadata is not available "
                "(only jobs received via email can be retried)."
            )

        logger.debug(
            "Retrying job '%s' uid=%s entry=%s",
            job.filename, job.imap_uid, job.imap_entry_id,
        )
        if job.imap_part_key == "message":
            return await self.async_process_imap_message(
                entry_id=job.imap_entry_id,  # type: ignore[arg-type]
                uid=job.imap_uid,  # type: ignore[arg-type]
                duplex_override=duplex_override or job.duplex,
                booklet_override=booklet_override if booklet_override is not None else job.booklet,
                sender=job.sender,
                copies=job.copies,
                collate=(
                    collate_override
                    if collate_override is not None
                    else job.collate
                ),
                orientation=job.orientation,
                media=job.media,
                raster_dpi=job.raster_dpi,
                reverse_order=(
                    reverse_order_override
                    if reverse_order_override is not None
                    else job.reverse_order
                ),
            )

        result = await self._async_fetch_and_print(
            entry_id=job.imap_entry_id,       # type: ignore[arg-type]
            uid=job.imap_uid,                  # type: ignore[arg-type]
            part_key=job.imap_part_key,        # type: ignore[arg-type]
            filename=job.filename,
            duplex_override=duplex_override or job.duplex,
            booklet_override=booklet_override if booklet_override is not None else job.booklet,
            sender=job.sender,
            copies=job.copies,
            collate=(
                collate_override
                if collate_override is not None
                else job.collate
            ),
            orientation=job.orientation,
            media=job.media,
            raster_dpi=job.raster_dpi,
            reverse_order=(
                reverse_order_override
                if reverse_order_override is not None
                else job.reverse_order
            ),
        )
        self._record_job(result)
        await self._async_notify_job(result)
        await self.async_request_refresh()
        return result

    async def async_retry_last_failed(self) -> PrintJobResult:
        """Retry the most recent failed job that has IMAP retry metadata."""
        from homeassistant.exceptions import HomeAssistantError

        for job in self._job_history:
            if not job.success and job.can_retry:
                return await self.async_retry_job(job)
        raise HomeAssistantError(
            "No failed job with retry information found in history."
        )

    # ------------------------------------------------------------------
    # Public helpers (called from services / button entity)
    # ------------------------------------------------------------------

    async def _async_post_process_email(self, entry_id: str, uid: str) -> None:
        """Apply the configured post-print email action via HA's IMAP actions."""
        action = self._email_action
        if action == "none":
            return

        try:
            if action == "mark_seen":
                await self.hass.services.async_call(
                    "imap", "seen",
                    {"entry": entry_id, "uid": uid},
                    blocking=True,
                )
            elif action == "move":
                await self.hass.services.async_call(
                    "imap", "move",
                    {
                        "entry": entry_id,
                        "uid": uid,
                        "target_folder": self._email_archive_folder,
                        "seen": True,
                    },
                    blocking=True,
                )
            elif action == "delete":
                await self.hass.services.async_call(
                    "imap", "delete",
                    {"entry": entry_id, "uid": uid},
                    blocking=True,
                )
        except Exception as exc:
            logger.warning(
                "Email post-processing action '%s' failed for uid=%s: %s",
                action, uid, exc,
            )

    async def _async_notify_job(self, result: PrintJobResult) -> None:
        """Send a HA persistent notification based on job outcome and settings."""
        if result.success and not self._notify_on_success:
            return
        if not result.success and not self._notify_on_failure:
            return

        if result.status_code == "queued-busy":
            title = "Print Bridge — Queued for retry"
            message = (
                f"**{result.filename}**\n"
                "Printer is busy; Print Bridge will poll it and resend the job."
            )
            if result.sender:
                message += f"\nFrom: {result.sender}"
        elif result.success:
            title = f"Print Bridge — Printed successfully"
            message = f"**{result.filename}**"
            if result.sender:
                message += f"\nFrom: {result.sender}"
            if result.duplex:
                message += f"\nDuplex: {result.duplex}"
            if result.booklet:
                message += "\nBooklet mode"
        else:
            title = "Print Bridge — Print failed"
            message = f"**{result.filename}** could not be printed."
            if result.error:
                message += f"\nError: {result.error}"
            if result.sender:
                message += f"\nFrom: {result.sender}"
            message += "\n\nCheck the HA logs or the Print Bridge sensor for details."

        try:
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": title,
                    "message": message,
                    "notification_id": f"print_bridge_{self._entry.entry_id}_{result.timestamp}",
                },
            )
        except Exception:
            logger.warning("Could not send notification for job '%s'", result.filename)

    async def _async_send_status_reply(
        self,
        *,
        imap_entry_id: str | None = None,
        sender: str | None,
        subject: str,
        results: list[PrintJobResult],
        params: MailPrintParameters,
    ) -> None:
        """Send a status email through notify service or SMTP fallback."""
        if params.reply is False:
            return
        if not (self._status_reply_enabled or params.reply is True):
            return
        await self._async_send_reply_message(
            imap_entry_id=imap_entry_id,
            sender=sender,
            title=f"Re: {subject or 'Print Bridge status'}",
            message=self._format_status_reply(results, params),
            results=results,
        )

    async def _async_send_config_reply(
        self,
        *,
        imap_entry_id: str | None,
        sender: str | None,
        subject: str,
        result: PrintJobResult,
    ) -> None:
        """Send the mail-parameter template to an allowed sender."""
        await self._async_send_reply_message(
            imap_entry_id=imap_entry_id,
            sender=sender,
            title=f"Re: {subject or 'Print Bridge configuration'}",
            message=self._format_config_reply(),
            results=[result],
        )

    async def _async_send_reply_message(
        self,
        *,
        imap_entry_id: str | None,
        sender: str | None,
        title: str,
        message: str,
        results: list[PrintJobResult],
    ) -> None:
        """Deliver a plain-text email reply and store delivery metadata."""
        if not sender:
            logger.warning("Cannot send print reply: sender address is empty")
            for result in results:
                result.status_reply_delivery = "not_sent: sender address is empty"
            await self._async_notify_status_reply_issue(
                "Sender address is empty, so no status email could be sent."
            )
            return

        for result in results:
            result.status_reply_recipient = sender
            result.status_reply_subject = title
            result.status_reply_message = message
            result.status_reply_delivery = "pending"
        service_ref = self._status_reply_notify_service
        if not service_ref:
            delivery = await self._async_send_status_reply_via_smtp(
                imap_entry_id=imap_entry_id,
                recipient=sender,
                subject=title,
                message=message,
            )
            for result in results:
                result.status_reply_delivery = delivery
            return

        domain, service = _split_notify_service(service_ref)
        try:
            await self.hass.services.async_call(
                domain,
                service,
                {
                    "title": title,
                    "message": message,
                    "target": [sender],
                },
                blocking=True,
            )
            for result in results:
                result.status_reply_delivery = f"sent via {domain}.{service}"
        except Exception as exc:
            logger.warning(
                "Status reply via %s.%s to %s failed: %s",
                domain,
                service,
                sender,
                exc,
            )
            for result in results:
                result.status_reply_delivery = f"failed via {domain}.{service}: {exc}"
            await self._async_notify_status_reply_issue(
                f"Status reply via {domain}.{service} failed for {sender}: {exc}"
            )

    async def _async_send_status_reply_via_smtp(
        self,
        *,
        imap_entry_id: str | None,
        recipient: str,
        subject: str,
        message: str,
    ) -> str:
        """Send status reply through the matching IMAP account's SMTP server."""
        if not imap_entry_id:
            await self._async_notify_status_reply_issue(
                "No IMAP entry was available for SMTP status reply fallback."
            )
            return "not_sent: no IMAP entry for SMTP fallback"

        imap_entry = next(
            (
                entry
                for entry in self.hass.config_entries.async_entries("imap")
                if entry.entry_id == imap_entry_id
            ),
            None,
        )
        if imap_entry is None:
            await self._async_notify_status_reply_issue(
                f"IMAP entry {imap_entry_id} was not found for SMTP status reply fallback."
            )
            return f"not_sent: IMAP entry {imap_entry_id} not found"

        data = imap_entry.data
        server = str(data.get("server", "")).strip()
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))
        use_ssl = bool(data.get("ssl", data.get("use_ssl", True)))
        port = int(data.get("smtp_port") or (465 if use_ssl else 587))

        if not (server and username and password):
            await self._async_notify_status_reply_issue(
                "The IMAP account does not expose enough credentials for SMTP status reply fallback. "
                "Configure Status Reply Notify Service instead."
            )
            return "not_sent: IMAP account has no SMTP credentials"

        try:
            await self.hass.async_add_executor_job(
                partial(
                    _send_status_email_smtp,
                    server=server,
                    port=port,
                    use_ssl=use_ssl,
                    username=username,
                    password=password,
                    sender=username,
                    recipient=recipient,
                    subject=subject,
                    message=message,
                )
            )
            return f"sent via smtp {server}:{port}"
        except Exception as exc:
            logger.warning("SMTP status reply to %s failed: %s", recipient, exc)
            await self._async_notify_status_reply_issue(
                f"SMTP status reply to {recipient} failed via {server}:{port}: {exc}"
            )
            return f"failed via smtp {server}:{port}: {exc}"

    async def _async_notify_status_reply_issue(self, message: str) -> None:
        """Surface status reply delivery problems in HA instead of only logging."""
        try:
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Print Bridge - Status reply not sent",
                    "message": message,
                    "notification_id": f"print_bridge_status_reply_{self._entry.entry_id}",
                },
            )
        except Exception:
            logger.debug(
                "Could not create status reply diagnostic notification",
                exc_info=True,
            )

    async def _async_fetch_email_context(
        self, entry_id: str, uid: str
    ) -> dict[str, str]:
        """Fetch sender/subject/body for old blueprint calls that omit them."""
        try:
            fetched: dict[str, Any] = await self.hass.services.async_call(
                "imap",
                "fetch",
                {"entry": entry_id, "uid": uid},
                blocking=True,
                return_response=True,
            )
        except Exception as exc:
            logger.debug(
                "Could not fetch email context for uid=%s entry=%s: %s",
                uid,
                entry_id,
                exc,
            )
            return {}

        return {
            "sender": _normalise_email_address(str(fetched.get("sender", ""))),
            "subject": str(fetched.get("subject", "")),
            "text": str(fetched.get("text", "")),
        }

    def _parameter_error_result(
        self,
        filename: str,
        sender: str | None,
        params: MailPrintParameters,
    ) -> PrintJobResult:
        """Build a failed result for invalid per-mail print parameters."""
        error = "; ".join(params.errors) or "Invalid print parameters"
        return PrintJobResult(
            filename=sanitize_ipp_job_name(filename),
            success=False,
            error=error,
            sender=sender,
            duplex=params.duplex or self._duplex_mode,
            booklet=bool(params.booklet),
            copies=params.copies or 1,
            collate=self._resolve_collate(params.collate),
            orientation=params.orientation,
            media=params.media,
            raster_dpi=params.raster_dpi or self._raster_dpi,
            reverse_order=self._resolve_reverse_order(params.reverse_order),
            merged_attachment_count=0,
            status_code="invalid-mail-parameters",
            status=error,
        )

    def _format_status_reply(
        self, results: list[PrintJobResult], params: MailPrintParameters
    ) -> str:
        """Build a plain-text print status reply body."""
        lines = [
            "Print Bridge status",
            "",
            f"Printer: {self._printer_name or 'Direct IPP printer'}",
            f"Endpoint: {self._ipp_endpoint}",
            f"Printer URI: {self._printer_uri}",
        ]
        if params.has_values:
            lines.append(
                "Mail parameters: "
                + ", ".join(
                    f"{key}={value}" for key, value in params.as_dict().items()
                )
            )
        if params.errors:
            lines.append("Mail parameter errors:")
            lines.extend(f"- {error}" for error in params.errors)
        lines.append("")

        for index, result in enumerate(results, start=1):
            result_status = (
                "queued" if result.status_code == "queued-busy"
                else "success" if result.success else "failed"
            )
            lines.extend(
                [
                    f"Job {index}: {result.filename}",
                    f"Result: {result_status}",
                    f"Status code: {result.status_code or 'n/a'}",
                    f"Status: {result.status or result.error or 'accepted'}",
                    f"Source format: {result.source_format or 'unknown'}",
                    f"Converted format: {result.converted_format or 'n/a'}",
                    f"Document format: {result.document_format or 'application/pdf'}",
                    f"Merged attachments: {result.merged_attachment_count if result.merged_attachment_count is not None else 1}",
                    f"Duplex: {result.duplex or 'default'}",
                    f"IPP sides: {result.sides or 'default'}",
                    f"Booklet: {'yes' if result.booklet else 'no'}",
                    f"Reverse order: {'yes' if result.reverse_order else 'no'}",
                    f"Reverse applied: {'yes' if result.reverse_order_applied else 'no'}",
                    f"Copies: {result.copies or 1}",
                    f"Collate: {'yes' if result.collate else 'no'}",
                    f"Orientation: {result.orientation or 'default'}",
                    f"Media: {result.media or 'default'}",
                    f"Raster DPI: {result.raster_dpi or 'n/a'}",
                    f"Timestamp: {result.timestamp}",
                    "",
                ]
            )
            if result.attachments:
                lines.append("Printed attachments:")
                lines.extend(f"- {name}" for name in result.attachments)
            if result.skipped_attachments:
                lines.append("Skipped attachments:")
                lines.extend(f"- {name}" for name in result.skipped_attachments)
            if result.attachments or result.skipped_attachments:
                lines.append("")
        return "\n".join(lines).strip()

    def _format_config_reply(self) -> str:
        """Build the mail-parameter template sent for configuration requests."""
        return "\n".join(
            [
                "Print Bridge configuration",
                "",
                "Reply to this email, keep the Print-Bridge line below, adjust values, and attach files.",
                "",
                (
                    "Print-Bridge: "
                    f"duplex={self._duplex_mode}; "
                    "booklet=false; "
                    "nb_copies=1; "
                    f"collate={'true' if self._collate else 'false'}; "
                    'attachment_filter=""; '
                    'attachment_ignore_filter=""; '
                    'allowed_extensions=""; '
                    'ignored_extensions=""; '
                    f"dpi={self._raster_dpi}; "
                    f"reverse_order={'true' if self._reverse_order else 'false'}; "
                    "reply=true"
                ),
                "",
                "Optional parameters:",
                "- attachment_filter=\"text in filename\"",
                "- attachment_ignore_filter=\"draft, sample\"",
                "- allowed_extensions=\"pdf docx jpg\"",
                "- ignored_extensions=\"png, tiff\"",
                "- orientation=portrait|landscape",
                "- media=iso_a4_210x297mm|na_letter_8.5x11in|na_legal_8.5x14in",
                "- order=reverse|normal",
                "",
                "Accepted boolean values: true/false, yes/no, on/off, 1/0.",
                "Invalid values make the job fail with a status reply instead of printing.",
            ]
        )

    async def async_process_imap_part(
        self,
        entry_id: str,
        uid: str,
        part_key: str,
        filename: str | None = None,
        duplex_override: str | None = None,
        booklet_override: bool | None = None,
        sender: str | None = None,
        attachment_filter: str | None = None,
        attachment_ignore_filter: str | None = None,
        allowed_extensions: Any = None,
        ignored_extensions: Any = None,
        copies: int | None = None,
        collate: bool | None = None,
        orientation: str | None = None,
        media: str | None = None,
        raster_dpi: int | None = None,
        reverse_order: bool | None = None,
        mail_config_allowed: bool | None = None,
        mail_subject: str | None = None,
        mail_text: str | None = None,
    ) -> PrintJobResult:
        """Fetch one IMAP attachment and print it (called by the service)."""
        if not sender or mail_subject is None or mail_text is None:
            context = await self._async_fetch_email_context(entry_id, uid)
            sender = sender or context.get("sender") or None
            mail_subject = (
                mail_subject
                if mail_subject is not None
                else context.get("subject", "")
            )
            mail_text = (
                mail_text if mail_text is not None else context.get("text", "")
            )

        mail_params = self._mail_params_for_sender(
            mail_subject or "",
            mail_text or "",
            sender,
            mail_config_allowed,
        )
        if mail_params.errors:
            result = self._parameter_error_result(
                filename or f"attachment_{part_key}.pdf",
                sender,
                mail_params,
            )
            await self._async_send_status_reply(
                imap_entry_id=entry_id,
                sender=sender,
                subject=mail_subject or "",
                results=[result],
                params=replace(mail_params, reply=True),
            )
            self._record_job(result)
            await self.async_request_refresh()
            return result
        effective_duplex_override = mail_params.duplex or duplex_override
        effective_booklet_override = (
            mail_params.booklet if mail_params.booklet is not None else booklet_override
        )
        effective_attachment_filter = mail_params.attachment_filter or attachment_filter
        effective_attachment_ignore_filter = (
            mail_params.attachment_ignore_filter or attachment_ignore_filter
        )
        effective_allowed_extensions = (
            mail_params.allowed_extensions or allowed_extensions
        )
        effective_ignored_extensions = (
            mail_params.ignored_extensions or ignored_extensions
        )
        effective_copies = mail_params.copies or copies
        effective_collate = (
            mail_params.collate if mail_params.collate is not None else collate
        )
        effective_orientation = mail_params.orientation or orientation
        effective_media = mail_params.media or media
        effective_raster_dpi = mail_params.raster_dpi or raster_dpi
        effective_reverse_order = (
            mail_params.reverse_order
            if mail_params.reverse_order is not None
            else reverse_order
        )

        # Decode RFC 2047 MIME-encoded filenames that arrive from the IMAP event.
        effective_filename = _decode_mime_filename(
            filename or f"attachment_{part_key}.pdf"
        )

        skip_reason = _filter_skip_reason(
            effective_filename,
            "",
            attachment_filter=effective_attachment_filter,
            attachment_ignore_filter=effective_attachment_ignore_filter,
            allowed_extensions=effective_allowed_extensions,
            ignored_extensions=effective_ignored_extensions,
        )
        if skip_reason:
            logger.debug("Skipping attachment '%s' — %s", effective_filename, skip_reason)
            return PrintJobResult(
                filename=effective_filename,
                success=True,
                error=f"skipped: {skip_reason}",
            )

        # Deduplication: if another blueprint already printed this exact attachment
        # within the last 60 seconds, skip silently to prevent double-printing.
        _dedup_key = f"{uid}:{part_key}"
        _dedup_window = timedelta(seconds=60)
        _now = datetime.now()
        if _dedup_key in self._recently_printed:
            _age = _now - self._recently_printed[_dedup_key]
            if _age < _dedup_window:
                logger.debug(
                    "Duplicate print skipped for uid=%s part=%s (already printed %.1fs ago)",
                    uid, part_key, _age.total_seconds(),
                )
                return PrintJobResult(
                    filename=effective_filename, success=True,
                    error=f"skipped: duplicate (uid={uid} part={part_key} already printed)",
                )
        self._recently_printed[_dedup_key] = _now
        # Purge entries older than the dedup window to avoid unbounded growth.
        self._recently_printed = {
            k: v for k, v in self._recently_printed.items()
            if (_now - v) < _dedup_window
        }

        result = await self._async_fetch_and_print(
            entry_id=entry_id,
            uid=uid,
            part_key=part_key,
            filename=effective_filename,
            duplex_override=effective_duplex_override,
            booklet_override=effective_booklet_override,
            sender=sender,
            copies=effective_copies,
            collate=effective_collate,
            orientation=effective_orientation,
            media=effective_media,
            raster_dpi=effective_raster_dpi,
            reverse_order=effective_reverse_order,
        )
        await self._async_send_status_reply(
            imap_entry_id=entry_id,
            sender=sender,
            subject=mail_subject or "",
            results=[result],
            params=mail_params,
        )
        self._record_job(result)
        await self.async_request_refresh()
        return result

    async def async_process_imap_message(
        self,
        entry_id: str,
        uid: str,
        parts: dict[str, dict[str, Any]] | None = None,
        duplex_override: str | None = None,
        booklet_override: bool | None = None,
        sender: str | None = None,
        attachment_filter: str | None = None,
        attachment_ignore_filter: str | None = None,
        allowed_extensions: Any = None,
        ignored_extensions: Any = None,
        copies: int | None = None,
        collate: bool | None = None,
        orientation: str | None = None,
        media: str | None = None,
        raster_dpi: int | None = None,
        reverse_order: bool | None = None,
        mail_config_allowed: bool | None = None,
        mail_subject: str | None = None,
        mail_text: str | None = None,
    ) -> PrintJobResult:
        """Fetch, convert, merge, and print all printable attachments from an email."""
        if parts is not None and not isinstance(parts, dict):
            parts = None
        if parts is None or not sender or mail_subject is None or mail_text is None:
            try:
                fetched: dict[str, Any] = await self.hass.services.async_call(
                    "imap",
                    "fetch",
                    {"entry": entry_id, "uid": uid},
                    blocking=True,
                    return_response=True,
                )
            except Exception as exc:
                result = PrintJobResult(
                    filename=f"email_{uid}_attachments.pdf",
                    success=False,
                    error=f"Failed to fetch email uid={uid}: {exc}",
                    sender=sender,
                    collate=self._resolve_collate(collate),
                    reverse_order=self._resolve_reverse_order(reverse_order),
                    imap_entry_id=entry_id,
                    imap_uid=uid,
                    imap_part_key="message",
                )
                self._record_job(result)
                await self.async_request_refresh()
                return result

            parts = parts if parts is not None else fetched.get("parts", {})
            sender = sender or _normalise_email_address(str(fetched.get("sender", "")))
            mail_subject = mail_subject if mail_subject is not None else str(fetched.get("subject", ""))
            mail_text = mail_text if mail_text is not None else str(fetched.get("text", ""))

        mail_params = self._mail_params_for_sender(
            mail_subject or "",
            mail_text or "",
            sender,
            mail_config_allowed,
        )
        if mail_params.errors:
            result = self._parameter_error_result(
                mail_subject or f"email_{uid}_attachments.pdf",
                sender,
                mail_params,
            )
            result.imap_entry_id = entry_id
            result.imap_uid = uid
            result.imap_part_key = "message"
            await self._async_send_status_reply(
                imap_entry_id=entry_id,
                sender=sender,
                subject=mail_subject or "",
                results=[result],
                params=replace(mail_params, reply=True),
            )
            self._record_job(result)
            await self.async_request_refresh()
            return result

        if mail_params.config_request and not any(
            _is_printable_part(str(part_key), part_info)
            for part_key, part_info in (parts or {}).items()
        ):
            result = PrintJobResult(
                filename="print-bridge-config.txt",
                success=True,
                sender=sender,
                duplex=self._duplex_mode,
                booklet=False,
                copies=1,
                collate=self._collate,
                raster_dpi=self._raster_dpi,
                reverse_order=self._reverse_order,
                merged_attachment_count=0,
                status_code="config-sent",
                status="configuration parameters sent",
                imap_entry_id=entry_id,
                imap_uid=uid,
                imap_part_key="message",
            )
            await self._async_send_config_reply(
                imap_entry_id=entry_id,
                sender=sender,
                subject=mail_subject or "",
                result=result,
            )
            self._record_job(result)
            await self.async_request_refresh()
            return result

        effective_duplex = mail_params.duplex or duplex_override or self._duplex_mode
        effective_attachment_filter = mail_params.attachment_filter or attachment_filter
        effective_attachment_ignore_filter = (
            mail_params.attachment_ignore_filter or attachment_ignore_filter
        )
        effective_allowed_extensions = (
            mail_params.allowed_extensions or allowed_extensions
        )
        effective_ignored_extensions = (
            mail_params.ignored_extensions or ignored_extensions
        )
        effective_copies = mail_params.copies or copies
        effective_collate = (
            mail_params.collate if mail_params.collate is not None else collate
        )
        effective_orientation = mail_params.orientation or orientation
        effective_media = mail_params.media or media
        effective_raster_dpi = mail_params.raster_dpi or raster_dpi
        effective_reverse_order = (
            mail_params.reverse_order
            if mail_params.reverse_order is not None
            else reverse_order
        )

        summary = await self._async_convert_message_attachments(
            entry_id,
            uid,
            parts or {},
            effective_attachment_filter,
            effective_attachment_ignore_filter,
            effective_allowed_extensions,
            effective_ignored_extensions,
        )
        job_filename = self._message_job_filename(mail_subject or "", uid, summary)

        if not summary.converted:
            filter_active = any(
                (
                    (effective_attachment_filter or "").strip(),
                    _normalise_text_filters(effective_attachment_ignore_filter),
                    _normalise_extension_filters(effective_allowed_extensions),
                    _normalise_extension_filters(effective_ignored_extensions),
                )
            )
            filter_excluded_all = (
                filter_active
                and bool(summary.skipped)
                and all(_is_filter_skip(skipped) for skipped in summary.skipped)
            )
            if filter_excluded_all:
                status = "skipped: no attachments matched filters"
                result = PrintJobResult(
                    filename=job_filename,
                    success=True,
                    sender=sender,
                    duplex=effective_duplex,
                    booklet=bool(
                        mail_params.booklet
                        if mail_params.booklet is not None
                        else booklet_override
                    ),
                    copies=effective_copies or 1,
                    collate=self._resolve_collate(effective_collate),
                    orientation=effective_orientation,
                    media=effective_media,
                    raster_dpi=effective_raster_dpi,
                    reverse_order=self._resolve_reverse_order(effective_reverse_order),
                    attachments=[],
                    skipped_attachments=list(summary.skipped),
                    merged_attachment_count=0,
                    status_code="skipped-filter",
                    status=status,
                    imap_entry_id=entry_id,
                    imap_uid=uid,
                    imap_part_key="message",
                )
                self._record_job(result)
                await self.async_request_refresh()
                return result

            error = "No printable attachments were converted"
            result = PrintJobResult(
                filename=job_filename,
                success=False,
                error=error,
                sender=sender,
                duplex=effective_duplex,
                booklet=bool(effective_booklet := (
                    mail_params.booklet
                    if mail_params.booklet is not None
                    else booklet_override
                )),
                copies=effective_copies or 1,
                collate=self._resolve_collate(effective_collate),
                orientation=effective_orientation,
                media=effective_media,
                raster_dpi=effective_raster_dpi,
                reverse_order=self._resolve_reverse_order(effective_reverse_order),
                attachments=[],
                skipped_attachments=list(summary.skipped),
                merged_attachment_count=0,
                status=error,
                imap_entry_id=entry_id,
                imap_uid=uid,
                imap_part_key="message",
            )
            await self._async_send_status_reply(
                imap_entry_id=entry_id,
                sender=sender,
                subject=mail_subject or "",
                results=[result],
                params=mail_params,
            )
            self._record_job(result)
            await self.async_request_refresh()
            return result

        merged_pdf = await self.hass.async_add_executor_job(
            merge_pdf_documents,
            summary.converted,
        )
        effective_booklet = (
            mail_params.booklet
            if mail_params.booklet is not None
            else (
                booklet_override
                if booklet_override is not None
                else any(is_booklet_job(item.filename, self._booklet_patterns) for item in summary.converted)
            )
        )
        result = await self.async_send_print_job(
            job_filename,
            merged_pdf,
            effective_duplex,
            bool(effective_booklet),
            copies=effective_copies,
            collate=effective_collate,
            orientation=effective_orientation,
            media=effective_media,
            raster_dpi=effective_raster_dpi,
            reverse_order=effective_reverse_order,
        )
        result.sender = sender
        result.imap_entry_id = entry_id
        result.imap_uid = uid
        result.imap_part_key = "message"
        result.source_format = "mixed" if len(summary.converted) > 1 else summary.converted[0].source_format
        result.converted_format = "application/pdf"
        result.attachments = summary.filenames
        result.skipped_attachments = list(summary.skipped)
        result.merged_attachment_count = len(summary.converted)
        await self._async_send_status_reply(
            imap_entry_id=entry_id,
            sender=sender,
            subject=mail_subject or "",
            results=[result],
            params=mail_params,
        )
        self._record_job(result)
        await self._async_notify_job(result)
        await self.async_request_refresh()
        return result

    async def _async_convert_message_attachments(
        self,
        entry_id: str,
        uid: str,
        parts: dict[str, dict[str, Any]],
        attachment_filter: str | None,
        attachment_ignore_filter: Any = None,
        allowed_extensions: Any = None,
        ignored_extensions: Any = None,
    ) -> AttachmentConversionSummary:
        """Fetch and convert all printable attachment parts for one message."""
        summary = AttachmentConversionSummary()
        for part_key, part_info in sorted(parts.items()):
            key = str(part_key)
            filename = _part_filename(part_info)
            if not filename:
                continue
            content_type = str(part_info.get("content_type", ""))
            resolved = printable_type(filename, content_type)
            if resolved is None:
                continue
            skip_reason = _filter_skip_reason(
                filename,
                content_type,
                attachment_filter=attachment_filter,
                attachment_ignore_filter=attachment_ignore_filter,
                allowed_extensions=allowed_extensions,
                ignored_extensions=ignored_extensions,
            )
            if skip_reason:
                summary.skipped.append(f"{filename}: {skip_reason}")
                continue
            if not resolved.supported:
                summary.skipped.append(f"{filename}: {resolved.reason or 'unsupported file type'}")
                continue
            try:
                response: dict[str, Any] = await self.hass.services.async_call(
                    "imap",
                    "fetch_part",
                    {"entry": entry_id, "uid": uid, "part": key},
                    blocking=True,
                    return_response=True,
                )
                raw_bytes = _decode_part_payload(response)
                converted = await self.hass.async_add_executor_job(
                    convert_document_to_pdf,
                    raw_bytes,
                    filename,
                    content_type or response.get("content_type"),
                )
            except Exception as exc:
                summary.skipped.append(f"{filename}: {_describe_exception(exc)}")
                continue
            summary.converted.append(converted)
        return summary

    def _message_job_filename(
        self,
        subject: str,
        uid: str,
        summary: AttachmentConversionSummary,
    ) -> str:
        """Return a display filename for a combined message job."""
        if len(summary.converted) == 1:
            return summary.converted[0].filename
        base = sanitize_ipp_job_name(subject or f"email_{uid}_attachments")
        stem, _ext = os.path.splitext(base)
        return f"{stem or 'email_attachments'}.pdf"

    async def async_process_queue_folder(
        self, *, request_refresh: bool = True
    ) -> int:
        """Print stable supported files from the configured queue folder."""
        if not self._is_within_schedule():
            return 0

        candidates = await self.hass.async_add_executor_job(
            self._queue_folder_files,
            True,
        )
        processed = 0
        for candidate in candidates:
            if candidate.path in self._processing_queue_files:
                continue
            if self._processed_queue_files.get(candidate.path) == candidate.mtime:
                continue

            self._processing_queue_files.add(candidate.path)
            try:
                result = await self.async_print_file(
                    candidate.path,
                    request_refresh=False,
                )
                if result.success:
                    processed += 1
                    if self._auto_delete:
                        deleted = await self.hass.async_add_executor_job(
                            self._delete_queue_file,
                            candidate.path,
                        )
                        if deleted:
                            self._processed_queue_files.pop(candidate.path, None)
                        else:
                            self._processed_queue_files[candidate.path] = candidate.mtime
                    else:
                        self._processed_queue_files[candidate.path] = candidate.mtime
                elif (
                    result.skipped_attachments
                    or str(result.error or "").startswith("Cannot read")
                ):
                    self._processed_queue_files[candidate.path] = candidate.mtime
            finally:
                self._processing_queue_files.discard(candidate.path)

        if processed and request_refresh:
            await self.async_request_refresh()
        return processed

    def _queue_folder_files(self, require_stable: bool) -> list[QueueFolderFile]:
        """Return supported queue-folder files sorted by name."""
        try:
            entries = list(os.scandir(self._queue_folder))
        except OSError:
            return []

        now = time.time()
        files: list[QueueFolderFile] = []
        for entry in entries:
            try:
                if not entry.is_file() or not _is_queue_file_name(entry.name):
                    continue
                stat = entry.stat()
            except OSError:
                continue
            if require_stable and now - stat.st_mtime < _QUEUE_FILE_STABLE_SECONDS:
                continue
            files.append(
                QueueFolderFile(
                    path=entry.path,
                    filename=entry.name,
                    mtime=stat.st_mtime,
                )
            )
        return sorted(files, key=lambda item: item.filename.lower())

    async def async_print_file(
        self,
        file_path: str,
        duplex_mode: str | None = None,
        force_booklet: bool = False,
        copies: int | None = None,
        collate: bool | None = None,
        orientation: str | None = None,
        media: str | None = None,
        raster_dpi: int | None = None,
        reverse_order: bool | None = None,
        *,
        request_refresh: bool = True,
    ) -> PrintJobResult:
        """Print a supported file from disk and return the result."""
        filename = os.path.basename(file_path)
        effective_duplex = duplex_mode or self._duplex_mode
        booklet = force_booklet or is_booklet_job(filename, self._booklet_patterns)

        def _read_file() -> bytes | None:
            try:
                with open(file_path, "rb") as f:
                    return f.read()
            except OSError:
                return None

        raw_data = await self.hass.async_add_executor_job(_read_file)
        if raw_data is None:
            result = PrintJobResult(
                filename=filename,
                success=False,
                error=f"Cannot read {file_path}",
                collate=self._resolve_collate(collate),
                reverse_order=self._resolve_reverse_order(reverse_order),
            )
            self._record_job(result)
            if request_refresh:
                await self.async_request_refresh()
            return result

        try:
            converted = await self.hass.async_add_executor_job(
                convert_document_to_pdf,
                raw_data,
                filename,
                None,
            )
        except (UnsupportedDocumentError, DocumentConversionError) as exc:
            error = _describe_exception(exc)
            result = PrintJobResult(
                filename=filename,
                success=False,
                error=error,
                source_format=(
                    printable_type(filename, None).source_format
                    if printable_type(filename, None)
                    else None
                ),
                collate=self._resolve_collate(collate),
                reverse_order=self._resolve_reverse_order(reverse_order),
                skipped_attachments=[f"{filename}: {error}"],
            )
            self._record_job(result)
            if request_refresh:
                await self.async_request_refresh()
            return result

        result = await self.async_send_print_job(
            filename,
            converted.pdf_data,
            effective_duplex,
            booklet,
            copies=copies,
            collate=collate,
            orientation=orientation,
            media=media,
            raster_dpi=raster_dpi,
            reverse_order=reverse_order,
        )
        result.source_format = converted.source_format
        result.converted_format = converted.converted_format
        result.attachments = [filename]
        result.merged_attachment_count = 1
        self._record_job(result)
        if request_refresh:
            await self.async_request_refresh()
        return result

    async def async_print_email(
        self,
        uid: str,
        imap_entry_id: str | None = None,
        duplex: str | None = None,
        booklet: bool = False,
        attachment_filter: str | None = None,
        attachment_ignore_filter: str | None = None,
        allowed_extensions: str | None = None,
        ignored_extensions: str | None = None,
        copies: int | None = None,
        collate: bool | None = None,
        orientation: str | None = None,
        media: str | None = None,
        raster_dpi: int | None = None,
        reverse_order: bool | None = None,
    ) -> dict:
        """Print all printable attachments from one IMAP email by UID."""
        from homeassistant.exceptions import HomeAssistantError

        if not imap_entry_id:
            imap_entry_id = self.selected_imap_entry_id
            if not imap_entry_id:
                raise HomeAssistantError(
                    "No IMAP integration configured. "
                    "Add the HA IMAP integration first."
                )

        try:
            fetch_result: dict = await self.hass.services.async_call(
                "imap",
                "fetch",
                {"entry": imap_entry_id, "uid": uid},
                blocking=True,
                return_response=True,
            )
        except Exception as exc:
            raise HomeAssistantError(
                f"Failed to fetch email uid={uid}: {exc}"
            ) from exc

        subject = str(fetch_result.get("subject", ""))
        body_text = str(fetch_result.get("text", ""))
        sender = _normalise_email_address(str(fetch_result.get("sender", "")))
        parts: dict = fetch_result.get("parts", {})
        printable_parts = {
            k: v for k, v in parts.items()
            if _is_printable_part(str(k), v)
        }

        if not printable_parts:
            raise HomeAssistantError(
                f"No printable attachments found in email uid={uid}. "
                f"Available parts: {list(parts.keys())}"
            )

        result = await self.async_process_imap_message(
            entry_id=imap_entry_id,
            uid=uid,
            parts=parts,
            duplex_override=duplex,
            booklet_override=booklet or None,
            sender=sender,
            attachment_filter=attachment_filter,
            attachment_ignore_filter=attachment_ignore_filter,
            allowed_extensions=allowed_extensions,
            ignored_extensions=ignored_extensions,
            copies=copies,
            collate=collate,
            orientation=orientation,
            media=media,
            raster_dpi=raster_dpi,
            reverse_order=reverse_order,
            mail_subject=subject,
            mail_text=body_text,
        )

        return {
            "uid": uid,
            "printed": result.merged_attachment_count or 0,
            "results": [
                {
                    "filename": result.filename,
                    "success": result.success,
                    "error": result.error,
                    "attachments": list(result.attachments),
                    "skipped_attachments": list(result.skipped_attachments),
                    "copies": result.copies,
                    "collate": result.collate,
                    "reverse_order": result.reverse_order,
                    "reverse_order_applied": result.reverse_order_applied,
                    "status_reply": {
                        "recipient": result.status_reply_recipient,
                        "subject": result.status_reply_subject,
                        "message": result.status_reply_message,
                        "delivery": result.status_reply_delivery,
                    },
                }
            ],
        }

    async def async_check_filter(
        self, imap_entry_id: str | None = None
    ) -> FilterPreviewResult:
        """Connect to IMAP and list emails matching the current filter settings.

        Uses the credentials from an existing HA IMAP config entry.
        Raises HomeAssistantError if no suitable IMAP entry is found.
        """
        from homeassistant.exceptions import HomeAssistantError

        imap_entries = self.hass.config_entries.async_entries("imap")
        if not imap_entries:
            raise HomeAssistantError(
                "No IMAP integration found. "
                "Configure the HA IMAP integration (Settings → Integrations → IMAP) first."
            )

        target = None
        if imap_entry_id is None:
            imap_entry_id = self.selected_imap_entry_id

        if imap_entry_id:
            for e in imap_entries:
                if e.entry_id == imap_entry_id:
                    target = e
                    break
            if target is None:
                raise HomeAssistantError(
                    f"IMAP entry '{imap_entry_id}' not found."
                )
        else:
            target = imap_entries[0]

        data = target.data
        server: str = data.get("server", "")
        port: int = int(data.get("port", 993))
        # HA IMAP integration may store SSL as "ssl" or "use_ssl"
        use_ssl: bool = bool(data.get("ssl", data.get("use_ssl", True)))
        username: str = data.get("username", "")
        password: str = data.get("password", "")
        folder: str = data.get("folder", "INBOX")
        allowed = self._allowed_senders
        folder_filter = self._folder_filter

        # For the preview, search the explicitly configured folders if set;
        # otherwise fall back to the IMAP entry's monitored folder.
        folders_to_search = folder_filter if folder_filter else [folder]

        logger.debug(
            "Running filter preview for %s@%s folders=%s senders=%s",
            username, server, folders_to_search, allowed or "all",
        )

        emails = await self.hass.async_add_executor_job(
            preview_mailbox, server, port, use_ssl, username, password,
            folders_to_search, allowed,
        )

        matching = [e for e in emails if e.matches_filter]
        with_pdf = [e for e in matching if e.has_pdf]
        with_printable = [
            e for e in matching
            if (
                getattr(e, "has_printable", None)
                or (getattr(e, "has_printable", None) is None and e.has_pdf)
            )
        ]

        result = FilterPreviewResult(
            checked_at=datetime.now().isoformat(timespec="seconds"),
            imap_account=f"{username}@{server}",
            imap_entry_id=target.entry_id,
            total_found=len(emails),
            matching=len(matching),
            with_pdf=len(with_pdf),
            with_printable=len(with_printable),
            emails=emails,
        )
        self._filter_preview = result
        await self.async_request_refresh()
        await self._async_notify_filter_preview(result)
        return result

    async def _async_notify_filter_preview(self, result: "FilterPreviewResult") -> None:
        """Show a persistent notification with the filter-preview results."""
        try:
            from homeassistant.components.persistent_notification import async_create as _pn_create

            printable_emails = [
                e for e in result.emails
                if (
                    getattr(e, "has_printable", None)
                    or (getattr(e, "has_printable", None) is None and e.has_pdf)
                )
                and e.matches_filter
            ]

            if not printable_emails:
                body = (
                    f"**Account:** {result.imap_account}  \n"
                    f"**Checked:** {result.checked_at}  \n\n"
                    f"Found **{result.total_found}** email(s), "
                    f"**{result.matching}** match the sender filter, "
                    f"**{result.with_printable or result.with_pdf}** have printable attachments.\n\n"
                    "_No printable emails found. Check your sender/folder filter settings._"
                )
            else:
                rows = "\n".join(
                    f"| `{e.uid}` | {e.subject[:35]} | {e.sender[:25]} | "
                    f"{getattr(e, 'printable_count', None) or e.pdf_count} |"
                    for e in printable_emails[:20]
                )
                more = f"\n_… and {len(printable_emails) - 20} more_" if len(printable_emails) > 20 else ""
                body = (
                    f"**Account:** {result.imap_account}  \n"
                    f"**Checked:** {result.checked_at}  \n\n"
                    f"Found **{result.total_found}** email(s) · "
                    f"**{result.matching}** match filter · "
                    f"**{result.with_printable or result.with_pdf}** have printable attachments\n\n"
                    "| UID | Subject | From | Printable |\n"
                    "|-----|---------|------|:----:|\n"
                    f"{rows}{more}\n\n"
                    "_To print one, call service `print_bridge.print_email` with the UID above._"
                )

            _pn_create(
                self.hass,
                body,
                title="Print Bridge — Mailbox Scan Results",
                notification_id=f"print_bridge_filter_preview_{self._entry.entry_id}",
            )
        except Exception:
            logger.debug("Could not create filter-preview notification", exc_info=True)

    async def _async_delete_queue_pdfs(self) -> int:
        """Delete supported files that are still waiting in the queue folder."""
        folder = self._queue_folder

        def _do_clear() -> int:
            deleted = 0
            try:
                for name in os.listdir(folder):
                    if _is_queue_file_name(name):
                        try:
                            os.remove(os.path.join(folder, name))
                            deleted += 1
                        except OSError:
                            logger.warning("Could not delete %s/%s", folder, name)
            except OSError:
                logger.warning("Could not list queue folder '%s'", folder)
            return deleted

        return await self.hass.async_add_executor_job(_do_clear)

    async def async_clear_queue(self) -> int:
        """Delete all supported queue-folder files."""
        deleted = await self._async_delete_queue_pdfs()
        await self.async_request_refresh()
        return deleted

    async def async_cancel_busy_print_jobs(self) -> int:
        """Discard jobs waiting for a busy printer to become ready."""
        count = len(self._printer_busy_jobs)
        self._printer_busy_jobs.clear()
        if not count:
            return 0
        await self.async_cancel_printer_busy_queue_task()
        await self.async_request_refresh()
        return count

    async def async_cancel_queued_jobs(self) -> int:
        """Discard jobs that Print Bridge has not submitted to the printer yet.

        This clears schedule-held IMAP jobs, printer-busy retry jobs, and
        supported files in the configured queue folder. It cannot recall a job
        once the printer has accepted it.
        """
        pending_count = len(self._pending_jobs)
        self._pending_jobs.clear()
        signal_count = len(self._signal_pending_jobs)
        self._signal_pending_jobs.clear()
        busy_count = await self.async_cancel_busy_print_jobs()
        deleted_files = await self._async_delete_queue_pdfs()
        total = pending_count + signal_count + busy_count + deleted_files
        if total:
            logger.info(
                "Cancelled %d queued job(s): %d schedule-held, %d signal-pending, %d printer-busy, %d file-queue file(s)",
                total,
                pending_count,
                signal_count,
                busy_count,
                deleted_files,
            )
        await self.async_request_refresh()
        return total

    async def async_check_printer_capabilities(
        self, *, force: bool = True
    ) -> PrinterCapabilities:
        """Query the printer's IPP document-format support and cache it."""
        if (
            not force
            and self._printer_capabilities is not None
            and self._capabilities_checked_at is not None
            and datetime.now() - self._capabilities_checked_at < _CAPABILITIES_TTL
        ):
            return self._printer_capabilities

        checked_at_dt = datetime.now()
        checked_at = checked_at_dt.isoformat(timespec="seconds")
        packet = build_get_printer_attributes_packet(self._printer_uri)
        session = async_get_clientsession(self.hass)

        try:
            async with session.post(
                self._ipp_endpoint,
                data=packet,
                headers={"Content-Type": "application/ipp"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                body = await resp.read()
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                ipp_ok, ipp_status = ipp_response_succeeded(body)
                if not ipp_ok:
                    raise RuntimeError(ipp_status)
                attrs = parse_ipp_attributes(body)
        except Exception as exc:
            capabilities = PrinterCapabilities(
                checked_at=checked_at,
                endpoint=self._ipp_endpoint,
                printer_uri=self._printer_uri,
                selected_document_format="application/pdf",
                error=str(exc),
            )
        else:
            document_formats = attrs.get("document-format-supported", [])
            selected_format, conversion_required = self._select_document_format(
                document_formats
            )
            capabilities = PrinterCapabilities(
                checked_at=checked_at,
                endpoint=self._ipp_endpoint,
                printer_uri=self._printer_uri,
                document_formats=document_formats,
                document_format_default=_first_or_none(
                    attrs.get("document-format-default", [])
                ),
                pdf_versions=attrs.get("pdf-versions-supported", []),
                pwg_raster_types=attrs.get(
                    "pwg-raster-document-type-supported", []
                ),
                pwg_raster_resolutions=attrs.get(
                    "pwg-raster-document-resolution-supported", []
                ),
                pwg_sheet_back=_first_or_none(
                    attrs.get("pwg-raster-document-sheet-back", [])
                ),
                sides_supported=attrs.get("sides-supported", []),
                printer_is_accepting_jobs=_first_bool(
                    attrs.get("printer-is-accepting-jobs", [])
                ),
                printer_state=_first_or_none(attrs.get("printer-state", [])),
                printer_state_reasons=attrs.get("printer-state-reasons", []),
                queued_job_count=_first_int(attrs.get("queued-job-count", [])),
                selected_document_format=selected_format,
                conversion_required=conversion_required,
            )

        self._printer_capabilities = capabilities
        self._capabilities_checked_at = checked_at_dt
        if self.data is not None:
            self.async_set_updated_data(
                AutoPrintData(
                    queue_depth=self.data.queue_depth,
                    printer_online=self.data.printer_online,
                    last_job=self.data.last_job,
                    job_history=list(self.data.job_history),
                    total_jobs_sent=self.data.total_jobs_sent,
                    filter_preview=self.data.filter_preview,
                    printer_capabilities=capabilities,
                    pending_jobs=list(self.data.pending_jobs),
                    printer_busy_jobs=list(self._printer_busy_jobs),
                    signal_pending_jobs=list(self._signal_pending_jobs),
                    signal_groups=list(self._signal_groups),
                    signal_groups_checked_at=self._signal_groups_checked_at,
                )
            )
        return capabilities

    async def async_send_print_job(
        self,
        filename: str,
        pdf_data: bytes,
        duplex_mode: str,
        booklet: bool,
        copies: int | None = None,
        collate: bool | None = None,
        orientation: str | None = None,
        media: str | None = None,
        raster_dpi: int | None = None,
        reverse_order: bool | None = None,
        _queue_on_busy: bool = True,
    ) -> PrintJobResult:
        """Build an IPP packet and POST it to CUPS."""
        filename = sanitize_ipp_job_name(filename)
        effective_raster_dpi = _normalise_raster_dpi(
            raster_dpi if raster_dpi is not None else self._raster_dpi
        )
        effective_orientation = _orientation_for_job(booklet, orientation)
        orientation_requested = (
            None
            if booklet
            else (
                _ORIENTATION_ENUMS[effective_orientation]
                if effective_orientation is not None
                else None
            )
        )
        effective_copies = copies or 1
        effective_collate = self._resolve_collate(collate)
        effective_media = media
        source_pdf_data = pdf_data
        effective_reverse_order = self._resolve_reverse_order(reverse_order)
        reverse_order_applied = False
        if booklet:
            duplex_mode = "two-sided-short-edge"
        if booklet:
            try:
                pdf_data = await self.hass.async_add_executor_job(
                    create_booklet, pdf_data
                )
                effective_media = effective_media or _media_for_pdf_page_size(pdf_data)
            except Exception as exc:
                error = _describe_exception(exc)
                logger.error("Booklet conversion failed for '%s': %s", filename, error)
                return PrintJobResult(
                    filename=filename, success=False, error=error,
                    duplex=duplex_mode, booklet=booklet,
                    copies=effective_copies, collate=effective_collate,
                    orientation=effective_orientation,
                    media=effective_media, raster_dpi=effective_raster_dpi,
                    reverse_order=effective_reverse_order,
                    reverse_order_applied=reverse_order_applied,
                    status=error,
                )

        sides = determine_sides(duplex_mode, booklet)
        if effective_reverse_order and sides == "one-sided" and not booklet:
            try:
                pdf_data = await self.hass.async_add_executor_job(
                    reverse_pdf_pages, pdf_data
                )
                reverse_order_applied = True
            except Exception as exc:
                error = _describe_exception(exc)
                logger.error("Page-order reversal failed for '%s': %s", filename, error)
                return PrintJobResult(
                    filename=filename,
                    success=False,
                    error=error,
                    duplex=duplex_mode,
                    booklet=booklet,
                    copies=effective_copies,
                    collate=effective_collate,
                    orientation=effective_orientation,
                    media=effective_media,
                    raster_dpi=effective_raster_dpi,
                    reverse_order=effective_reverse_order,
                    reverse_order_applied=False,
                    sides=sides,
                    status=error,
                )
        try:
            document_format, document_data = await self._async_prepare_document_for_printing(
                pdf_data, sides, raster_dpi=effective_raster_dpi
            )
        except Exception as exc:
            error = _describe_exception(exc)
            logger.error("Document conversion failed for '%s': %s", filename, error)
            return PrintJobResult(
                filename=filename, success=False, error=error,
                duplex=duplex_mode, booklet=booklet,
                copies=effective_copies, collate=effective_collate,
                orientation=effective_orientation,
                media=effective_media, raster_dpi=effective_raster_dpi,
                reverse_order=effective_reverse_order,
                reverse_order_applied=reverse_order_applied,
                sides=sides, status=error,
            )
        reported_raster_dpi = (
            effective_raster_dpi
            if document_format in {"image/pwg-raster", "image/jpeg"}
            else None
        )

        packet = build_ipp_packet(
            self._printer_uri,
            filename,
            sides,
            document_data,
            document_format=document_format,
            copies=copies,
            collate=effective_collate if effective_copies > 1 else None,
            orientation_requested=(
                None if booklet else orientation_requested
            ),
            media=effective_media,
            print_scaling="fit" if booklet else None,
        )

        session = async_get_clientsession(self.hass)
        try:
            async with session.post(
                self._ipp_endpoint,
                data=packet,
                headers={"Content-Type": "application/ipp"},
                timeout=aiohttp.ClientTimeout(
                    total=_PRINT_JOB_TIMEOUT_SECONDS,
                    sock_connect=30,
                ),
            ) as resp:
                body = await resp.read()
                body_prefix = body[:256].lstrip().lower()
                if resp.status != 200:
                    error = f"HTTP {resp.status}"
                    logger.error("CUPS rejected job for '%s': %s", filename, error)
                    return PrintJobResult(
                        filename=filename, success=False, error=error,
                        duplex=duplex_mode, booklet=booklet,
                        copies=effective_copies, collate=effective_collate,
                        orientation=effective_orientation,
                        media=effective_media, raster_dpi=reported_raster_dpi,
                        reverse_order=effective_reverse_order,
                        reverse_order_applied=reverse_order_applied,
                        sides=sides, document_format=document_format,
                        status_code=error, status=error,
                    )

                if body_prefix.startswith(b"<!doctype html") or body_prefix.startswith(b"<html"):
                    error = "HTTP 200 with HTML response"
                    logger.error("CUPS rejected job for '%s': %s", filename, error)
                    return PrintJobResult(
                        filename=filename, success=False, error=error,
                        duplex=duplex_mode, booklet=booklet,
                        copies=effective_copies, collate=effective_collate,
                        orientation=effective_orientation,
                        media=effective_media, raster_dpi=reported_raster_dpi,
                        reverse_order=effective_reverse_order,
                        reverse_order_applied=reverse_order_applied,
                        sides=sides, document_format=document_format,
                        status_code="HTTP 200", status=error,
                    )

                status_code, ipp_status = parse_ipp_response_status(body)
                ipp_ok = status_code is not None and status_code < 0x0100
                ipp_status_code = (
                    f"IPP 0x{status_code:04x}" if status_code is not None else None
                )
                if ipp_ok:
                    logger.debug(
                        "Print job accepted for '%s' (format=%s, sides=%s, %s)",
                        filename, document_format, sides, ipp_status,
                    )
                    return PrintJobResult(
                        filename=filename, success=True,
                        duplex=duplex_mode, booklet=booklet,
                        copies=effective_copies, collate=effective_collate,
                        orientation=effective_orientation,
                        media=effective_media, raster_dpi=reported_raster_dpi,
                        reverse_order=effective_reverse_order,
                        reverse_order_applied=reverse_order_applied,
                        sides=sides, document_format=document_format,
                        status_code=ipp_status_code, status=ipp_status,
                    )

                error = ipp_status
                if status_code in _PRINTER_BUSY_STATUS_CODES and _queue_on_busy:
                    busy_job = BusyPrintJob(
                        filename=filename,
                        pdf_data=source_pdf_data,
                        duplex_mode=duplex_mode,
                        booklet=booklet,
                        copies=copies,
                        collate=collate,
                        orientation=orientation,
                        media=media,
                        raster_dpi=raster_dpi,
                        reverse_order=effective_reverse_order,
                    )
                    self._queue_printer_busy_job(busy_job)
                    queued_status = (
                        "printer busy; queued for retry when printer is ready"
                    )
                    logger.warning(
                        "Printer reported busy for '%s'; queued for retry",
                        filename,
                    )
                    return PrintJobResult(
                        filename=filename,
                        success=True,
                        duplex=duplex_mode,
                        booklet=booklet,
                        copies=effective_copies,
                        collate=effective_collate,
                        orientation=effective_orientation,
                        media=effective_media,
                        raster_dpi=reported_raster_dpi,
                        reverse_order=effective_reverse_order,
                        reverse_order_applied=reverse_order_applied,
                        sides=sides,
                        document_format=document_format,
                        status_code="queued-busy",
                        status=queued_status,
                    )
                logger.error("IPP rejected job for '%s': %s", filename, error)
                return PrintJobResult(
                    filename=filename, success=False, error=error,
                    duplex=duplex_mode, booklet=booklet,
                    copies=effective_copies, collate=effective_collate,
                    orientation=effective_orientation,
                    media=effective_media, raster_dpi=reported_raster_dpi,
                    reverse_order=effective_reverse_order,
                    reverse_order_applied=reverse_order_applied,
                    sides=sides, document_format=document_format,
                    status_code=ipp_status_code, status=ipp_status,
                )

        except asyncio.TimeoutError as exc:
            error = (
                f"{_describe_exception(exc)} while POSTing to {self._ipp_endpoint} "
                f"(timeout={_PRINT_JOB_TIMEOUT_SECONDS}s)"
            )
            if self._is_direct_mode:
                logger.warning(
                    "Timed out waiting for direct IPP response for '%s'; "
                    "treating as submitted because some printers print without "
                    "returning a final response: %s",
                    filename,
                    error,
                )
                return PrintJobResult(
                    filename=filename,
                    success=True,
                    error=f"submitted; {error}",
                    duplex=duplex_mode,
                    booklet=booklet,
                    copies=effective_copies,
                    collate=effective_collate,
                    orientation=effective_orientation,
                    media=effective_media,
                    raster_dpi=reported_raster_dpi,
                    reverse_order=effective_reverse_order,
                    reverse_order_applied=reverse_order_applied,
                    sides=sides,
                    document_format=document_format,
                    status_code="timeout-submitted",
                    status=error,
                )
            logger.error("Network error printing '%s': %s", filename, error)
            return PrintJobResult(
                filename=filename, success=False, error=error,
                duplex=duplex_mode, booklet=booklet,
                copies=effective_copies, collate=effective_collate,
                orientation=effective_orientation,
                media=effective_media, raster_dpi=reported_raster_dpi,
                reverse_order=effective_reverse_order,
                reverse_order_applied=reverse_order_applied,
                sides=sides, document_format=document_format,
                status_code="timeout", status=error,
            )
        except aiohttp.ClientError as exc:
            error = (
                f"{_describe_exception(exc)} while POSTing to {self._ipp_endpoint} "
                f"(timeout={_PRINT_JOB_TIMEOUT_SECONDS}s)"
            )
            logger.error("Network error printing '%s': %s", filename, error)
            return PrintJobResult(
                filename=filename, success=False, error=error,
                duplex=duplex_mode, booklet=booklet,
                copies=effective_copies, collate=effective_collate,
                orientation=effective_orientation,
                media=effective_media, raster_dpi=reported_raster_dpi,
                reverse_order=effective_reverse_order,
                reverse_order_applied=reverse_order_applied,
                sides=sides, document_format=document_format,
                status_code="network-error", status=error,
            )

    def _queue_printer_busy_job(self, job: BusyPrintJob) -> None:
        """Queue a job rejected because the printer reported server-error-busy."""
        self._printer_busy_jobs.append(job)
        self._start_printer_busy_queue()
        if self.data is not None:
            self.async_set_updated_data(
                AutoPrintData(
                    queue_depth=self.data.queue_depth,
                    printer_online=self.data.printer_online,
                    last_job=self.data.last_job,
                    job_history=list(self.data.job_history),
                    total_jobs_sent=self.data.total_jobs_sent,
                    filter_preview=self.data.filter_preview,
                    printer_capabilities=self.data.printer_capabilities,
                    pending_jobs=list(self.data.pending_jobs),
                    printer_busy_jobs=list(self._printer_busy_jobs),
                    signal_pending_jobs=list(self._signal_pending_jobs),
                    signal_groups=list(self._signal_groups),
                    signal_groups_checked_at=self._signal_groups_checked_at,
                )
            )

    def _start_printer_busy_queue(self) -> None:
        """Start the background busy-printer retry loop if needed."""
        if (
            self._printer_busy_queue_task is not None
            and not self._printer_busy_queue_task.done()
        ):
            return
        self._printer_busy_queue_task = self.hass.async_create_task(
            self._async_printer_busy_queue_loop()
        )

    async def async_cancel_printer_busy_queue_task(self) -> None:
        """Cancel the background busy-printer queue task during unload."""
        if (
            self._printer_busy_queue_task is None
            or self._printer_busy_queue_task.done()
        ):
            return
        self._printer_busy_queue_task.cancel()
        try:
            await self._printer_busy_queue_task
        except asyncio.CancelledError:
            pass
        finally:
            self._printer_busy_queue_task = None

    async def _async_printer_busy_queue_loop(self) -> None:
        """Poll printer readiness and resend jobs rejected as server-error-busy."""
        try:
            while self._printer_busy_jobs:
                await self._async_wait_until_printer_ready_for_retry()
                if not self._printer_busy_jobs:
                    break

                job = self._printer_busy_jobs.pop(0)
                job.attempts += 1
                result = await self.async_send_print_job(
                    job.filename,
                    job.pdf_data,
                    job.duplex_mode,
                    job.booklet,
                    copies=job.copies,
                    collate=job.collate,
                    orientation=job.orientation,
                    media=job.media,
                    raster_dpi=job.raster_dpi,
                    reverse_order=job.reverse_order,
                    _queue_on_busy=False,
                )
                if result.status_code in {"IPP 0x0506", "IPP 0x0507"}:
                    if job.attempts >= _PRINTER_BUSY_QUEUE_MAX_ATTEMPTS:
                        result.error = (
                            result.error or "printer busy retry limit reached"
                        )
                        self._record_job(result)
                        await self._async_notify_job(result)
                    else:
                        self._printer_busy_jobs.insert(0, job)
                        await self.async_request_refresh()
                        await asyncio.sleep(_PRINTER_BUSY_POLL_INTERVAL_SECONDS)
                    continue

                self._record_job(result)
                await self._async_notify_job(result)
                await self.async_request_refresh()
        finally:
            if self._printer_busy_queue_task is asyncio.current_task():
                self._printer_busy_queue_task = None

    async def _async_wait_until_printer_ready_for_retry(self) -> None:
        """Poll Get-Printer-Attributes until the printer looks ready."""
        while self._printer_busy_jobs:
            if await self._async_printer_ready_for_retry():
                return
            await self.async_request_refresh()
            await asyncio.sleep(_PRINTER_BUSY_POLL_INTERVAL_SECONDS)

    async def _async_printer_ready_for_retry(self) -> bool:
        """Return True when a busy-queued job should be retried."""
        capabilities = await self.async_check_printer_capabilities(force=True)
        if capabilities.error:
            return await self._async_check_printer_online()
        if capabilities.printer_is_accepting_jobs is False:
            return False
        if capabilities.printer_state in {"processing", "stopped"}:
            return False
        if capabilities.queued_job_count is not None and capabilities.queued_job_count > 0:
            return False
        return True

    def _select_document_format(self, document_formats: list[str]) -> tuple[str, bool]:
        """Choose the document format Print Bridge should send."""
        if "application/pdf" in document_formats or not self._is_direct_mode:
            return "application/pdf", False
        if "image/pwg-raster" in document_formats:
            return "image/pwg-raster", True
        if "image/jpeg" in document_formats:
            return "image/jpeg", True
        return "application/pdf", False

    async def _async_prepare_document_for_printing(
        self, pdf_data: bytes, sides: str, raster_dpi: int | None = None
    ) -> tuple[str, bytes]:
        """Return document-format and payload bytes accepted by this printer."""
        if not self._is_direct_mode:
            return "application/pdf", pdf_data

        capabilities = await self.async_check_printer_capabilities(force=False)
        document_format = capabilities.selected_document_format or "application/pdf"
        if document_format == "application/pdf":
            return document_format, pdf_data
        if document_format == "image/pwg-raster":
            color_type = (
                "srgb_8"
                if "srgb_8" in capabilities.pwg_raster_types
                else "sgray_8"
            )
            dpi = _normalise_raster_dpi(
                raster_dpi if raster_dpi is not None else self._raster_dpi
            )
            raster_data = await self.hass.async_add_executor_job(
                partial(
                    convert_pdf_to_pwg_raster,
                    pdf_data,
                    sides,
                    dpi=dpi,
                    color_type=color_type,
                    sheet_back=capabilities.pwg_sheet_back,
                )
            )
            return document_format, raster_data
        if document_format == "image/jpeg":
            dpi = _normalise_raster_dpi(
                raster_dpi if raster_dpi is not None else self._raster_dpi
            )
            jpeg_data = await self.hass.async_add_executor_job(
                partial(convert_pdf_to_jpeg, pdf_data, dpi=dpi)
            )
            return document_format, jpeg_data
        raise ValueError(
            "Printer does not support PDF or a built-in convertible format "
            f"(supported: {', '.join(capabilities.document_formats) or 'unknown'})"
        )

    # ------------------------------------------------------------------
    # Coordinator periodic update (printer status only)
    # ------------------------------------------------------------------

    async def async_flush_pending(self) -> int:
        """Print all pending (schedule-queued) jobs immediately.

        Returns the number of jobs dispatched.
        """
        if not self._pending_jobs:
            return 0

        jobs = list(self._pending_jobs)
        self._pending_jobs.clear()
        logger.info("Flushing %d pending job(s) from schedule queue", len(jobs))

        for job in jobs:
            if job.message_level or job.part_key is None:
                result = await self.async_process_imap_message(
                    entry_id=job.entry_id,
                    uid=job.uid,
                    duplex_override=job.duplex_override,
                    booklet_override=job.booklet_override,
                    sender=job.sender,
                    copies=job.copies,
                    collate=job.collate,
                    orientation=job.orientation,
                    media=job.media,
                    raster_dpi=job.raster_dpi,
                    reverse_order=job.reverse_order,
                    mail_subject=job.mail_subject,
                    mail_text=job.mail_text,
                )
            else:
                result = await self._async_fetch_and_print(
                    entry_id=job.entry_id,
                    uid=job.uid,
                    part_key=job.part_key,
                    filename=job.filename,
                    duplex_override=job.duplex_override,
                    booklet_override=job.booklet_override,
                    sender=job.sender,
                    copies=job.copies,
                    collate=job.collate,
                    orientation=job.orientation,
                    media=job.media,
                    raster_dpi=job.raster_dpi,
                    reverse_order=job.reverse_order,
                )
            # Apply configured IMAP action now that the job has been printed.
            if result.success and (result.merged_attachment_count or 0) > 0:
                await self._async_post_process_email(job.entry_id, job.uid)
            if not (job.message_level or job.part_key is None):
                await self._async_send_status_reply(
                    imap_entry_id=job.entry_id,
                    sender=job.sender,
                    subject=job.mail_subject,
                    results=[result],
                    params=job.mail_params,
                )
                self._record_job(result)
                await self._async_notify_job(result)

        await self.async_request_refresh()
        return len(jobs)

    async def _async_update_data(self) -> AutoPrintData:
        """Check printer state, process folder intake, and flush scheduled jobs."""
        await self.async_sync_signal_receiver()
        self._expire_signal_pending_jobs()
        printer_online = await self._async_check_printer_online()

        # Auto-flush pending jobs when the print window opens.
        currently_open = self._is_within_schedule()
        if (
            self._pending_jobs
            and currently_open
            and self._last_schedule_state is not True  # None (startup) or False triggers flush
        ):
            logger.info("Print window opened — flushing %d pending job(s)", len(self._pending_jobs))
            await self.async_flush_pending()

        if self._printer_busy_jobs:
            self._start_printer_busy_queue()

        if self._auto_print_enabled and currently_open:
            await self.async_process_queue_folder(request_refresh=False)

        self._last_schedule_state = currently_open
        queue_depth = await self.hass.async_add_executor_job(self._count_queue_files)

        last_job = self._job_history[0] if self._job_history else None
        return AutoPrintData(
            queue_depth=queue_depth,
            printer_online=printer_online,
            last_job=last_job,
            job_history=list(self._job_history),
            total_jobs_sent=self._total_jobs_sent,
            filter_preview=self._filter_preview,
            printer_capabilities=self._printer_capabilities,
            pending_jobs=list(self._pending_jobs),
            printer_busy_jobs=list(self._printer_busy_jobs),
            signal_pending_jobs=list(self._signal_pending_jobs),
            signal_groups=list(self._signal_groups),
            signal_groups_checked_at=self._signal_groups_checked_at,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _async_check_printer_online(self) -> bool:
        """Return True if the printer or CUPS server is reachable."""
        check_url = self._ipp_endpoint if self._is_direct_mode else self._cups_url
        session = async_get_clientsession(self.hass)
        try:
            async with session.head(
                check_url, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                return resp.status < 500
        except Exception:
            return False

    def _count_queue_files(self) -> int:
        return len(self._queue_folder_files(False))

    @staticmethod
    def _delete_queue_file(path: str) -> bool:
        try:
            os.remove(path)
            return True
        except OSError:
            logger.warning("Could not delete queue file '%s'", path)
            return False

    def _record_job(self, result: PrintJobResult) -> None:
        """Prepend result to history and fire an audit event."""
        self._job_history = ([result] + self._job_history)[:50]
        self._total_jobs_sent += 1

        # Fire to HA event bus → appears in Logbook via logbook.py descriptor.
        self.hass.bus.async_fire(
            EVENT_JOB_COMPLETED,
            {
                "entry_id": self._entry.entry_id,
                "printer": self._printer_name,
                "filename": result.filename,
                "success": result.success,
                "error": result.error,
                "sender": result.sender,
                "duplex": result.duplex,
                "booklet": result.booklet,
                "copies": result.copies,
                "collate": result.collate,
                "orientation": result.orientation,
                "media": result.media,
                "raster_dpi": result.raster_dpi,
                "reverse_order": result.reverse_order,
                "reverse_order_applied": result.reverse_order_applied,
                "sides": result.sides,
                "document_format": result.document_format,
                "status_code": result.status_code,
                "status": result.status,
                "source_format": result.source_format,
                "converted_format": result.converted_format,
                "attachments": list(result.attachments),
                "skipped_attachments": list(result.skipped_attachments),
                "merged_attachment_count": result.merged_attachment_count,
                "status_reply_recipient": result.status_reply_recipient,
                "status_reply_subject": result.status_reply_subject,
                "status_reply_message": result.status_reply_message,
                "status_reply_delivery": result.status_reply_delivery,
                "source": result.source,
                "signal_job_id": result.signal_job_id,
                "signal_group_id": result.signal_group_id,
                "signal_group_name": result.signal_group_name,
                "timestamp": result.timestamp,
            },
        )
