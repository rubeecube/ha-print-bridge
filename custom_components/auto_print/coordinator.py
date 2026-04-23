"""DataUpdateCoordinator for the Auto Print integration.

Responsibilities:
  - Listen for imap_content events fired by HA's built-in IMAP integration.
  - For each PDF attachment, call imap.fetch_part to retrieve the bytes.
  - Optionally reorder pages for booklet printing.
  - Send the print job to CUPS via a raw IPP/2.0 request (aiohttp).
  - Fire auto_print_job_completed events → HA Logbook audit trail.
  - Periodically check printer reachability and count queued files.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
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
    DEFAULT_AUTO_DELETE,
    DEFAULT_DUPLEX_MODE,
    DEFAULT_EMAIL_ACTION,
    DEFAULT_EMAIL_ARCHIVE_FOLDER,
    DEFAULT_NOTIFY_ON_FAILURE,
    DEFAULT_NOTIFY_ON_SUCCESS,
    DEFAULT_QUEUE_FOLDER,
    DOMAIN,
    EVENT_JOB_COMPLETED,
)
from .imap_checker import EmailPreview, preview_mailbox
from .print_handler import (
    build_ipp_packet,
    cups_printer_uri,
    determine_sides,
    http_url_to_ipp_uri,
    is_booklet_job,
)

logger = logging.getLogger(__name__)

_STATUS_INTERVAL = timedelta(minutes=5)


@dataclass
class PrintJobResult:
    """Outcome of a single print attempt, including audit metadata."""

    filename: str
    success: bool
    error: str | None = None
    sender: str | None = None
    duplex: str | None = None
    booklet: bool = False
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
    total_found: int                # total messages inspected
    matching: int                   # messages matching the sender filter
    with_pdf: int                   # matching messages that have a PDF attachment
    emails: list[EmailPreview] = field(default_factory=list)


@dataclass
class AutoPrintData:
    """Snapshot of integration state exposed to entities."""

    queue_depth: int = 0
    printer_online: bool = False
    last_job: PrintJobResult | None = None
    job_history: list[PrintJobResult] = field(default_factory=list)
    total_jobs_sent: int = 0
    filter_preview: FilterPreviewResult | None = None


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
        return [s.lower() for s in self._entry.options.get(CONF_ALLOWED_SENDERS, [])]

    @property
    def _folder_filter(self) -> list[str]:
        """IMAP folder names to accept; empty list means accept all folders."""
        return [f.strip() for f in self._entry.options.get(CONF_FOLDER_FILTER, []) if f.strip()]

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
        sender: str = event.data.get("sender", "").lower()
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

        had_pdf = False
        for part_key, part_info in parts.items():
            if part_info.get("content_type") != "application/pdf":
                continue

            had_pdf = True
            filename: str = (
                part_info.get("filename")
                or part_info.get("file_name")
                or f"document_{part_key}.pdf"
            )
            result = await self._async_fetch_and_print(
                entry_id=entry_id,
                uid=uid,
                part_key=part_key,
                filename=filename,
                sender=sender,
            )
            self._record_job(result)
            await self._async_notify_job(result)

        if had_pdf:
            await self._async_post_process_email(entry_id, uid)

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
    ) -> PrintJobResult:
        """Fetch one attachment via imap.fetch_part and print it.

        IMAP identifiers are always stored in the result so the job can be
        retried later via async_retry_job / auto_print.retry_job.
        """
        try:
            response: dict[str, Any] = await self.hass.services.async_call(
                "imap",
                "fetch_part",
                {"entry_id": entry_id, "uid": uid, "part": part_key},
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
            filename, pdf_bytes, effective_duplex, effective_booklet
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
                    {"entry_id": entry_id, "uid": uid},
                    blocking=True,
                )
            elif action == "move":
                await self.hass.services.async_call(
                    "imap", "move",
                    {
                        "entry_id": entry_id,
                        "uid": uid,
                        "target_folder": self._email_archive_folder,
                        "seen": True,
                    },
                    blocking=True,
                )
            elif action == "delete":
                await self.hass.services.async_call(
                    "imap", "delete",
                    {"entry_id": entry_id, "uid": uid},
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
            title = f"Auto Print — Printed successfully"
            message = f"**{result.filename}**"
            if result.sender:
                message += f"\nFrom: {result.sender}"
            if result.duplex:
                message += f"\nDuplex: {result.duplex}"
            if result.booklet:
                message += "\nBooklet mode"
        else:
            title = "Auto Print — Print failed"
            message = f"**{result.filename}** could not be printed."
            if result.error:
                message += f"\nError: {result.error}"
            if result.sender:
                message += f"\nFrom: {result.sender}"
            message += "\n\nCheck the HA logs or the Auto Print sensor for details."

        try:
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": title,
                    "message": message,
                    "notification_id": f"auto_print_{self._entry.entry_id}_{result.timestamp}",
                },
            )
        except Exception:
            logger.warning("Could not send notification for job '%s'", result.filename)

    async def async_process_imap_part(
        self,
        entry_id: str,
        uid: str,
        part_key: str,
        filename: str | None = None,
        duplex_override: str | None = None,
        booklet_override: bool | None = None,
        sender: str | None = None,
    ) -> PrintJobResult:
        """Fetch one IMAP attachment and print it (called by the service)."""
        effective_filename = filename or f"attachment_{part_key}.pdf"
        result = await self._async_fetch_and_print(
            entry_id=entry_id,
            uid=uid,
            part_key=part_key,
            filename=effective_filename,
            duplex_override=duplex_override,
            booklet_override=booklet_override,
            sender=sender,
        )
        self._record_job(result)
        await self.async_request_refresh()
        return result

    async def async_print_file(
        self,
        file_path: str,
        duplex_mode: str | None = None,
        force_booklet: bool = False,
    ) -> PrintJobResult:
        """Print a PDF file from disk and return the result."""
        filename = os.path.basename(file_path)
        effective_duplex = duplex_mode or self._duplex_mode
        booklet = force_booklet or is_booklet_job(filename, self._booklet_patterns)

        try:
            with open(file_path, "rb") as f:
                pdf_data = f.read()
        except OSError as exc:
            result = PrintJobResult(filename=filename, success=False, error=str(exc))
            self._record_job(result)
            return result

        result = await self.async_send_print_job(filename, pdf_data, effective_duplex, booklet)
        self._record_job(result)
        await self.async_request_refresh()
        return result

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
            total_found=len(emails),
            matching=len(matching),
            with_pdf=len(with_pdf),
            emails=emails,
        )
        self._filter_preview = result
        await self.async_request_refresh()
        return result

    async def async_clear_queue(self) -> int:
        """Delete all PDFs in the configured queue folder."""
        folder = self._queue_folder
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
        await self.async_request_refresh()
        return deleted

    async def async_send_print_job(
        self, filename: str, pdf_data: bytes, duplex_mode: str, booklet: bool
    ) -> PrintJobResult:
        """Build an IPP packet and POST it to CUPS."""
        if booklet:
            try:
                pdf_data = await self.hass.async_add_executor_job(
                    create_booklet, pdf_data
                )
            except Exception as exc:
                logger.error("Booklet conversion failed for '%s': %s", filename, exc)
                return PrintJobResult(
                    filename=filename, success=False, error=str(exc),
                    duplex=duplex_mode, booklet=booklet,
                )

        sides = determine_sides(duplex_mode, booklet)
        packet = build_ipp_packet(self._printer_uri, filename, sides, pdf_data)

        session = async_get_clientsession(self.hass)
        try:
            async with session.post(
                self._ipp_endpoint,
                data=packet,
                headers={"Content-Type": "application/ipp"},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                body = await resp.text(errors="replace")
                if resp.status == 200 and "<!DOCTYPE HTML>" not in body:
                    logger.debug(
                        "Print job accepted for '%s' (sides=%s)", filename, sides
                    )
                    return PrintJobResult(
                        filename=filename, success=True,
                        duplex=duplex_mode, booklet=booklet,
                    )

                error = f"HTTP {resp.status}"
                logger.error("CUPS rejected job for '%s': %s", filename, error)
                return PrintJobResult(
                    filename=filename, success=False, error=error,
                    duplex=duplex_mode, booklet=booklet,
                )

        except aiohttp.ClientError as exc:
            logger.error("Network error printing '%s': %s", filename, exc)
            return PrintJobResult(
                filename=filename, success=False, error=str(exc),
                duplex=duplex_mode, booklet=booklet,
            )

    # ------------------------------------------------------------------
    # Coordinator periodic update (printer status only)
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> AutoPrintData:
        """Check printer reachability and queue depth."""
        printer_online = await self._async_check_printer_online()
        queue_depth = self._count_queue_files()
        last_job = self._job_history[0] if self._job_history else None
        return AutoPrintData(
            queue_depth=queue_depth,
            printer_online=printer_online,
            last_job=last_job,
            job_history=list(self._job_history),
            total_jobs_sent=self._total_jobs_sent,
            filter_preview=self._filter_preview,
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
                "timestamp": result.timestamp,
            },
        )
