"""Entity state tests for the Print Bridge integration.

Golden rules applied:
  - Sensor states reflect coordinator data.
  - Binary sensor tracks printer_online flag.
  - Button press dispatches a print job (success and failure paths).
  - All entities have unique_ids (required for HA entity registry).

Entities are looked up by unique_id suffix (via the entity registry) since
translations are not loaded in the test environment.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.print_bridge.const import (
    BINARY_SENSOR_PRINTER_ONLINE,
    BUTTON_TEST_PAGE,
    DOMAIN,
    SENSOR_LAST_JOB,
    SENSOR_QUEUE_DEPTH,
)
from custom_components.print_bridge.coordinator import AutoPrintData, PrintJobResult

from .conftest import MOCK_CONFIG_DATA, MOCK_OPTIONS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _setup(hass: HomeAssistant, data: AutoPrintData) -> MockConfigEntry:
    with patch(
        "custom_components.print_bridge.coordinator.AutoPrintCoordinator._async_update_data",
        return_value=data,
    ):
        entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA, options=MOCK_OPTIONS)
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _entity_id(hass: HomeAssistant, entry: MockConfigEntry, suffix: str) -> str:
    reg = er.async_get(hass)
    for e in reg.entities.values():
        if e.config_entry_id == entry.entry_id and (e.unique_id or "").endswith(suffix):
            return e.entity_id
    raise AssertionError(f"No entity with unique_id suffix '{suffix}' for entry {entry.entry_id}")


# ---------------------------------------------------------------------------
# Queue depth sensor
# ---------------------------------------------------------------------------

async def test_queue_depth_sensor(hass: HomeAssistant) -> None:
    entry = await _setup(hass, AutoPrintData(queue_depth=5, printer_online=True))
    assert hass.states.get(_entity_id(hass, entry, SENSOR_QUEUE_DEPTH)).state == "5"


async def test_queue_depth_zero(hass: HomeAssistant) -> None:
    entry = await _setup(hass, AutoPrintData(queue_depth=0, printer_online=True))
    assert hass.states.get(_entity_id(hass, entry, SENSOR_QUEUE_DEPTH)).state == "0"


# ---------------------------------------------------------------------------
# Last-job sensor
# ---------------------------------------------------------------------------

async def test_last_job_success(hass: HomeAssistant) -> None:
    job = PrintJobResult(filename="doc.pdf", success=True)
    data = AutoPrintData(queue_depth=0, printer_online=True, last_job=job, job_history=[job])
    entry = await _setup(hass, data)
    assert hass.states.get(_entity_id(hass, entry, SENSOR_LAST_JOB)).state == "success"


async def test_last_job_failure(hass: HomeAssistant) -> None:
    job = PrintJobResult(filename="bad.pdf", success=False, error="HTTP 500")
    data = AutoPrintData(queue_depth=0, printer_online=True, last_job=job, job_history=[job])
    entry = await _setup(hass, data)
    state = hass.states.get(_entity_id(hass, entry, SENSOR_LAST_JOB))
    assert state.state == "failed"
    assert state.attributes.get("last_status") == "HTTP 500"


async def test_last_job_no_jobs(hass: HomeAssistant) -> None:
    entry = await _setup(hass, AutoPrintData(queue_depth=0, printer_online=True))
    state = hass.states.get(_entity_id(hass, entry, SENSOR_LAST_JOB))
    assert state.state in ("unknown", "unavailable", "None")


# ---------------------------------------------------------------------------
# Printer online binary sensor
# ---------------------------------------------------------------------------

async def test_printer_online_on(hass: HomeAssistant) -> None:
    entry = await _setup(hass, AutoPrintData(queue_depth=0, printer_online=True))
    assert hass.states.get(_entity_id(hass, entry, BINARY_SENSOR_PRINTER_ONLINE)).state == "on"


async def test_printer_online_off(hass: HomeAssistant) -> None:
    entry = await _setup(hass, AutoPrintData(queue_depth=0, printer_online=False))
    assert hass.states.get(_entity_id(hass, entry, BINARY_SENSOR_PRINTER_ONLINE)).state == "off"


# ---------------------------------------------------------------------------
# Test page button
# ---------------------------------------------------------------------------

async def test_button_press_success(hass: HomeAssistant) -> None:
    entry = await _setup(hass, AutoPrintData(queue_depth=0, printer_online=True))
    btn = _entity_id(hass, entry, BUTTON_TEST_PAGE)
    success = PrintJobResult(filename="auto_print_test_page.pdf", success=True)

    with patch(
        "custom_components.print_bridge.coordinator.AutoPrintCoordinator.async_send_print_job",
        return_value=success,
    ):
        await hass.services.async_call("button", "press", {"entity_id": btn}, blocking=True)


async def test_button_press_failure_raises(hass: HomeAssistant) -> None:
    entry = await _setup(hass, AutoPrintData(queue_depth=0, printer_online=True))
    btn = _entity_id(hass, entry, BUTTON_TEST_PAGE)
    failure = PrintJobResult(filename="auto_print_test_page.pdf", success=False, error="HTTP 503")

    with (
        patch(
            "custom_components.print_bridge.coordinator.AutoPrintCoordinator.async_send_print_job",
            return_value=failure,
        ),
        pytest.raises(HomeAssistantError),
    ):
        await hass.services.async_call("button", "press", {"entity_id": btn}, blocking=True)


# ---------------------------------------------------------------------------
# Unique IDs (required by HA quality scale)
# ---------------------------------------------------------------------------

async def test_all_entities_have_unique_ids(hass: HomeAssistant) -> None:
    entry = await _setup(hass, AutoPrintData(queue_depth=0, printer_online=True))
    reg = er.async_get(hass)
    entries = [e for e in reg.entities.values() if e.config_entry_id == entry.entry_id]
    assert entries, "No entities registered"
    for e in entries:
        assert e.unique_id, f"Entity '{e.entity_id}' has no unique_id"
