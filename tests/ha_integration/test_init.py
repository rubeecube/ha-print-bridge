"""Integration setup / teardown + imap_content event listener tests.

Golden rules applied:
  - async_setup_entry succeeds (state = LOADED).
  - imap_content event listener is registered during setup.
  - All five services are registered after setup.
  - Services are removed after the last entry unloads.
  - async_unload_entry succeeds (state = NOT_LOADED).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.print_bridge.const import (
    DOMAIN,
    SERVICE_CHECK_FILTER,
    SERVICE_CLEAR_QUEUE,
    SERVICE_PRINT_EMAIL,
    SERVICE_PRINT_FILE,
    SERVICE_PROCESS_IMAP_PART,
    SERVICE_RETRY_JOB,
)

from .conftest import MOCK_CONFIG_DATA, MOCK_OPTIONS


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

async def test_setup_entry_loads_successfully(
    hass: HomeAssistant,
    mock_coordinator_update,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA, options=MOCK_OPTIONS)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


async def test_setup_registers_services(
    hass: HomeAssistant,
    mock_coordinator_update,
) -> None:
    """All services must be registered after a successful setup."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA, options=MOCK_OPTIONS)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    for svc in (
        SERVICE_PRINT_FILE,
        SERVICE_PRINT_EMAIL,
        SERVICE_CLEAR_QUEUE,
        SERVICE_PROCESS_IMAP_PART,
        SERVICE_CHECK_FILTER,
        SERVICE_RETRY_JOB,
    ):
        assert hass.services.has_service(DOMAIN, svc), f"Service {svc!r} not registered"


async def test_setup_registers_all_platforms(
    hass: HomeAssistant,
    mock_coordinator_update,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA, options=MOCK_OPTIONS)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    platform_domains = {eid.split(".")[0] for eid in hass.states.async_entity_ids()}
    assert "sensor" in platform_domains
    assert "binary_sensor" in platform_domains
    assert "button" in platform_domains
    assert "select" in platform_domains
    assert "switch" in platform_domains
    assert "text" in platform_domains


def test_brand_assets_are_packaged() -> None:
    """HA serves custom integration icons from custom_components/<domain>/brand."""
    from pathlib import Path

    brand_dir = Path(__file__).parents[2] / "custom_components" / "print_bridge" / "brand"
    for filename in ("icon.png", "logo.png", "dark_icon.png", "dark_logo.png"):
        assert (brand_dir / filename).is_file()


async def test_imap_content_listener_registered(
    hass: HomeAssistant,
    mock_coordinator_update,
) -> None:
    """async_setup_entry must subscribe to 'imap_content' on the HA event bus.

    We verify this by patching AutoPrintCoordinator.async_handle_imap_event at
    class level BEFORE setup, so the bound method captured by the bus listener
    IS the mock.  Then we fire an imap_content event and assert it was received.
    """
    from custom_components.print_bridge.coordinator import AutoPrintCoordinator

    with patch.object(
        AutoPrintCoordinator, "async_handle_imap_event", new=AsyncMock()
    ) as mock_handler:
        entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA, options=MOCK_OPTIONS)
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        hass.bus.async_fire(
            "imap_content",
            {"sender": "test@example.com", "entry_id": "fake", "uid": "1", "parts": {}},
        )
        await hass.async_block_till_done()

    assert mock_handler.called, (
        "Expected AutoPrintCoordinator.async_handle_imap_event to be called "
        "when an imap_content event is fired, but it was not."
    )


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------

async def test_unload_sets_state_not_loaded(
    hass: HomeAssistant,
    mock_coordinator_update,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA, options=MOCK_OPTIONS)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_unload_removes_services_when_last_entry_unloads(
    hass: HomeAssistant,
    mock_coordinator_update,
) -> None:
    """All services must be removed when the last config entry is unloaded."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA, options=MOCK_OPTIONS)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    for svc in (
        SERVICE_PRINT_FILE,
        SERVICE_PRINT_EMAIL,
        SERVICE_CLEAR_QUEUE,
        SERVICE_PROCESS_IMAP_PART,
        SERVICE_CHECK_FILTER,
        SERVICE_RETRY_JOB,
    ):
        assert not hass.services.has_service(DOMAIN, svc), f"Service {svc!r} still registered after unload"
