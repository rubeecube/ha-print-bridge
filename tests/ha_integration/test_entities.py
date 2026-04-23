"""Entity state tests for the Auto Print integration.

Golden rules applied:
  - Sensor states reflect coordinator data.
  - Binary sensor tracks printer_online flag.
  - Button press dispatches a print job (success and failure paths).
  - All entities have unique_ids (required for HA entity registry).

Entities are looked up by unique_id (via the entity registry) rather than by
entity_id string because translations are not loaded in the test environment,
so entity_ids may not contain the translated entity names.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.auto_print.const import (
    BINARY_SENSOR_PRINTER_ONLINE,
    BUTTON_TEST_PAGE,
    DOMAIN,
    SENSOR_LAST_JOB,
    SENSOR_QUEUE_DEPTH,
)
from custom_components.auto_print.coordinator import AutoPrintData, PrintJobResult

from .conftest import MOCK_CONFIG_DATA, MOCK_OPTIONS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _setup_entry(hass: HomeAssistant, coordinator_data: AutoPrintData) -> MockConfigEntry:
    """Add a config entry and mock the coordinator with *coordinator_data*."""
    with patch(
        "custom_components.auto_print.coordinator.AutoPrintCoordinator._async_update_data",
        return_value=coordinator_data,
    ):
        entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA, options=MOCK_OPTIONS)
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _entity_id_by_suffix(hass: HomeAssistant, entry: MockConfigEntry, suffix: str) -> str:
    """Return the entity_id whose unique_id ends with *suffix*."""
    reg = er.async_get(hass)
    for entity_entry in reg.entities.values():
        if (
            entity_entry.config_entry_id == entry.entry_id
            and entity_entry.unique_id
            and entity_entry.unique_id.endswith(suffix)
        ):
            return entity_entry.entity_id
    raise AssertionError(
        f"No entity with unique_id suffix '{suffix}' found for entry {entry.entry_id}"
    )


# ---------------------------------------------------------------------------
# Queue depth sensor
# ---------------------------------------------------------------------------

async def test_queue_depth_sensor_shows_coordinator_value(hass: HomeAssistant) -> None:
    """queue_depth sensor must reflect the value from coordinator data."""
    data = AutoPrintData(queue_depth=3, printer_online=True)
    entry = await _setup_entry(hass, data)

    entity_id = _entity_id_by_suffix(hass, entry, SENSOR_QUEUE_DEPTH)
    assert hass.states.get(entity_id).state == "3"


async def test_queue_depth_sensor_zero_when_empty(hass: HomeAssistant) -> None:
    data = AutoPrintData(queue_depth=0, printer_online=True)
    entry = await _setup_entry(hass, data)

    entity_id = _entity_id_by_suffix(hass, entry, SENSOR_QUEUE_DEPTH)
    assert hass.states.get(entity_id).state == "0"


# ---------------------------------------------------------------------------
# Last-job sensor
# ---------------------------------------------------------------------------

async def test_last_job_sensor_reflects_success(hass: HomeAssistant) -> None:
    last = PrintJobResult(filename="doc.pdf", success=True)
    data = AutoPrintData(queue_depth=0, printer_online=True, last_job=last, job_history=[last])
    entry = await _setup_entry(hass, data)

    entity_id = _entity_id_by_suffix(hass, entry, SENSOR_LAST_JOB)
    assert hass.states.get(entity_id).state == "success"


async def test_last_job_sensor_reflects_failure(hass: HomeAssistant) -> None:
    last = PrintJobResult(filename="bad.pdf", success=False, error="HTTP 500")
    data = AutoPrintData(queue_depth=0, printer_online=True, last_job=last, job_history=[last])
    entry = await _setup_entry(hass, data)

    entity_id = _entity_id_by_suffix(hass, entry, SENSOR_LAST_JOB)
    state = hass.states.get(entity_id)
    assert state.state == "failed"
    assert state.attributes.get("last_status") == "HTTP 500"


async def test_last_job_sensor_unknown_when_no_jobs(hass: HomeAssistant) -> None:
    data = AutoPrintData(queue_depth=0, printer_online=True, last_job=None)
    entry = await _setup_entry(hass, data)

    entity_id = _entity_id_by_suffix(hass, entry, SENSOR_LAST_JOB)
    # No jobs yet — state is None / unknown / unavailable.
    assert hass.states.get(entity_id).state in ("unknown", "unavailable", "None")


# ---------------------------------------------------------------------------
# Printer online binary sensor
# ---------------------------------------------------------------------------

async def test_printer_online_binary_sensor_on(hass: HomeAssistant) -> None:
    data = AutoPrintData(queue_depth=0, printer_online=True)
    entry = await _setup_entry(hass, data)

    entity_id = _entity_id_by_suffix(hass, entry, BINARY_SENSOR_PRINTER_ONLINE)
    assert hass.states.get(entity_id).state == "on"


async def test_printer_online_binary_sensor_off(hass: HomeAssistant) -> None:
    data = AutoPrintData(queue_depth=0, printer_online=False)
    entry = await _setup_entry(hass, data)

    entity_id = _entity_id_by_suffix(hass, entry, BINARY_SENSOR_PRINTER_ONLINE)
    assert hass.states.get(entity_id).state == "off"


# ---------------------------------------------------------------------------
# Test page button
# ---------------------------------------------------------------------------

async def test_button_press_succeeds(hass: HomeAssistant) -> None:
    """Pressing the test-page button must dispatch a successful print job."""
    data = AutoPrintData(queue_depth=0, printer_online=True)
    entry = await _setup_entry(hass, data)

    entity_id = _entity_id_by_suffix(hass, entry, BUTTON_TEST_PAGE)
    success_result = PrintJobResult(filename="auto_print_test_page.pdf", success=True)

    with patch(
        "custom_components.auto_print.coordinator.AutoPrintCoordinator._async_send_print_job",
        return_value=success_result,
    ):
        await hass.services.async_call(
            "button", "press", {"entity_id": entity_id}, blocking=True
        )


async def test_button_press_raises_on_failure(hass: HomeAssistant) -> None:
    """A failed print job must raise HomeAssistantError so HA shows an error."""
    data = AutoPrintData(queue_depth=0, printer_online=True)
    entry = await _setup_entry(hass, data)

    entity_id = _entity_id_by_suffix(hass, entry, BUTTON_TEST_PAGE)
    failure = PrintJobResult(filename="auto_print_test_page.pdf", success=False, error="HTTP 503")

    with patch(
        "custom_components.auto_print.coordinator.AutoPrintCoordinator._async_send_print_job",
        return_value=failure,
    ):
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "button", "press", {"entity_id": entity_id}, blocking=True
            )


# ---------------------------------------------------------------------------
# Unique IDs (required by HA golden rules)
# ---------------------------------------------------------------------------

async def test_all_entities_have_unique_ids(hass: HomeAssistant) -> None:
    """Every entity registered by the integration must have a unique_id.

    This is required for the entity registry (enabling disable/rename from UI).
    """
    data = AutoPrintData(queue_depth=0, printer_online=True)
    entry = await _setup_entry(hass, data)

    reg = er.async_get(hass)
    entries = [e for e in reg.entities.values() if e.config_entry_id == entry.entry_id]

    assert entries, "No entities were registered for the config entry"
    for reg_entry in entries:
        assert reg_entry.unique_id, (
            f"Entity '{reg_entry.entity_id}' has no unique_id — "
            "users cannot manage it from the entity registry"
        )
