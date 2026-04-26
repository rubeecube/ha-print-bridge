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

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.print_bridge.const import (
    BINARY_SENSOR_PRINTER_ONLINE,
    BUTTON_PRINT_EMAIL_PREFIX,
    BUTTON_PRINT_EMAIL_SLOTS,
    CONF_ALLOWED_SENDERS,
    CONF_AUTO_PRINT_ENABLED,
    CONF_DIRECT_PRINTER_URL,
    CONF_DUPLEX_MODE,
    CONF_SELECTED_IMAP_ENTRY_ID,
    CONF_SELECTED_PRINTER_ENTRY_ID,
    CONF_SCHEDULE_START,
    BUTTON_TEST_PAGE,
    DOMAIN,
    SELECT_DUPLEX_MODE,
    SELECT_IMAP_ACCOUNT,
    SELECT_TARGET_PRINTER,
    SENSOR_LAST_JOB,
    SENSOR_QUEUE_DEPTH,
    SWITCH_AUTO_PRINT_ENABLED,
    TEXT_ALLOWED_SENDERS,
    TEXT_SCHEDULE_START,
)
from custom_components.print_bridge.coordinator import (
    AutoPrintData,
    FilterPreviewResult,
    PrintJobResult,
)
from custom_components.print_bridge.imap_checker import EmailPreview

from .conftest import MOCK_CONFIG_DATA, MOCK_OPTIONS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _setup(
    hass: HomeAssistant,
    data: AutoPrintData,
    entry_data: dict | None = None,
) -> MockConfigEntry:
    with patch(
        "custom_components.print_bridge.coordinator.AutoPrintCoordinator._async_update_data",
        return_value=data,
    ):
        entry = MockConfigEntry(
            domain=DOMAIN,
            data=entry_data if entry_data is not None else MOCK_CONFIG_DATA,
            options=MOCK_OPTIONS,
        )
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


def _email_preview(uid: str, subject: str = "Invoice") -> EmailPreview:
    return EmailPreview(
        uid=uid,
        subject=subject,
        sender="sender@example.com",
        date="Fri, 24 Apr 2026 10:00:00 +0000",
        folder="INBOX",
        has_pdf=True,
        pdf_count=1,
        matches_filter=True,
    )


def _filter_preview(*emails: EmailPreview) -> FilterPreviewResult:
    return FilterPreviewResult(
        checked_at="2026-04-24T10:00:00",
        imap_account="sender@example.com@mail.example.com",
        imap_entry_id="imap_entry_1",
        total_found=len(emails),
        matching=len(emails),
        with_pdf=len(emails),
        emails=list(emails),
    )


def _add_imap_entry(
    hass: HomeAssistant,
    username: str,
    server: str = "mail.example.com",
    folder: str = "INBOX",
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain="imap",
        title=username,
        data={"username": username, "server": server, "folder": folder},
    )
    entry.add_to_hass(hass)
    return entry


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
# Select entities
# ---------------------------------------------------------------------------

async def test_imap_account_select_persists_selected_account(
    hass: HomeAssistant,
) -> None:
    _add_imap_entry(hass, "first@example.com")
    second = _add_imap_entry(hass, "second@example.com", folder="Receipts")
    entry = await _setup(hass, AutoPrintData(queue_depth=0, printer_online=True))

    select_id = _entity_id(hass, entry, SELECT_IMAP_ACCOUNT)
    state = hass.states.get(select_id)
    assert state.state == "first@example.com (mail.example.com/INBOX)"

    await hass.services.async_call(
        "select",
        "select_option",
        {
            "entity_id": select_id,
            "option": "second@example.com (mail.example.com/Receipts)",
        },
        blocking=True,
    )

    assert entry.options[CONF_SELECTED_IMAP_ENTRY_ID] == second.entry_id
    assert hass.states.get(select_id).state == (
        "second@example.com (mail.example.com/Receipts)"
    )


async def test_target_printer_select_persists_selected_printer(
    hass: HomeAssistant,
) -> None:
    first = await _setup(
        hass,
        AutoPrintData(queue_depth=0, printer_online=True),
        entry_data={"cups_url": "http://cups-a.local:631", "printer_name": "Kitchen"},
    )
    second = await _setup(
        hass,
        AutoPrintData(queue_depth=0, printer_online=True),
        entry_data={"cups_url": "http://cups-b.local:631", "printer_name": "Office"},
    )

    select_id = _entity_id(hass, first, SELECT_TARGET_PRINTER)
    assert hass.states.get(select_id).state == "Kitchen"

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": select_id, "option": "Office"},
        blocking=True,
    )

    assert first.options[CONF_SELECTED_PRINTER_ENTRY_ID] == second.entry_id
    assert hass.states.get(select_id).state == "Office"


async def test_config_switch_persists_option(hass: HomeAssistant) -> None:
    entry = await _setup(hass, AutoPrintData(queue_depth=0, printer_online=True))
    switch_id = _entity_id(hass, entry, SWITCH_AUTO_PRINT_ENABLED)

    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": switch_id},
        blocking=True,
    )

    assert entry.options[CONF_AUTO_PRINT_ENABLED] is False
    assert hass.states.get(switch_id).state == "off"


async def test_config_select_persists_duplex_mode(hass: HomeAssistant) -> None:
    entry = await _setup(hass, AutoPrintData(queue_depth=0, printer_online=True))
    select_id = _entity_id(hass, entry, SELECT_DUPLEX_MODE)

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": select_id, "option": "One-sided"},
        blocking=True,
    )

    assert entry.options[CONF_DUPLEX_MODE] == "one-sided"
    assert hass.states.get(select_id).state == "One-sided"


