"""DataUpdateCoordinator for the Auto Print integration.

Responsibilities:
  - Poll the IMAP server for new PDF emails from allowed senders.
  - Save PDFs to the configured queue folder.
  - Optionally reorder pages for booklet printing.
  - Send the print job to CUPS via a raw IPP request (aiohttp).
  - Optionally delete the file after a successful print.
  - Track queue depth, last job status, and printer reachability.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import timedelta

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .booklet_maker import create_booklet
from .const import (
    CONF_ALLOWED_SENDERS,
    CONF_AUTO_DELETE,
    CONF_BOOKLET_PATTERNS,
    CONF_CUPS_URL,
    CONF_DUPLEX_MODE,
    CONF_IMAP_FOLDER,
    CONF_IMAP_PASSWORD,
    CONF_IMAP_PORT,
    CONF_IMAP_SERVER,
    CONF_IMAP_USE_SSL,
    CONF_IMAP_USERNAME,
    CONF_POLL_INTERVAL_MINUTES,
    CONF_PRINTER_NAME,
    CONF_QUEUE_FOLDER,
    DEFAULT_AUTO_DELETE,
    DEFAULT_DUPLEX_MODE,
    DEFAULT_POLL_INTERVAL_MINUTES,
    DEFAULT_QUEUE_FOLDER,
    DOMAIN,
)
from .imap_handler import PdfAttachment, fetch_pdf_attachments
from .print_handler import build_ipp_packet, determine_sides, is_booklet_job

logger = logging.getLogger(__name__)


@dataclass
class PrintJobResult:
    """Outcome of a single print attempt."""

    filename: str
    success: bool
    error: str | None = None


@dataclass
class AutoPrintData:
    """Snapshot of integration state, stored in the coordinator."""

    queue_depth: int = 0
    printer_online: bool = False
    last_job: PrintJobResult | None = None
    # Accumulated history — most recent first, capped at 20 entries.
    job_history: list[PrintJobResult] = field(default_factory=list)


class AutoPrintCoordinator(DataUpdateCoordinator[AutoPrintData]):
    """Manages IMAP polling, printing, and state for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        poll_minutes: int = entry.options.get(
            CONF_POLL_INTERVAL_MINUTES, DEFAULT_POLL_INTERVAL_MINUTES
        )
        super().__init__(
            hass,
            logger,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(minutes=poll_minutes),
        )
        self._entry = entry

    # ------------------------------------------------------------------
    # Properties derived from config / options
    # ------------------------------------------------------------------

    @property
    def _imap_cfg(self) -> dict:
        d = self._entry.data
        return {
            "server": d[CONF_IMAP_SERVER],
            "port": d[CONF_IMAP_PORT],
            "use_ssl": d[CONF_IMAP_USE_SSL],
            "username": d[CONF_IMAP_USERNAME],
            "password": d[CONF_IMAP_PASSWORD],
            "folder": d[CONF_IMAP_FOLDER],
            "allowed_senders": d[CONF_ALLOWED_SENDERS],
        }

    @property
    def _cups_url(self) -> str:
        return self._entry.data[CONF_CUPS_URL].rstrip("/")

    @property
    def _printer_name(self) -> str:
        return self._entry.data[CONF_PRINTER_NAME]

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
    def _ipp_endpoint(self) -> str:
        return f"{self._cups_url}/printers/{self._printer_name}"

    # ------------------------------------------------------------------
    # Public helpers (called from services / button)
    # ------------------------------------------------------------------

    async def async_print_file(
        self,
        file_path: str,
        duplex_mode: str | None = None,
        force_booklet: bool = False,
    ) -> PrintJobResult:
        """Print an existing file on disk and return the result."""
        filename = os.path.basename(file_path)
        effective_duplex = duplex_mode or self._duplex_mode
        booklet = force_booklet or is_booklet_job(filename, self._booklet_patterns)

        try:
            with open(file_path, "rb") as f:
                pdf_data = f.read()
        except OSError as exc:
            return PrintJobResult(filename=filename, success=False, error=str(exc))

        return await self._async_send_print_job(
            filename, pdf_data, effective_duplex, booklet
        )

    async def async_clear_queue(self) -> int:
        """Delete all PDF files in the queue folder. Returns the number deleted."""
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

    # ------------------------------------------------------------------
    # Coordinator update
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> AutoPrintData:
        """Poll IMAP for new jobs, print them, and return current state."""
        previous = self.data or AutoPrintData()

        printer_online = await self._async_check_printer_online()

        new_jobs: list[PrintJobResult] = []
        if printer_online:
            new_jobs = await self._async_process_new_emails()
        else:
            logger.warning("Printer offline — skipping IMAP poll this cycle")

        history = (new_jobs + previous.job_history)[:20]
        last_job = history[0] if history else None
        queue_depth = self._count_queue_files()

        return AutoPrintData(
            queue_depth=queue_depth,
            printer_online=printer_online,
            last_job=last_job,
            job_history=history,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _async_check_printer_online(self) -> bool:
        """Return True if the CUPS server responds to a HEAD request."""
        session = async_get_clientsession(self.hass)
        try:
            async with session.head(self._cups_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                return resp.status < 500
        except Exception:
            return False

    async def _async_process_new_emails(self) -> list[PrintJobResult]:
        """Fetch PDFs from IMAP and print them; return one result per attachment."""
        cfg = self._imap_cfg
        attachments: list[PdfAttachment] = await self.hass.async_add_executor_job(
            fetch_pdf_attachments,
            cfg["server"],
            cfg["port"],
            cfg["use_ssl"],
            cfg["username"],
            cfg["password"],
            cfg["folder"],
            cfg["allowed_senders"],
        )

        if not attachments:
            return []

        results = await asyncio.gather(
            *[self._async_handle_attachment(att) for att in attachments],
            return_exceptions=False,
        )
        return list(results)

    async def _async_handle_attachment(self, att: PdfAttachment) -> PrintJobResult:
        """Save, optionally convert, print, and optionally delete one attachment."""
        queue_folder = self._queue_folder
        os.makedirs(queue_folder, exist_ok=True)
        file_path = os.path.join(queue_folder, att.filename)

        try:
            with open(file_path, "wb") as f:
                f.write(att.data)
        except OSError as exc:
            return PrintJobResult(filename=att.filename, success=False, error=str(exc))

        booklet = is_booklet_job(att.filename, self._booklet_patterns)
        result = await self._async_send_print_job(
            att.filename, att.data, self._duplex_mode, booklet
        )

        if self._auto_delete or result.success:
            try:
                os.remove(file_path)
            except OSError:
                logger.warning("Could not remove '%s' after printing", file_path)

        return result

    async def _async_send_print_job(
        self, filename: str, pdf_data: bytes, duplex_mode: str, booklet: bool
    ) -> PrintJobResult:
        """Build the IPP packet and POST it to CUPS. Returns a PrintJobResult."""
        if booklet:
            try:
                pdf_data = await self.hass.async_add_executor_job(
                    create_booklet, pdf_data
                )
            except Exception as exc:
                logger.error("Booklet conversion failed for '%s': %s", filename, exc)
                return PrintJobResult(filename=filename, success=False, error=str(exc))

        sides = determine_sides(duplex_mode, booklet)
        packet = build_ipp_packet(self._printer_name, filename, sides, pdf_data)

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
                    logger.debug("Print job accepted for '%s' (sides=%s)", filename, sides)
                    return PrintJobResult(filename=filename, success=True)

                error = f"HTTP {resp.status}"
                logger.error("CUPS rejected job for '%s': %s", filename, error)
                return PrintJobResult(filename=filename, success=False, error=error)

        except aiohttp.ClientError as exc:
            logger.error("Network error printing '%s': %s", filename, exc)
            return PrintJobResult(filename=filename, success=False, error=str(exc))

    def _count_queue_files(self) -> int:
        """Count PDF files currently sitting in the queue folder."""
        try:
            return sum(
                1
                for name in os.listdir(self._queue_folder)
                if name.lower().endswith(".pdf")
            )
        except OSError:
            return 0
