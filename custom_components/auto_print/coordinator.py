"""DataUpdateCoordinator for the Auto Print integration.

Responsibilities:
  - Listen for imap_content events fired by HA's built-in IMAP integration.
  - For each PDF attachment, call imap.fetch_part to retrieve the bytes.
  - Optionally reorder pages for booklet printing.
  - Send the print job to CUPS via a raw IPP/2.0 request (aiohttp).
  - Periodically check printer reachability and count queued files.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass, field
from datetime import timedelta
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
    CONF_DUPLEX_MODE,
    CONF_PRINTER_NAME,
    CONF_QUEUE_FOLDER,
    DEFAULT_AUTO_DELETE,
    DEFAULT_DUPLEX_MODE,
    DEFAULT_QUEUE_FOLDER,
    DOMAIN,
)
from .print_handler import build_ipp_packet, determine_sides, is_booklet_job

logger = logging.getLogger(__name__)

# Printer-status check interval (not used for IMAP — that is event-driven).
_STATUS_INTERVAL = timedelta(minutes=5)


@dataclass
class PrintJobResult:
    """Outcome of a single print attempt."""

    filename: str
    success: bool
    error: str | None = None


@dataclass
class AutoPrintData:
    """Snapshot of integration state exposed to entities."""

    queue_depth: int = 0
    printer_online: bool = False
    last_job: PrintJobResult | None = None
    job_history: list[PrintJobResult] = field(default_factory=list)


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

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def _cups_url(self) -> str:
        return self._entry.data[CONF_CUPS_URL].rstrip("/")

    @property
    def _printer_name(self) -> str:
        return self._entry.data[CONF_PRINTER_NAME]

    @property
    def _ipp_endpoint(self) -> str:
        return f"{self._cups_url}/printers/{self._printer_name}"

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

    # ------------------------------------------------------------------
    # IMAP event handler — called by hass.bus.async_listen
    # ------------------------------------------------------------------

    async def async_handle_imap_event(self, event: Event) -> None:
        """Process an imap_content event from HA's built-in IMAP integration.

        Filters by allowed senders, retrieves each PDF part via
        imap.fetch_part, decodes it, and prints to CUPS.
        """
        sender: str = event.data.get("sender", "").lower()
        allowed = self._allowed_senders
        if allowed and sender not in allowed:
            logger.debug("Skipping email from %s (not in allowed_senders)", sender)
            return

        parts: dict[str, dict[str, Any]] = event.data.get("parts", {})
        entry_id: str = event.data.get("entry_id", "")
        uid: str = str(event.data.get("uid", ""))

        for part_key, part_info in parts.items():
            if part_info.get("content_type") != "application/pdf":
                continue

            filename: str = (
                part_info.get("filename")
                or part_info.get("file_name")
                or f"document_{part_key}.pdf"
            )
            result = await self._async_fetch_and_print(
                entry_id=entry_id, uid=uid, part_key=part_key, filename=filename
            )
            self._record_job(result)

        if parts:
            await self.async_request_refresh()

    async def async_process_imap_part(
        self,
        entry_id: str,
        uid: str,
        part_key: str,
        filename: str | None = None,
        duplex_override: str | None = None,
        booklet_override: bool | None = None,
    ) -> PrintJobResult:
        """Fetch one IMAP attachment and print it with optional setting overrides.

        Called by the ``auto_print.process_imap_part`` service so that
        automations and blueprints can drive printing with per-job settings.
        """
        effective_filename = filename or f"attachment_{part_key}.pdf"
        result = await self._async_fetch_and_print(
            entry_id=entry_id,
            uid=uid,
            part_key=part_key,
            filename=effective_filename,
            duplex_override=duplex_override,
            booklet_override=booklet_override,
        )
        self._record_job(result)
        await self.async_request_refresh()
        return result

    async def _async_fetch_and_print(
        self,
        entry_id: str,
        uid: str,
        part_key: str,
        filename: str,
        duplex_override: str | None = None,
        booklet_override: bool | None = None,
    ) -> PrintJobResult:
        """Fetch one attachment via imap.fetch_part and print it.

        *duplex_override* and *booklet_override* take precedence over the
        integration's configured defaults when provided.
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
            return PrintJobResult(filename=filename, success=False, error=str(exc))

        try:
            raw: str = response["part_data"]
            encoding: str = response.get("content_transfer_encoding", "base64").lower()
            if encoding == "base64":
                pdf_bytes = base64.b64decode(raw)
            else:
                # 7bit / 8bit / binary — data is already plain text/bytes
                pdf_bytes = (
                    raw.encode("latin-1") if isinstance(raw, str) else bytes(raw)
                )
        except Exception as exc:
            logger.error("Decoding attachment '%s' failed: %s", filename, exc)
            return PrintJobResult(filename=filename, success=False, error=str(exc))

        effective_duplex = duplex_override or self._duplex_mode
        effective_booklet = (
            booklet_override
            if booklet_override is not None
            else is_booklet_job(filename, self._booklet_patterns)
        )
        return await self.async_send_print_job(
            filename, pdf_bytes, effective_duplex, effective_booklet
        )

    # ------------------------------------------------------------------
    # Public helpers (called from services / button entity)
    # ------------------------------------------------------------------

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
            return PrintJobResult(filename=filename, success=False, error=str(exc))

        result = await self.async_send_print_job(
            filename, pdf_data, effective_duplex, booklet
        )
        self._record_job(result)
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
                    logger.debug(
                        "Print job accepted for '%s' (sides=%s)", filename, sides
                    )
                    return PrintJobResult(filename=filename, success=True)

                error = f"HTTP {resp.status}"
                logger.error("CUPS rejected job for '%s': %s", filename, error)
                return PrintJobResult(filename=filename, success=False, error=error)

        except aiohttp.ClientError as exc:
            logger.error("Network error printing '%s': %s", filename, exc)
            return PrintJobResult(filename=filename, success=False, error=str(exc))

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
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _async_check_printer_online(self) -> bool:
        """Return True if the CUPS server responds to a HEAD request."""
        session = async_get_clientsession(self.hass)
        try:
            async with session.head(
                self._cups_url, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                return resp.status < 500
        except Exception:
            return False

    def _count_queue_files(self) -> int:
        """Count PDF files sitting in the queue folder."""
        try:
            return sum(
                1
                for name in os.listdir(self._queue_folder)
                if name.lower().endswith(".pdf")
            )
        except OSError:
            return 0

    def _record_job(self, result: PrintJobResult) -> None:
        """Prepend a result to the job history (capped at 20 entries)."""
        self._job_history = ([result] + self._job_history)[:20]
