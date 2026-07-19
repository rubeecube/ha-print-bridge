"""Switch entities that expose Print Bridge options on the dashboard."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_AUTO_DELETE,
    CONF_AUTO_PRINT_ENABLED,
    CONF_NOTIFY_ON_FAILURE,
    CONF_NOTIFY_ON_SUCCESS,
    CONF_REVERSE_ORDER,
    CONF_SCHEDULE_ENABLED,
    CONF_SIGNAL_ENABLED,
    CONF_STATUS_REPLY_ENABLED,
    DEFAULT_AUTO_DELETE,
    DEFAULT_AUTO_PRINT_ENABLED,
    DEFAULT_NOTIFY_ON_FAILURE,
    DEFAULT_NOTIFY_ON_SUCCESS,
    DEFAULT_REVERSE_ORDER,
    DEFAULT_SCHEDULE_ENABLED,
    DEFAULT_SIGNAL_ENABLED,
    DEFAULT_STATUS_REPLY_ENABLED,
    SWITCH_AUTO_DELETE,
    SWITCH_AUTO_PRINT_ENABLED,
    SWITCH_NOTIFY_ON_FAILURE,
    SWITCH_NOTIFY_ON_SUCCESS,
    SWITCH_REVERSE_ORDER,
    SWITCH_SCHEDULE_ENABLED,
    SWITCH_SIGNAL_ENABLED,
    SWITCH_STATUS_REPLY_ENABLED,
)
from .coordinator import AutoPrintCoordinator
from .sensor import _device_info


@dataclass(frozen=True)
class _OptionSwitch:
    key: str
    translation_key: str
    default: bool
    icon: str


_SWITCHES: tuple[_OptionSwitch, ...] = (
    _OptionSwitch(
        CONF_AUTO_PRINT_ENABLED,
        SWITCH_AUTO_PRINT_ENABLED,
        DEFAULT_AUTO_PRINT_ENABLED,
        "mdi:printer-wireless",
    ),
    _OptionSwitch(
        CONF_AUTO_DELETE,
        SWITCH_AUTO_DELETE,
        DEFAULT_AUTO_DELETE,
        "mdi:file-remove-outline",
    ),
    _OptionSwitch(
        CONF_NOTIFY_ON_FAILURE,
        SWITCH_NOTIFY_ON_FAILURE,
        DEFAULT_NOTIFY_ON_FAILURE,
        "mdi:alert-circle-outline",
    ),
    _OptionSwitch(
        CONF_NOTIFY_ON_SUCCESS,
        SWITCH_NOTIFY_ON_SUCCESS,
        DEFAULT_NOTIFY_ON_SUCCESS,
        "mdi:check-circle-outline",
    ),
    _OptionSwitch(
        CONF_STATUS_REPLY_ENABLED,
        SWITCH_STATUS_REPLY_ENABLED,
        DEFAULT_STATUS_REPLY_ENABLED,
        "mdi:email-sync-outline",
    ),
    _OptionSwitch(
        CONF_REVERSE_ORDER,
        SWITCH_REVERSE_ORDER,
        DEFAULT_REVERSE_ORDER,
        "mdi:sort-descending",
    ),
    _OptionSwitch(
        CONF_SIGNAL_ENABLED,
        SWITCH_SIGNAL_ENABLED,
        DEFAULT_SIGNAL_ENABLED,
        "mdi:message-text-lock-outline",
    ),
    _OptionSwitch(
        CONF_SCHEDULE_ENABLED,
        SWITCH_SCHEDULE_ENABLED,
        DEFAULT_SCHEDULE_ENABLED,
        "mdi:calendar-clock",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AutoPrintCoordinator = entry.runtime_data
    async_add_entities(
        [OptionSwitch(coordinator, entry, description) for description in _SWITCHES]
    )


class OptionSwitch(CoordinatorEntity[AutoPrintCoordinator], SwitchEntity):
    """Boolean Print Bridge option exposed as a switch."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AutoPrintCoordinator,
        entry: ConfigEntry,
        description: _OptionSwitch,
    ) -> None:
        super().__init__(coordinator)
        self._description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.translation_key}"
        self._attr_translation_key = description.translation_key
        self._attr_icon = description.icon
        self._attr_device_info = _device_info(entry)

    @property
    def is_on(self) -> bool:
        if self._description.key == CONF_SIGNAL_ENABLED:
            return self.coordinator._signal_enabled
        return bool(
            self.coordinator._entry.options.get(
                self._description.key, self._description.default
            )
        )

    @property
    def available(self) -> bool:
        if self._description.key == CONF_SIGNAL_ENABLED:
            return self.coordinator.signal_rest_integration_detected
        return super().available

    async def async_turn_on(self, **kwargs) -> None:
        if (
            self._description.key == CONF_SIGNAL_ENABLED
            and not self.coordinator.signal_rest_integration_detected
        ):
            raise HomeAssistantError(
                "Configure the Home Assistant Signal Messenger integration before "
                "enabling Signal intake."
            )
        self.coordinator.set_option(self._description.key, True)

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.set_option(self._description.key, False)
