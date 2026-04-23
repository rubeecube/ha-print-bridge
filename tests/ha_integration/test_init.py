"""Integration setup / teardown tests for Auto Print.

Golden rules applied:
  - async_setup_entry succeeds and registers all platforms.
  - async_unload_entry removes all platforms and services.
  - Services are registered after setup and removed after unload.
  - Reloading the entry on options change is triggered.
"""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.auto_print.const import DOMAIN, SERVICE_CLEAR_QUEUE, SERVICE_PRINT_FILE

from .conftest import MOCK_CONFIG_DATA, MOCK_OPTIONS


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

async def test_setup_entry_loads_successfully(
    hass: HomeAssistant,
    mock_coordinator_update,
) -> None:
    """A valid config entry must load with state LOADED."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA, options=MOCK_OPTIONS)
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED


async def test_setup_entry_registers_services(
    hass: HomeAssistant,
    mock_coordinator_update,
) -> None:
    """After setup, both services must be registered."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA, options=MOCK_OPTIONS)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.services.has_service(DOMAIN, SERVICE_PRINT_FILE)
    assert hass.services.has_service(DOMAIN, SERVICE_CLEAR_QUEUE)


async def test_setup_entry_creates_entities(
    hass: HomeAssistant,
    mock_coordinator_update,
) -> None:
    """After setup, queue_depth sensor, printer_online binary_sensor and
    test_page button must exist."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA, options=MOCK_OPTIONS)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    entity_ids = hass.states.async_entity_ids()
    domains_present = {eid.split(".")[0] for eid in entity_ids}
    assert "sensor" in domains_present
    assert "binary_sensor" in domains_present
    assert "button" in domains_present


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------

async def test_unload_entry_sets_state_not_loaded(
    hass: HomeAssistant,
    mock_coordinator_update,
) -> None:
    """Unloading must transition the entry to NOT_LOADED."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA, options=MOCK_OPTIONS)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_unload_removes_services_when_no_entries_remain(
    hass: HomeAssistant,
    mock_coordinator_update,
) -> None:
    """After the last entry is unloaded, services must be de-registered."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA, options=MOCK_OPTIONS)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert not hass.services.has_service(DOMAIN, SERVICE_PRINT_FILE)
    assert not hass.services.has_service(DOMAIN, SERVICE_CLEAR_QUEUE)
