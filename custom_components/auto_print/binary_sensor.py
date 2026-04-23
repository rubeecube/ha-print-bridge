"""Binary sensor entities for the Auto Print integration."""

from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import BINARY_SENSOR_PRINTER_ONLINE, DATA_COORDINATOR, DOMAIN
from .coordinator import AutoPrintCoordinator
from .sensor import _device_info

logger = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AutoPrintCoordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
    async_add_entities([PrinterOnlineSensor(coordinator, entry)])


class PrinterOnlineSensor(CoordinatorEntity[AutoPrintCoordinator], BinarySensorEntity):
    """Reports whether the CUPS server is reachable."""

    _attr_has_entity_name = True
    _attr_translation_key = BINARY_SENSOR_PRINTER_ONLINE
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: AutoPrintCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{BINARY_SENSOR_PRINTER_ONLINE}"
        self._attr_device_info = _device_info(entry)

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.data and self.coordinator.data.printer_online)
