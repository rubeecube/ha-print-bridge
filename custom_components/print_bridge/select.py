"""Select entities for choosing the active mailbox and target printer."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_DUPLEX_MODE,
    CONF_EMAIL_ACTION,
    CONF_PRINTER_NAME,
    DEFAULT_DUPLEX_MODE,
    DEFAULT_EMAIL_ACTION,
    DOMAIN,
    DUPLEX_MODES,
    EMAIL_ACTIONS,
    SELECT_DUPLEX_MODE,
    SELECT_EMAIL_ACTION,
    SELECT_IMAP_ACCOUNT,
    SELECT_TARGET_PRINTER,
)
from .coordinator import AutoPrintCoordinator
from .sensor import _device_info


@dataclass(frozen=True)
class _OptionSelect:
    key: str
    translation_key: str
    default: str
    choices: dict[str, str]
    icon: str


_OPTION_SELECTS: tuple[_OptionSelect, ...] = (
    _OptionSelect(
        CONF_DUPLEX_MODE,
        SELECT_DUPLEX_MODE,
        DEFAULT_DUPLEX_MODE,
        DUPLEX_MODES,
        "mdi:page-layout-body",
    ),
    _OptionSelect(
        CONF_EMAIL_ACTION,
        SELECT_EMAIL_ACTION,
        DEFAULT_EMAIL_ACTION,
        EMAIL_ACTIONS,
        "mdi:email-arrow-right-outline",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AutoPrintCoordinator = entry.runtime_data
    async_add_entities(
        [
            ImapAccountSelect(coordinator, entry),
            TargetPrinterSelect(coordinator, entry),
            *[
                OptionSelect(coordinator, entry, description)
                for description in _OPTION_SELECTS
            ],
        ]
    )


def _imap_label(entry: ConfigEntry) -> str:
    username = entry.data.get("username") or entry.title or "IMAP account"
    server = entry.data.get("server", "")
    folder = entry.data.get("folder", "INBOX")
    if server:
        return f"{username} ({server}/{folder})"
    return username


def _printer_label(entry: ConfigEntry) -> str:
    if printer_name := entry.data.get(CONF_PRINTER_NAME):
        return printer_name
    if entry.title and entry.title != "Mock Title":
        return entry.title.removeprefix("Print Bridge - ").removeprefix("Print Bridge — ")
    return "Direct Printer"


def _label_map(
    entries: list[ConfigEntry], label_fn: Callable[[ConfigEntry], str]
) -> dict[str, str]:
    base_labels = {entry.entry_id: label_fn(entry) for entry in entries}
    counts = Counter(base_labels.values())
    mapping: dict[str, str] = {}
    for entry in entries:
        label = base_labels[entry.entry_id]
        if counts[label] > 1:
            label = f"{label} [{entry.entry_id[:8]}]"
        mapping[label] = entry.entry_id
    return mapping


class ImapAccountSelect(CoordinatorEntity[AutoPrintCoordinator], SelectEntity):
    """Select which HA IMAP account is scanned by Check Filter."""

    _attr_has_entity_name = True
    _attr_translation_key = SELECT_IMAP_ACCOUNT
    _attr_icon = "mdi:email-sync-outline"

    def __init__(self, coordinator: AutoPrintCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{SELECT_IMAP_ACCOUNT}"
        self._attr_device_info = _device_info(entry)

    def _options_by_label(self) -> dict[str, str]:
        return _label_map(
            list(self.coordinator.hass.config_entries.async_entries("imap")),
            _imap_label,
        )

    @property
    def available(self) -> bool:
        return bool(self._options_by_label())

    @property
    def options(self) -> list[str]:
        return list(self._options_by_label())

    @property
    def current_option(self) -> str | None:
        selected = self.coordinator.selected_imap_entry_id
        for label, entry_id in self._options_by_label().items():
            if entry_id == selected:
                return label
        return None

    async def async_select_option(self, option: str) -> None:
        mapping = self._options_by_label()
        if option not in mapping:
            raise HomeAssistantError(f"Unknown IMAP account option: {option}")
        self.coordinator.set_selected_imap_entry_id(mapping[option])
        self.async_write_ha_state()


class TargetPrinterSelect(CoordinatorEntity[AutoPrintCoordinator], SelectEntity):
    """Select which Print Bridge printer receives dashboard print actions."""

    _attr_has_entity_name = True
    _attr_translation_key = SELECT_TARGET_PRINTER
    _attr_icon = "mdi:printer-cog"

    def __init__(self, coordinator: AutoPrintCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{SELECT_TARGET_PRINTER}"
        self._attr_device_info = _device_info(entry)

    def _options_by_label(self) -> dict[str, str]:
        return _label_map(
            list(self.coordinator.hass.config_entries.async_entries(DOMAIN)),
            _printer_label,
        )

    @property
    def available(self) -> bool:
        return bool(self._options_by_label())

    @property
    def options(self) -> list[str]:
        return list(self._options_by_label())

    @property
    def current_option(self) -> str | None:
        selected = self.coordinator.selected_printer_entry_id
        for label, entry_id in self._options_by_label().items():
            if entry_id == selected:
                return label
        return None

    async def async_select_option(self, option: str) -> None:
        mapping = self._options_by_label()
        if option not in mapping:
            raise HomeAssistantError(f"Unknown printer option: {option}")
        self.coordinator.set_selected_printer_entry_id(mapping[option])
        self.async_write_ha_state()


class OptionSelect(CoordinatorEntity[AutoPrintCoordinator], SelectEntity):
    """Choice-based Print Bridge option exposed as a select entity."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AutoPrintCoordinator,
        entry: ConfigEntry,
        description: _OptionSelect,
    ) -> None:
        super().__init__(coordinator)
        self._description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.translation_key}"
        self._attr_translation_key = description.translation_key
        self._attr_icon = description.icon
        self._attr_device_info = _device_info(entry)

    @property
    def options(self) -> list[str]:
        return list(self._description.choices.values())

    @property
    def current_option(self) -> str | None:
        value = self.coordinator._entry.options.get(
            self._description.key, self._description.default
        )
        return self._description.choices.get(value, value)

    async def async_select_option(self, option: str) -> None:
        label_to_value = {label: value for value, label in self._description.choices.items()}
        if option not in label_to_value:
            raise HomeAssistantError(f"Unknown option: {option}")
        self.coordinator.set_option(self._description.key, label_to_value[option])
        self.async_write_ha_state()
