"""DataUpdateCoordinator for the Print Bridge integration.

Responsibilities:
  - Listen for imap_content events fired by HA's built-in IMAP integration.
  - For each PDF attachment, call imap.fetch_part to retrieve the bytes.
  - Optionally reorder pages for booklet printing.
  - Send the print job to CUPS via a raw IPP/2.0 request (aiohttp).
  - Fire print_bridge_job_completed events → HA Logbook audit trail.
  - Periodically check printer reachability and count queued files.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from dataclasses import dataclass, field
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
from .const import (
    CONF_ALLOWED_SENDERS,
    CONF_AUTO_DELETE,
    CONF_BOOKLET_PATTERNS,
    CONF_CUPS_URL,
    CONF_DIRECT_PRINTER_URL,
    CONF_DUPLEX_MODE,
    CONF_EMAIL_ACTION,
    CONF_EMAIL_ARCHIVE_FOLDER,
    CONF_FOLDER_FILTER,
    CONF_NOTIFY_ON_FAILURE,
    CONF_NOTIFY_ON_SUCCESS,
    CONF_PRINTER_NAME,
    CONF_QUEUE_FOLDER,
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
    DEFAULT_DUPLEX_MODE,
    DEFAULT_EMAIL_ACTION,
    DEFAULT_EMAIL_ARCHIVE_FOLDER,
    DEFAULT_NOTIFY_ON_FAILURE,
    DEFAULT_NOTIFY_ON_SUCCESS,
    DEFAULT_QUEUE_FOLDER,
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
)
from .imap_checker import EmailPreview, preview_mailbox
from .mail_params import MailPrintParameters, parse_mail_print_parameters
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

logger = logging.getLogger(__name__)

_STATUS_INTERVAL = timedelta(minutes=5)
_CAPABILITIES_TTL = timedelta(hours=1)
_PRINT_JOB_TIMEOUT_SECONDS = 300
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


def _split_notify_service(value: str) -> tuple[str, str]:
    """Return (domain, service) for a notify service reference."""
    service_ref = value.strip()
    if "." in service_ref:
        domain, service = service_ref.split(".", 1)
        return domain.strip(), service.strip()
    return "notify", service_ref


def _is_pdf_part(part_info: dict[str, Any]) -> bool:
    """Return True if an IMAP part metadata dict describes a PDF attachment."""
    content_type = str(part_info.get("content_type", ""))
    return content_type.split(";", 1)[0].strip().lower() == "application/pdf"


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


def _first_or_none(values: list[str]) -> str | None:
    return values[0] if values else None


def _orientation_for_job(booklet: bool, orientation: str | None) -> str | None:
    """Return the effective job orientation keyword."""
    if booklet:
        return "landscape"
    if orientation in _ORIENTATION_ENUMS:
        return orientation
    return None


def _resolution_dpi(values: list[str]) -> int:
    for value in values:
        match = re.match(r"^(\d+)(?:x\d+)?dpi$", value)
        if match:
            return int(match.group(1))
    return 300


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
    orientation: str | None = None
    media: str | None = None
    sides: str | None = None
    document_format: str | None = None
    status_code: str | None = None
    status: str | None = None
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
    part_key: str
    filename: str
    sender: str | None = None
    duplex_override: str | None = None
    booklet_override: bool | None = None
    copies: int | None = None
    orientation: str | None = None
    media: str | None = None
    mail_subject: str = ""
    mail_params: MailPrintParameters = field(default_factory=MailPrintParameters)
    queued_at: str = field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds")
    )

    def as_dict(self) -> dict:
        return {
            "filename": self.filename,
            "sender": self.sender,
            "queued_at": self.queued_at,
            "uid": self.uid,
            "duplex": self.duplex_override,
            "booklet": self.booklet_override,
            "copies": self.copies,
            "orientation": self.orientation,
            "media": self.media,
        }


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
    def _allowed_senders(self) -> list[str]:
        senders: list[str] = []
        for sender in self._entry.options.get(CONF_ALLOWED_SENDERS, []):
            normalised = _normalise_email_address(sender)
            if normalised:
                senders.append(normalised)
        return senders

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
        mail_params = parse_mail_print_parameters(
            subject,
            str(event.data.get("text", "")),
        )

        had_pdf = False
        actually_printed = False  # tracks whether any part was immediately printed
        results: list[PrintJobResult] = []
        for part_key, part_info in parts.items():
            if not _is_pdf_part(part_info):
                continue

            had_pdf = True
            filename: str = _decode_mime_filename(
                part_info.get("filename")
                or part_info.get("file_name")
                or f"document_{part_key}.pdf"
            )
            if mail_params.attachment_filter:
                if mail_params.attachment_filter.lower() not in filename.lower():
                    continue

            # Schedule check — queue the job if outside the allowed window.
            if not self._is_within_schedule():
                pending = PendingJob(
                    entry_id=entry_id,
                    uid=uid,
                    part_key=part_key,
                    filename=filename,
                    sender=sender,
                    duplex_override=mail_params.duplex,
                    booklet_override=mail_params.booklet,
                    copies=mail_params.copies,
                    orientation=mail_params.orientation,
                    media=mail_params.media,
                    mail_subject=subject,
                    mail_params=mail_params,
                )
                self._pending_jobs.append(pending)
                logger.info(
                    "Job '%s' queued — outside print schedule (%s–%s)",
                    filename, self._schedule_start, self._schedule_end,
                )
                try:
                    await self.hass.services.async_call(
                        "persistent_notification", "create",
                        {
                            "title": "Print Bridge — Job queued",
                            "message": (
                                f"**{filename}** from {sender or 'unknown'} was received "
                                f"outside the print schedule ({self._schedule_start}–"
                                f"{self._schedule_end}) and will be printed when the "
                                f"window opens."
                            ),
                            "notification_id": f"print_bridge_queued_{entry_id}_{uid}",
                        },
                    )
                except Exception:
                    pass
                continue  # don't print now

            actually_printed = True
            result = await self._async_fetch_and_print(
                entry_id=entry_id,
                uid=uid,
                part_key=part_key,
                filename=filename,
                sender=sender,
                duplex_override=mail_params.duplex,
                booklet_override=mail_params.booklet,
                copies=mail_params.copies,
                orientation=mail_params.orientation,
                media=mail_params.media,
            )
            results.append(result)
            self._record_job(result)
            await self._async_notify_job(result)

        # Only post-process the email after it has been printed immediately.
        # If all parts were queued by the schedule, post-processing is deferred
        # to async_flush_pending (to avoid deleting the email before printing).
        if actually_printed:
            await self._async_post_process_email(entry_id, uid)
            await self._async_send_status_reply(
                sender=sender,
                subject=subject,
                results=results,
                params=mail_params,
            )

        if parts:
            await self.async_request_refresh()

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
        orientation: str | None = None,
        media: str | None = None,
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
                imap_entry_id=entry_id, imap_uid=uid, imap_part_key=part_key,
            )

        try:
            raw: str = response["part_data"]
            encoding: str = response.get("content_transfer_encoding", "base64").lower()
            if encoding == "base64":
                pdf_bytes = base64.b64decode(raw)
            else:
                pdf_bytes = (
                    raw.encode("latin-1") if isinstance(raw, str) else bytes(raw)
                )
        except Exception as exc:
            logger.error("Decoding attachment '%s' failed: %s", filename, exc)
            return PrintJobResult(
                filename=filename, success=False, error=str(exc),
                sender=sender,
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
            pdf_bytes,
            effective_duplex,
            effective_booklet,
            copies=copies,
            orientation=orientation,
            media=media,
        )
        # Attach IMAP identifiers for future retry.
        result.sender = sender
        result.imap_entry_id = entry_id
        result.imap_uid = uid
        result.imap_part_key = part_key
        return result

    # ------------------------------------------------------------------
    # Retry
    # ------------------------------------------------------------------

    async def async_retry_job(
        self,
        job: PrintJobResult,
        duplex_override: str | None = None,
        booklet_override: bool | None = None,
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
        result = await self._async_fetch_and_print(
            entry_id=job.imap_entry_id,       # type: ignore[arg-type]
            uid=job.imap_uid,                  # type: ignore[arg-type]
            part_key=job.imap_part_key,        # type: ignore[arg-type]
            filename=job.filename,
            duplex_override=duplex_override or job.duplex,
            booklet_override=booklet_override if booklet_override is not None else job.booklet,
            sender=job.sender,
            copies=job.copies,
            orientation=job.orientation,
            media=job.media,
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

        if result.success:
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
        sender: str | None,
        subject: str,
        results: list[PrintJobResult],
        params: MailPrintParameters,
    ) -> None:
        """Send a status email through a configured HA notify service."""
        if params.reply is False:
            return
        if not (self._status_reply_enabled or params.reply is True):
            return
        if not sender:
            logger.warning("Cannot send print status reply: sender address is empty")
            return

        service_ref = self._status_reply_notify_service
        if not service_ref:
            logger.warning(
                "Cannot send print status reply to %s: status reply notify service is not configured",
                sender,
            )
            return

        domain, service = _split_notify_service(service_ref)
        message = self._format_status_reply(results, params)
        try:
            await self.hass.services.async_call(
                domain,
                service,
                {
                    "title": f"Re: {subject or 'Print Bridge status'}",
                    "message": message,
                    "target": [sender],
                },
                blocking=True,
            )
        except Exception as exc:
            logger.warning(
                "Status reply via %s.%s to %s failed: %s",
                domain,
                service,
                sender,
                exc,
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
                + ", ".join(f"{key}={value}" for key, value in params.as_dict().items())
            )
        lines.append("")

        for index, result in enumerate(results, start=1):
            lines.extend(
                [
                    f"Job {index}: {result.filename}",
                    f"Result: {'success' if result.success else 'failed'}",
                    f"Status code: {result.status_code or 'n/a'}",
                    f"Status: {result.status or result.error or 'accepted'}",
                    f"Document format: {result.document_format or 'application/pdf'}",
                    f"Duplex: {result.duplex or 'default'}",
                    f"IPP sides: {result.sides or 'default'}",
                    f"Booklet: {'yes' if result.booklet else 'no'}",
                    f"Copies: {result.copies or 1}",
                    f"Orientation: {result.orientation or 'default'}",
                    f"Media: {result.media or 'default'}",
                    f"Timestamp: {result.timestamp}",
                    "",
                ]
            )
        return "\n".join(lines).strip()

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
        copies: int | None = None,
        orientation: str | None = None,
        media: str | None = None,
        mail_subject: str | None = None,
        mail_text: str | None = None,
    ) -> PrintJobResult:
        """Fetch one IMAP attachment and print it (called by the service)."""
        mail_params = parse_mail_print_parameters(mail_subject or "", mail_text or "")
        effective_duplex_override = mail_params.duplex or duplex_override
        effective_booklet_override = (
            mail_params.booklet if mail_params.booklet is not None else booklet_override
        )
        effective_attachment_filter = mail_params.attachment_filter or attachment_filter
        effective_copies = mail_params.copies or copies
        effective_orientation = mail_params.orientation or orientation
        effective_media = mail_params.media or media

        # Decode RFC 2047 MIME-encoded filenames that arrive from the IMAP event.
        effective_filename = _decode_mime_filename(
            filename or f"attachment_{part_key}.pdf"
        )

        # Skip this attachment if it doesn't match the caller's name filter.
        if effective_attachment_filter and effective_attachment_filter.strip():
            if effective_attachment_filter.strip().lower() not in effective_filename.lower():
                logger.debug(
                    "Skipping attachment '%s' — does not match filter '%s'",
                    effective_filename, effective_attachment_filter,
                )
                return PrintJobResult(
                    filename=effective_filename, success=True,
                    error=f"skipped: does not match filter '{effective_attachment_filter}'",
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
            orientation=effective_orientation,
            media=effective_media,
        )
        self._record_job(result)
        await self._async_send_status_reply(
            sender=sender,
            subject=mail_subject or "",
            results=[result],
            params=mail_params,
        )
        await self.async_request_refresh()
        return result

    async def async_print_file(
        self,
        file_path: str,
        duplex_mode: str | None = None,
        force_booklet: bool = False,
        copies: int | None = None,
        orientation: str | None = None,
        media: str | None = None,
    ) -> PrintJobResult:
        """Print a PDF file from disk and return the result."""
        filename = os.path.basename(file_path)
        effective_duplex = duplex_mode or self._duplex_mode
        booklet = force_booklet or is_booklet_job(filename, self._booklet_patterns)

        def _read_file() -> bytes | None:
            try:
                with open(file_path, "rb") as f:
                    return f.read()
            except OSError:
                return None

        pdf_data = await self.hass.async_add_executor_job(_read_file)
        if pdf_data is None:
            result = PrintJobResult(filename=filename, success=False, error=f"Cannot read {file_path}")
            self._record_job(result)
            return result

        result = await self.async_send_print_job(
            filename,
            pdf_data,
            effective_duplex,
            booklet,
            copies=copies,
            orientation=orientation,
            media=media,
        )
        self._record_job(result)
        await self.async_request_refresh()
        return result

    async def async_print_email(
        self,
        uid: str,
        imap_entry_id: str | None = None,
        duplex: str | None = None,
        booklet: bool = False,
        attachment_filter: str | None = None,
        copies: int | None = None,
        orientation: str | None = None,
        media: str | None = None,
    ) -> dict:
        """Print all PDF attachments from one IMAP email by UID."""
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
        pdf_parts = {
            k: v for k, v in parts.items()
            if _is_pdf_part(v)
        }

        if not pdf_parts:
            raise HomeAssistantError(
                f"No PDF attachments found in email uid={uid}. "
                f"Available parts: {list(parts.keys())}"
            )

        results = []
        for part_key, part_info in pdf_parts.items():
            filename = (
                part_info.get("filename")
                or part_info.get("file_name")
                or f"attachment_{part_key}.pdf"
            )
            result = await self.async_process_imap_part(
                entry_id=imap_entry_id,
                uid=uid,
                part_key=part_key,
                filename=filename,
                duplex_override=duplex,
                booklet_override=booklet or None,
                sender=sender,
                attachment_filter=attachment_filter,
                copies=copies,
                orientation=orientation,
                media=media,
                mail_subject=subject,
                mail_text=body_text,
            )
            results.append({
                "filename": result.filename,
                "success": result.success,
                "error": result.error,
            })

        return {
            "uid": uid,
            "printed": len(results),
            "results": results,
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

        result = FilterPreviewResult(
            checked_at=datetime.now().isoformat(timespec="seconds"),
            imap_account=f"{username}@{server}",
            imap_entry_id=target.entry_id,
            total_found=len(emails),
            matching=len(matching),
            with_pdf=len(with_pdf),
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

            pdf_emails = [e for e in result.emails if e.has_pdf and e.matches_filter]

            if not pdf_emails:
                body = (
                    f"**Account:** {result.imap_account}  \n"
                    f"**Checked:** {result.checked_at}  \n\n"
                    f"Found **{result.total_found}** email(s), "
                    f"**{result.matching}** match the sender filter, "
                    f"**{result.with_pdf}** have a PDF attachment.\n\n"
                    "_No printable emails found. Check your sender/folder filter settings._"
                )
            else:
                rows = "\n".join(
                    f"| `{e.uid}` | {e.subject[:35]} | {e.sender[:25]} | {e.pdf_count} |"
                    for e in pdf_emails[:20]
                )
                more = f"\n_… and {len(pdf_emails) - 20} more_" if len(pdf_emails) > 20 else ""
                body = (
                    f"**Account:** {result.imap_account}  \n"
                    f"**Checked:** {result.checked_at}  \n\n"
                    f"Found **{result.total_found}** email(s) · "
                    f"**{result.matching}** match filter · "
                    f"**{result.with_pdf}** have PDF\n\n"
                    "| UID | Subject | From | PDFs |\n"
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
        """Delete PDF files that are still waiting in the queue folder."""
        folder = self._queue_folder

        def _do_clear() -> int:
            deleted = 0
            try:
                for name in os.listdir(folder):
                    if name.lower().endswith(".pdf"):
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
        """Delete all PDFs in the configured queue folder."""
        deleted = await self._async_delete_queue_pdfs()
        await self.async_request_refresh()
        return deleted

    async def async_cancel_queued_jobs(self) -> int:
        """Discard jobs that Print Bridge has not submitted to the printer yet.

        This clears both schedule-held IMAP jobs and PDFs in the configured
        queue folder. It cannot recall a job once the printer has accepted it.
        """
        pending_count = len(self._pending_jobs)
        self._pending_jobs.clear()
        deleted_files = await self._async_delete_queue_pdfs()
        total = pending_count + deleted_files
        if total:
            logger.info(
                "Cancelled %d queued job(s): %d schedule-held, %d file-queue PDF(s)",
                total,
                pending_count,
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
        orientation: str | None = None,
        media: str | None = None,
    ) -> PrintJobResult:
        """Build an IPP packet and POST it to CUPS."""
        filename = sanitize_ipp_job_name(filename)
        effective_orientation = _orientation_for_job(booklet, orientation)
        orientation_requested = (
            _ORIENTATION_ENUMS[effective_orientation]
            if effective_orientation is not None
            else None
        )
        effective_copies = copies or 1
        if booklet:
            try:
                pdf_data = await self.hass.async_add_executor_job(
                    create_booklet, pdf_data
                )
            except Exception as exc:
                error = _describe_exception(exc)
                logger.error("Booklet conversion failed for '%s': %s", filename, error)
                return PrintJobResult(
                    filename=filename, success=False, error=error,
                    duplex=duplex_mode, booklet=booklet,
                    copies=effective_copies, orientation=effective_orientation,
                    media=media, status=error,
                )

        sides = determine_sides(duplex_mode, booklet)
        try:
            document_format, document_data = await self._async_prepare_document_for_printing(
                pdf_data, sides
            )
        except Exception as exc:
            error = _describe_exception(exc)
            logger.error("Document conversion failed for '%s': %s", filename, error)
            return PrintJobResult(
                filename=filename, success=False, error=error,
                duplex=duplex_mode, booklet=booklet,
                copies=effective_copies, orientation=effective_orientation,
                media=media, sides=sides, status=error,
            )

        packet = build_ipp_packet(
            self._printer_uri,
            filename,
            sides,
            document_data,
            document_format=document_format,
            copies=copies,
            orientation_requested=orientation_requested,
            media=media,
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
                        copies=effective_copies, orientation=effective_orientation,
                        media=media, sides=sides, document_format=document_format,
                        status_code=error, status=error,
                    )

                if body_prefix.startswith(b"<!doctype html") or body_prefix.startswith(b"<html"):
                    error = "HTTP 200 with HTML response"
                    logger.error("CUPS rejected job for '%s': %s", filename, error)
                    return PrintJobResult(
                        filename=filename, success=False, error=error,
                        duplex=duplex_mode, booklet=booklet,
                        copies=effective_copies, orientation=effective_orientation,
                        media=media, sides=sides, document_format=document_format,
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
                        copies=effective_copies, orientation=effective_orientation,
                        media=media, sides=sides, document_format=document_format,
                        status_code=ipp_status_code, status=ipp_status,
                    )

                error = ipp_status
                logger.error("IPP rejected job for '%s': %s", filename, error)
                return PrintJobResult(
                    filename=filename, success=False, error=error,
                    duplex=duplex_mode, booklet=booklet,
                    copies=effective_copies, orientation=effective_orientation,
                    media=media, sides=sides, document_format=document_format,
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
                    orientation=effective_orientation,
                    media=media,
                    sides=sides,
                    document_format=document_format,
                    status_code="timeout-submitted",
                    status=error,
                )
            logger.error("Network error printing '%s': %s", filename, error)
            return PrintJobResult(
                filename=filename, success=False, error=error,
                duplex=duplex_mode, booklet=booklet,
                copies=effective_copies, orientation=effective_orientation,
                media=media, sides=sides, document_format=document_format,
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
                copies=effective_copies, orientation=effective_orientation,
                media=media, sides=sides, document_format=document_format,
                status_code="network-error", status=error,
            )

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
        self, pdf_data: bytes, sides: str
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
            dpi = _resolution_dpi(capabilities.pwg_raster_resolutions)
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
            jpeg_data = await self.hass.async_add_executor_job(
                partial(convert_pdf_to_jpeg, pdf_data)
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
            result = await self._async_fetch_and_print(
                entry_id=job.entry_id,
                uid=job.uid,
                part_key=job.part_key,
                filename=job.filename,
                duplex_override=job.duplex_override,
                booklet_override=job.booklet_override,
                sender=job.sender,
                copies=job.copies,
                orientation=job.orientation,
                media=job.media,
            )
            self._record_job(result)
            await self._async_notify_job(result)
            # Apply configured IMAP action now that the job has been printed.
            await self._async_post_process_email(job.entry_id, job.uid)
            await self._async_send_status_reply(
                sender=job.sender,
                subject=job.mail_subject,
                results=[result],
                params=job.mail_params,
            )

        await self.async_request_refresh()
        return len(jobs)

    async def _async_update_data(self) -> AutoPrintData:
        """Check printer reachability, queue depth, and flush pending if schedule just opened."""
        printer_online = await self._async_check_printer_online()
        queue_depth = await self.hass.async_add_executor_job(self._count_queue_files)

        # Auto-flush pending jobs when the print window opens.
        currently_open = self._is_within_schedule()
        if (
            self._pending_jobs
            and currently_open
            and self._last_schedule_state is not True  # None (startup) or False triggers flush
        ):
            logger.info("Print window opened — flushing %d pending job(s)", len(self._pending_jobs))
            await self.async_flush_pending()

        self._last_schedule_state = currently_open

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
        try:
            return sum(
                1
                for name in os.listdir(self._queue_folder)
                if name.lower().endswith(".pdf")
            )
        except OSError:
            return 0

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
                "orientation": result.orientation,
                "media": result.media,
                "sides": result.sides,
                "document_format": result.document_format,
                "status_code": result.status_code,
                "status": result.status,
                "timestamp": result.timestamp,
            },
        )
