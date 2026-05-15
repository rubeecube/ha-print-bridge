"""Button entities for the Print Bridge integration."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    BUTTON_CANCEL_QUEUED_JOBS,
    BUTTON_CHECK_FILTER,
    BUTTON_CHECK_PRINTER_CAPABILITIES,
    BUTTON_FLUSH_PENDING,
    BUTTON_PRINT_EMAIL_PREFIX,
    BUTTON_PRINT_EMAIL_SLOTS,
    BUTTON_RETRY_LAST_FAILED,
    BUTTON_TEST_PAGE,
    DOMAIN,
)
from .coordinator import AutoPrintCoordinator
from .sensor import _device_info

logger = logging.getLogger(__name__)

_TEST_PAGE_CONTENT = b"""\
%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/MediaBox[0 0 595 842]/Parent 2 0 R/Resources<</Font<</F1 4 0 R>>>>>>endobj
4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
5 0 obj<</Length 44>>
stream
BT /F1 24 Tf 200 400 Td (Print Bridge test) Tj ET
endstream
endobj
xref
0 6
0000000000 65535 f\r
0000000009 00000 n\r
0000000058 00000 n\r
0000000115 00000 n\r
0000000266 00000 n\r
0000000343 00000 n\r
trailer<</Size 6/Root 1 0 R>>
startxref
440
%%EOF
"""


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AutoPrintCoordinator = entry.runtime_data
    async_add_entities(
        [
            TestPageButton(coordinator, entry),
            CheckFilterButton(coordinator, entry),
            CheckPrinterCapabilitiesButton(coordinator, entry),
            RetryLastFailedButton(coordinator, entry),
            FlushPendingButton(coordinator, entry),
            CancelQueuedJobsButton(coordinator, entry),
            *[
                PrintPreviewEmailButton(coordinator, entry, slot)
                for slot in range(BUTTON_PRINT_EMAIL_SLOTS)
            ],
        ]
    )


class TestPageButton(CoordinatorEntity[AutoPrintCoordinator], ButtonEntity):
    """Send a minimal test-page PDF to the printer."""

    _attr_has_entity_name = True
    _attr_translation_key = BUTTON_TEST_PAGE
    _attr_icon = "mdi:printer-check"

    def __init__(self, coordinator: AutoPrintCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{BUTTON_TEST_PAGE}"
        self._attr_device_info = _device_info(entry)

    async def async_press(self) -> None:
        """Send the built-in test page to the printer."""
        target = self.coordinator.selected_printer_coordinator
        result = await target.async_send_print_job(
            filename="auto_print_test_page.pdf",
            pdf_data=_TEST_PAGE_CONTENT,
            duplex_mode="one-sided",
            booklet=False,
        )
        if not result.success:
            raise HomeAssistantError(f"Test page failed: {result.error}")


class CheckFilterButton(CoordinatorEntity[AutoPrintCoordinator], ButtonEntity):
    """Run a filter preview: connect to IMAP and show matching emails."""

    _attr_has_entity_name = True
    _attr_translation_key = BUTTON_CHECK_FILTER
    _attr_icon = "mdi:email-search"

    def __init__(self, coordinator: AutoPrintCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{BUTTON_CHECK_FILTER}"
        self._attr_device_info = _device_info(entry)

    async def async_press(self) -> None:
        """Run filter preview and update sensor.auto_print_*_filter_preview."""
        await self.coordinator.async_check_filter()


class CheckPrinterCapabilitiesButton(
    CoordinatorEntity[AutoPrintCoordinator], ButtonEntity
):
    """Query the selected printer's IPP document-format support."""

    _attr_has_entity_name = True
    _attr_translation_key = BUTTON_CHECK_PRINTER_CAPABILITIES
    _attr_icon = "mdi:printer-search"

    def __init__(self, coordinator: AutoPrintCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{BUTTON_CHECK_PRINTER_CAPABILITIES}"
        self._attr_device_info = _device_info(entry)

    async def async_press(self) -> None:
        """Refresh printer capabilities for the selected target printer."""
        target = self.coordinator.selected_printer_coordinator
        await target.async_check_printer_capabilities(force=True)


class RetryLastFailedButton(CoordinatorEntity[AutoPrintCoordinator], ButtonEntity):
    """Re-fetch and reprint the most recent failed email print job."""

    _attr_has_entity_name = True
    _attr_translation_key = BUTTON_RETRY_LAST_FAILED
    _attr_icon = "mdi:printer-alert"

    def __init__(self, coordinator: AutoPrintCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{BUTTON_RETRY_LAST_FAILED}"
        self._attr_device_info = _device_info(entry)

    @property
    def available(self) -> bool:
        """Only available when there is a failed retryable job in history."""
        data = self.coordinator.data
        if not data:
            return False
        return any(
            not j.success and j.can_retry
            for j in data.job_history
        )

    async def async_press(self) -> None:
        """Retry the most recent failed job."""
        await self.coordinator.async_retry_last_failed()


class FlushPendingButton(CoordinatorEntity[AutoPrintCoordinator], ButtonEntity):
    """Print all schedule-queued jobs immediately, regardless of the time window."""

    _attr_has_entity_name = True
    _attr_translation_key = BUTTON_FLUSH_PENDING
    _attr_icon = "mdi:printer-off-outline"

    def __init__(self, coordinator: AutoPrintCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{BUTTON_FLUSH_PENDING}"
        self._attr_device_info = _device_info(entry)

    @property
    def available(self) -> bool:
        """Only available when there are pending jobs."""
        data = self.coordinator.data
        return bool(data and data.pending_jobs)

    async def async_press(self) -> None:
        """Flush the schedule queue and print all waiting jobs now."""
        await self.coordinator.async_flush_pending()


class CancelQueuedJobsButton(CoordinatorEntity[AutoPrintCoordinator], ButtonEntity):
    """Cancel jobs that are still waiting inside Print Bridge."""

    _attr_has_entity_name = True
    _attr_translation_key = BUTTON_CANCEL_QUEUED_JOBS
    _attr_icon = "mdi:printer-cancel"

    def __init__(self, coordinator: AutoPrintCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{BUTTON_CANCEL_QUEUED_JOBS}"
        self._attr_device_info = _device_info(entry)

    @property
    def available(self) -> bool:
        """Only available when Print Bridge has queued work to discard."""
        data = self.coordinator.data
        return bool(data and (data.pending_jobs or data.queue_depth > 0))

    async def async_press(self) -> None:
        """Discard jobs that have not yet been submitted to the printer."""
        await self.coordinator.async_cancel_queued_jobs()


class PrintPreviewEmailButton(CoordinatorEntity[AutoPrintCoordinator], ButtonEntity):
    """Print one of the latest matching emails from the filter preview."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:email-fast-outline"

    def __init__(
        self, coordinator: AutoPrintCoordinator, entry: ConfigEntry, slot: int
    ) -> None:
        super().__init__(coordinator)
        self._slot = slot
        self._attr_unique_id = (
            f"{entry.entry_id}_{BUTTON_PRINT_EMAIL_PREFIX}_{slot + 1}"
        )
        self._attr_device_info = _device_info(entry)

    @property
    def _email(self) -> dict | None:
        data = self.coordinator.data
        if not data or not data.filter_preview:
            return None
        printable = []
        for email in data.filter_preview.emails:
            has_printable = (
                email.has_printable
                if email.has_printable is not None
                else email.has_pdf
            )
            if has_printable and email.matches_filter:
                printable.append(email.as_dict())
        if self._slot >= len(printable):
            return None
        return printable[self._slot]

    @property
    def name(self) -> str:
        """Return a compact, dynamic label for the email slot."""
        email = self._email
        if not email:
            return f"Print Email {self._slot + 1}"
        subject = email.get("subject") or "(no subject)"
        return f"Print Email {self._slot + 1}: {subject[:48]}"

    @property
    def available(self) -> bool:
        """Only available after a scan finds a printable email in this slot."""
        return self._email is not None

    @property
    def extra_state_attributes(self) -> dict:
        """Expose the scanned email represented by this button."""
        email = self._email
        if not email:
            return {"slot": self._slot + 1}
        return {
            "slot": self._slot + 1,
            "uid": email.get("uid"),
            "subject": email.get("subject"),
            "sender": email.get("sender"),
            "date": email.get("date"),
            "folder": email.get("folder"),
            "pdf_count": email.get("pdf_count"),
            "printable_count": email.get("printable_count"),
        }

    async def async_press(self) -> None:
        """Print all supported attachments from the email in this slot."""
        email = self._email
        data = self.coordinator.data
        preview = data.filter_preview if data else None
        if not email or not preview:
            raise HomeAssistantError(
                "No scanned email is available for this button. Run Check Filter first."
            )

        target = self.coordinator.selected_printer_coordinator
        result = await target.async_print_email(
            uid=email["uid"],
            imap_entry_id=preview.imap_entry_id,
        )
        failures = [r for r in result["results"] if not r["success"]]
        if failures:
            first = failures[0]
            raise HomeAssistantError(
                f"Email print failed for '{first['filename']}': {first['error']}"
            )
