"""Auto Print integration setup."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv

from .const import (
    CONF_DUPLEX_MODE,
    DATA_COORDINATOR,
    DOMAIN,
    DUPLEX_MODES,
    FIELD_BOOKLET,
    FIELD_DUPLEX,
    FIELD_FILE_PATH,
    SERVICE_CLEAR_QUEUE,
    SERVICE_PRINT_FILE,
)
from .coordinator import AutoPrintCoordinator

logger = logging.getLogger(__name__)

PLATFORMS = ["sensor", "binary_sensor", "button"]

_PRINT_FILE_SCHEMA = vol.Schema(
    {
        vol.Required(FIELD_FILE_PATH): cv.string,
        vol.Optional(FIELD_DUPLEX): vol.In(DUPLEX_MODES),
        vol.Optional(FIELD_BOOKLET, default=False): cv.boolean,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Auto Print from a config entry."""
    coordinator = AutoPrintCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        DATA_COORDINATOR: coordinator,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register services once (guard against duplicate registration on reload).
    if not hass.services.has_service(DOMAIN, SERVICE_PRINT_FILE):
        _register_services(hass)

    # Re-create services on options update so coordinators pick up new settings.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    # Remove services only when no entries remain.
    if not hass.data.get(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE_PRINT_FILE)
        hass.services.async_remove(DOMAIN, SERVICE_CLEAR_QUEUE)

    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload entry when options change so the coordinator picks up the new poll interval."""
    await hass.config_entries.async_reload(entry.entry_id)


def _register_services(hass: HomeAssistant) -> None:
    """Register the integration-level services."""

    async def _handle_print_file(call: ServiceCall) -> None:
        file_path: str = call.data[FIELD_FILE_PATH]
        duplex: str | None = call.data.get(FIELD_DUPLEX)
        booklet: bool = call.data.get(FIELD_BOOKLET, False)

        # Dispatch to the first available coordinator.
        coordinator = _get_any_coordinator(hass)
        result = await coordinator.async_print_file(file_path, duplex, booklet)
        if not result.success:
            raise HomeAssistantError(
                f"Print job failed for '{result.filename}': {result.error}"
            )

    async def _handle_clear_queue(call: ServiceCall) -> None:
        coordinator = _get_any_coordinator(hass)
        deleted = await coordinator.async_clear_queue()
        logger.debug("Cleared %d file(s) from the print queue", deleted)

    hass.services.async_register(
        DOMAIN,
        SERVICE_PRINT_FILE,
        _handle_print_file,
        schema=_PRINT_FILE_SCHEMA,
    )
    hass.services.async_register(DOMAIN, SERVICE_CLEAR_QUEUE, _handle_clear_queue)


def _get_any_coordinator(hass: HomeAssistant) -> AutoPrintCoordinator:
    """Return the coordinator for the first loaded config entry."""
    entries = hass.data.get(DOMAIN, {})
    if not entries:
        raise HomeAssistantError("Auto Print is not configured")
    entry_data = next(iter(entries.values()))
    return entry_data[DATA_COORDINATOR]
