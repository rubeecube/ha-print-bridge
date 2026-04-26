"""Sensor entities for the Print Bridge integration."""

from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_LAST_FILENAME,
    ATTR_LAST_STATUS,
    CONF_DIRECT_PRINTER_URL,
    CONF_PRINTER_NAME,
    DOMAIN,
    SENSOR_FILTER_PREVIEW,
    SENSOR_JOB_LOG,
    SENSOR_LAST_JOB,
    SENSOR_PENDING_JOBS,
    SENSOR_QUEUE_DEPTH,
)
from .coordinator import AutoPrintCoordinator, AutoPrintData

logger = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AutoPrintCoordinator = entry.runtime_data
    async_add_entities(
        [
            QueueDepthSensor(coordinator, entry),
            LastJobSensor(coordinator, entry),
            JobLogSensor(coordinator, entry),
            FilterPreviewSensor(coordinator, entry),
            PendingJobsSensor(coordinator, entry),
        ]
    )


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    # Use a stable direct-mode label so entity_ids do not include DHCP-assigned IPs.
    printer_label = (
        entry.data.get(CONF_PRINTER_NAME)
        or ("Direct Printer" if entry.data.get(CONF_DIRECT_PRINTER_URL) else None)
        or "Printer"
    )
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"Print Bridge — {printer_label}",
        manufacturer="Print Bridge",
        model="Email → IPP Bridge",
        entry_type=DeviceEntryType.SERVICE,
    )


class QueueDepthSensor(CoordinatorEntity[AutoPrintCoordinator], SensorEntity):
    """Number of PDF files currently waiting in the print queue folder."""

    _attr_has_entity_name = True
    _attr_translation_key = SENSOR_QUEUE_DEPTH
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "files"
    _attr_icon = "mdi:printer-outline"

    def __init__(self, coordinator: AutoPrintCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{SENSOR_QUEUE_DEPTH}"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> int:
        return self.coordinator.data.queue_depth if self.coordinator.data else 0


class LastJobSensor(CoordinatorEntity[AutoPrintCoordinator], SensorEntity):
    """Status of the most recently attempted print job."""

    _attr_has_entity_name = True
    _attr_translation_key = SENSOR_LAST_JOB
    _attr_icon = "mdi:file-pdf-box"

    def __init__(self, coordinator: AutoPrintCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{SENSOR_LAST_JOB}"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> str | None:
        data: AutoPrintData | None = self.coordinator.data
        if data is None or data.last_job is None:
            return None
        return "success" if data.last_job.success else "failed"

    @property
    def extra_state_attributes(self) -> dict:
        data: AutoPrintData | None = self.coordinator.data
        if data is None or data.last_job is None:
            return {}
        job = data.last_job
        attrs: dict = {ATTR_LAST_FILENAME: job.filename}
        if job.error:
            attrs[ATTR_LAST_STATUS] = job.error
        if job.sender:
            attrs["sender"] = job.sender
        if job.duplex:
            attrs["duplex"] = job.duplex
        attrs["booklet"] = job.booklet
        attrs["timestamp"] = job.timestamp
        return attrs


class JobLogSensor(CoordinatorEntity[AutoPrintCoordinator], SensorEntity):
    """Cumulative print job counter with full history in attributes.

    state      : total number of jobs sent since last HA restart
    attributes : jobs — list of the last 50 print attempts with full metadata
    """

    _attr_has_entity_name = True
    _attr_translation_key = SENSOR_JOB_LOG
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "jobs"
    _attr_icon = "mdi:clipboard-text-clock"

    def __init__(self, coordinator: AutoPrintCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{SENSOR_JOB_LOG}"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> int:
        return self.coordinator.data.total_jobs_sent if self.coordinator.data else 0

    @property
    def extra_state_attributes(self) -> dict:
        data: AutoPrintData | None = self.coordinator.data
        if data is None:
            return {"jobs": []}
        return {
            "jobs": [
                {
                    "index": i,
                    "timestamp": j.timestamp,
                    "filename": j.filename,
                    "success": j.success,
                    "error": j.error,
                    "sender": j.sender,
                    "duplex": j.duplex,
                    "booklet": j.booklet,
                    "can_retry": j.can_retry,
                    "uid": j.imap_uid,
                }
                for i, j in enumerate(data.job_history)
            ]
        }


class FilterPreviewSensor(CoordinatorEntity[AutoPrintCoordinator], SensorEntity):
    """Shows results of the last 'Check Filter' run.

    state      : number of emails that match the filter AND have a PDF
    attributes : full list of inspected emails with match / pdf status,
                 plus summary counters and the time the check was run
    """

    _attr_has_entity_name = True
    _attr_translation_key = SENSOR_FILTER_PREVIEW
    _attr_native_unit_of_measurement = "emails"
    _attr_icon = "mdi:email-search"

    def __init__(self, coordinator: AutoPrintCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{SENSOR_FILTER_PREVIEW}"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> int | None:
        data = self.coordinator.data
        if data is None or data.filter_preview is None:
            return None
        return data.filter_preview.with_pdf

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        if data is None or data.filter_preview is None:
            return {"checked_at": None, "emails": []}
        preview = data.filter_preview
        return {
            "checked_at": preview.checked_at,
            "imap_account": preview.imap_account,
            "imap_entry_id": preview.imap_entry_id,
            "total_found": preview.total_found,
            "matching_filter": preview.matching,
            "with_pdf": preview.with_pdf,
            "emails": [e.as_dict() for e in preview.emails],
        }


class PendingJobsSensor(CoordinatorEntity[AutoPrintCoordinator], SensorEntity):
    """Jobs held in the schedule queue, waiting for the print window to open.

    state      : number of queued jobs
    attributes : jobs[] list with filename, sender, queued_at, uid
                 schedule_enabled, schedule_start, schedule_end,
                 schedule_days, schedule_template
    """

    _attr_has_entity_name = True
    _attr_translation_key = SENSOR_PENDING_JOBS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "jobs"
    _attr_icon = "mdi:clock-outline"

    def __init__(self, coordinator: AutoPrintCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{SENSOR_PENDING_JOBS}"
        self._attr_device_info = _device_info(entry)
        self._entry = entry

    @property
    def native_value(self) -> int:
        data = self.coordinator.data
        return len(data.pending_jobs) if data else 0

    @property
    def extra_state_attributes(self) -> dict:
        from .const import (
            CONF_SCHEDULE_DAYS, CONF_SCHEDULE_ENABLED, CONF_SCHEDULE_END,
            CONF_SCHEDULE_START, CONF_SCHEDULE_TEMPLATE, DEFAULT_SCHEDULE_DAYS,
            DEFAULT_SCHEDULE_ENABLED, DEFAULT_SCHEDULE_END,
            DEFAULT_SCHEDULE_START, DEFAULT_SCHEDULE_TEMPLATE,
        )
        opts = self._entry.options
        data = self.coordinator.data
        return {
            "schedule_enabled": opts.get(CONF_SCHEDULE_ENABLED, DEFAULT_SCHEDULE_ENABLED),
            "schedule_start": opts.get(CONF_SCHEDULE_START, DEFAULT_SCHEDULE_START),
            "schedule_end": opts.get(CONF_SCHEDULE_END, DEFAULT_SCHEDULE_END),
            "schedule_days": list(opts.get(CONF_SCHEDULE_DAYS, DEFAULT_SCHEDULE_DAYS)),
            "schedule_template": opts.get(
                CONF_SCHEDULE_TEMPLATE, DEFAULT_SCHEDULE_TEMPLATE
            ),
            "jobs": [j.as_dict() for j in (data.pending_jobs if data else [])],
        }
