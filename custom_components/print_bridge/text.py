"""Text entities that expose Print Bridge options on the dashboard."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import re
from typing import Any

from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, TemplateError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.template import Template
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_ALLOWED_SENDERS,
    CONF_BOOKLET_PATTERNS,
    CONF_EMAIL_ARCHIVE_FOLDER,
    CONF_FOLDER_FILTER,
    CONF_QUEUE_FOLDER,
    CONF_SCHEDULE_DAYS,
    CONF_SCHEDULE_END,
    CONF_SCHEDULE_START,
    CONF_SCHEDULE_TEMPLATE,
    CONF_STATUS_REPLY_NOTIFY_SERVICE,
    DEFAULT_EMAIL_ARCHIVE_FOLDER,
    DEFAULT_QUEUE_FOLDER,
    DEFAULT_SCHEDULE_DAYS,
    DEFAULT_SCHEDULE_END,
    DEFAULT_SCHEDULE_START,
    DEFAULT_SCHEDULE_TEMPLATE,
    DEFAULT_STATUS_REPLY_NOTIFY_SERVICE,
    TEXT_ALLOWED_SENDERS,
    TEXT_BOOKLET_PATTERNS,
    TEXT_EMAIL_ARCHIVE_FOLDER,
    TEXT_FOLDER_FILTER,
    TEXT_QUEUE_FOLDER,
    TEXT_SCHEDULE_DAYS,
    TEXT_SCHEDULE_END,
    TEXT_SCHEDULE_START,
    TEXT_SCHEDULE_TEMPLATE,
    TEXT_STATUS_REPLY_NOTIFY_SERVICE,
)
from .coordinator import AutoPrintCoordinator
from .sensor import _device_info

_HHMM_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_DAY_ALIASES = {
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


@dataclass(frozen=True)
class _OptionText:
    key: str
    translation_key: str
    default: Any
    icon: str
    parser: Callable[[str], Any]
    formatter: Callable[[Any], str]
    pattern: re.Pattern[str] | None = None


def _split_lines(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[\n,]+", value) if part.strip()]


def _split_lower_lines(value: str) -> list[str]:
    return [part.lower() for part in _split_lines(value)]


def _format_list(value: Any) -> str:
    if not value:
        return ""
    return "\n".join(str(item) for item in value)


def _parse_schedule_days(value: str) -> list[str]:
    days: list[str] = []
    for raw_day in re.split(r"[\s,;]+", value):
        raw_day = raw_day.strip().lower()
        if not raw_day:
            continue
        day = _DAY_ALIASES.get(raw_day)
        if day is None:
            raise HomeAssistantError(
                f"Unknown schedule day '{raw_day}'. Use mon, tue, wed, thu, fri, sat, sun."
            )
        if day not in days:
            days.append(day)
    return days


_TEXTS: tuple[_OptionText, ...] = (
    _OptionText(
        CONF_ALLOWED_SENDERS,
        TEXT_ALLOWED_SENDERS,
        (),
        "mdi:email-check-outline",
        _split_lower_lines,
        _format_list,
    ),
    _OptionText(
        CONF_FOLDER_FILTER,
        TEXT_FOLDER_FILTER,
        (),
        "mdi:folder-filter-outline",
        _split_lines,
        _format_list,
    ),
    _OptionText(
        CONF_BOOKLET_PATTERNS,
        TEXT_BOOKLET_PATTERNS,
        (),
        "mdi:book-open-page-variant-outline",
        _split_lines,
        _format_list,
    ),
    _OptionText(
        CONF_QUEUE_FOLDER,
        TEXT_QUEUE_FOLDER,
        DEFAULT_QUEUE_FOLDER,
        "mdi:folder-outline",
        lambda value: value.strip(),
        lambda value: str(value or DEFAULT_QUEUE_FOLDER),
    ),
    _OptionText(
        CONF_EMAIL_ARCHIVE_FOLDER,
        TEXT_EMAIL_ARCHIVE_FOLDER,
        DEFAULT_EMAIL_ARCHIVE_FOLDER,
        "mdi:archive-outline",
        lambda value: value.strip(),
        lambda value: str(value or DEFAULT_EMAIL_ARCHIVE_FOLDER),
    ),
    _OptionText(
        CONF_STATUS_REPLY_NOTIFY_SERVICE,
        TEXT_STATUS_REPLY_NOTIFY_SERVICE,
        DEFAULT_STATUS_REPLY_NOTIFY_SERVICE,
        "mdi:email-sync-outline",
        lambda value: value.strip(),
        lambda value: str(value or ""),
    ),
    _OptionText(
        CONF_SCHEDULE_START,
        TEXT_SCHEDULE_START,
        DEFAULT_SCHEDULE_START,
        "mdi:clock-start",
        lambda value: value.strip(),
        lambda value: str(value or DEFAULT_SCHEDULE_START),
        _HHMM_PATTERN,
    ),
    _OptionText(
        CONF_SCHEDULE_END,
        TEXT_SCHEDULE_END,
        DEFAULT_SCHEDULE_END,
        "mdi:clock-end",
        lambda value: value.strip(),
        lambda value: str(value or DEFAULT_SCHEDULE_END),
        _HHMM_PATTERN,
    ),
    _OptionText(
        CONF_SCHEDULE_DAYS,
        TEXT_SCHEDULE_DAYS,
        DEFAULT_SCHEDULE_DAYS,
        "mdi:calendar-week",
        _parse_schedule_days,
        _format_list,
    ),
    _OptionText(
        CONF_SCHEDULE_TEMPLATE,
        TEXT_SCHEDULE_TEMPLATE,
        DEFAULT_SCHEDULE_TEMPLATE,
        "mdi:code-braces",
        lambda value: value.strip(),
        lambda value: str(value or ""),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AutoPrintCoordinator = entry.runtime_data
    async_add_entities(
        [OptionText(coordinator, entry, description) for description in _TEXTS]
    )


class OptionText(CoordinatorEntity[AutoPrintCoordinator], TextEntity):
    """String or string-list Print Bridge option exposed as a text entity."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AutoPrintCoordinator,
        entry: ConfigEntry,
        description: _OptionText,
    ) -> None:
        super().__init__(coordinator)
        self._description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.translation_key}"
        self._attr_translation_key = description.translation_key
        self._attr_icon = description.icon
        self._attr_pattern = description.pattern.pattern if description.pattern else None
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> str:
        value = self.coordinator._entry.options.get(
            self._description.key, self._description.default
        )
        return self._description.formatter(value)

    async def async_set_value(self, value: str) -> None:
        if self._description.pattern and not self._description.pattern.match(value):
            raise HomeAssistantError(
                f"Value for {self._description.key} must use HH:MM format."
            )

        if self._description.key == CONF_SCHEDULE_TEMPLATE and value.strip():
            try:
                Template(value, self.coordinator.hass).ensure_valid()
            except TemplateError as exc:
                raise HomeAssistantError("Invalid schedule template.") from exc

        parsed = self._description.parser(value)
        self.coordinator.set_option(self._description.key, parsed)