async def test_config_text_persists_sender_list(hass: HomeAssistant) -> None:
    entry = await _setup(hass, AutoPrintData(queue_depth=0, printer_online=True))
    text_id = _entity_id(hass, entry, TEXT_ALLOWED_SENDERS)

    await hass.services.async_call(
        "text",
        "set_value",
        {"entity_id": text_id, "value": "A@Example.COM\nb@example.com"},
        blocking=True,
    )

    assert entry.options[CONF_ALLOWED_SENDERS] == [
        "a@example.com",
        "b@example.com",
    ]
    assert hass.states.get(text_id).state == "a@example.com\nb@example.com"


async def test_config_text_validates_schedule_time(hass: HomeAssistant) -> None:
    entry = await _setup(hass, AutoPrintData(queue_depth=0, printer_online=True))
    text_id = _entity_id(hass, entry, TEXT_SCHEDULE_START)

    await hass.services.async_call(
        "text",
        "set_value",
        {"entity_id": text_id, "value": "06:30"},
        blocking=True,
    )

    assert entry.options[CONF_SCHEDULE_START] == "06:30"
    assert hass.states.get(text_id).state == "06:30"


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


async def test_preview_email_buttons_registered(hass: HomeAssistant) -> None:
    entry = await _setup(
        hass,
        AutoPrintData(
            queue_depth=0,
            printer_online=True,
            filter_preview=_filter_preview(*[
                _email_preview(str(i), f"Mail {i}") for i in range(1, 6)
            ]),
        ),
    )

    for slot in range(1, BUTTON_PRINT_EMAIL_SLOTS + 1):
        entity_id = _entity_id(hass, entry, f"{BUTTON_PRINT_EMAIL_PREFIX}_{slot}")
        state = hass.states.get(entity_id)
        assert state.state != "unavailable"
        assert state.attributes["uid"] == str(slot)


async def test_preview_email_button_prints_selected_mail(hass: HomeAssistant) -> None:
    entry = await _setup(
        hass,
        AutoPrintData(
            queue_depth=0,
            printer_online=True,
            filter_preview=_filter_preview(
                _email_preview("101", "First"),
                _email_preview("202", "Second"),
            ),
        ),
    )
    btn = _entity_id(hass, entry, f"{BUTTON_PRINT_EMAIL_PREFIX}_2")

    with patch.object(
        entry.runtime_data,
        "async_print_email",
        new=AsyncMock(return_value={"uid": "202", "printed": 1, "results": []}),
    ) as mock_print:
        await hass.services.async_call("button", "press", {"entity_id": btn}, blocking=True)

    mock_print.assert_awaited_once_with(uid="202", imap_entry_id="imap_entry_1")


async def test_preview_email_button_uses_selected_target_printer(
    hass: HomeAssistant,
) -> None:
    entry = await _setup(
        hass,
        AutoPrintData(
            queue_depth=0,
            printer_online=True,
            filter_preview=_filter_preview(_email_preview("303", "Target")),
        ),
        entry_data={"cups_url": "http://cups-a.local:631", "printer_name": "Kitchen"},
    )
    target = await _setup(
        hass,
        AutoPrintData(queue_depth=0, printer_online=True),
        entry_data={"cups_url": "http://cups-b.local:631", "printer_name": "Office"},
    )
    entry.runtime_data.set_selected_printer_entry_id(target.entry_id)
    btn = _entity_id(hass, entry, f"{BUTTON_PRINT_EMAIL_PREFIX}_1")

    with (
        patch.object(
            entry.runtime_data,
            "async_print_email",
            new=AsyncMock(return_value={"uid": "303", "printed": 1, "results": []}),
        ) as source_print,
        patch.object(
            target.runtime_data,
            "async_print_email",
            new=AsyncMock(return_value={"uid": "303", "printed": 1, "results": []}),
        ) as target_print,
    ):
        await hass.services.async_call("button", "press", {"entity_id": btn}, blocking=True)

    source_print.assert_not_awaited()
    target_print.assert_awaited_once_with(uid="303", imap_entry_id="imap_entry_1")


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


async def test_direct_mode_entity_ids_do_not_include_printer_host(
    hass: HomeAssistant,
) -> None:
    entry = await _setup(
        hass,
        AutoPrintData(queue_depth=0, printer_online=True),
        entry_data={CONF_DIRECT_PRINTER_URL: "http://office-printer.local/ipp/print"},
    )

    entity_ids = [
        entity_id
        for entity_id in hass.states.async_entity_ids()
        if entity_id.startswith((
            "sensor.print_bridge",
            "button.print_bridge",
            "binary_sensor.print_bridge",
            "select.print_bridge",
        ))
    ]
    assert entity_ids
    assert all("office_printer_local" not in entity_id for entity_id in entity_ids)
    assert hass.states.get(_entity_id(hass, entry, SENSOR_QUEUE_DEPTH)).entity_id.startswith(
        "sensor.print_bridge_direct_printer_"
    )


async def test_direct_mode_migrates_existing_host_entity_ids(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_DIRECT_PRINTER_URL: "http://office-printer.local/ipp/print"},
        options=MOCK_OPTIONS,
    )
    entry.add_to_hass(hass)

    reg = er.async_get(hass)
    reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_{SENSOR_QUEUE_DEPTH}",
        suggested_object_id="print_bridge_office_printer_local_print_queue_depth",
        config_entry=entry,
    )
    assert reg.async_is_registered(
        "sensor.print_bridge_office_printer_local_print_queue_depth"
    )

    with patch(
        "custom_components.print_bridge.coordinator.AutoPrintCoordinator._async_update_data",
        return_value=AutoPrintData(queue_depth=0, printer_online=True),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert not reg.async_is_registered(
        "sensor.print_bridge_office_printer_local_print_queue_depth"
    )
    assert reg.async_is_registered("sensor.print_bridge_direct_printer_print_queue_depth")
