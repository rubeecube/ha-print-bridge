"""Config flow tests for the Auto Print integration.

Golden rules applied:
  - Happy path: user step → printer step → entry created.
  - Each error branch in both steps is covered and recovery is verified.
  - Duplicate unique-id is rejected (abort).
  - Options flow: existing values pre-populated, updates persisted.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.auto_print.const import DOMAIN

from .conftest import MOCK_CONFIG_DATA, MOCK_OPTIONS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STEP1_INPUT = {
    "imap_server": "imap.example.com",
    "imap_port": 993,
    "imap_use_ssl": True,
    "imap_username": "print@example.com",
    "imap_password": "secret",
    "imap_folder": "INBOX",
    "allowed_senders": "sender@example.com",   # text area — one per line
}

_STEP2_INPUT = {
    "cups_url": "http://10.0.0.1:631",
    "printer_name": "TestPrinter",
}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

async def test_full_flow_creates_entry(
    hass: HomeAssistant,
    mock_imap_ok,
    mock_cups_ok,
    mock_setup_entry,
) -> None:
    """Test that a complete, valid config flow creates a config entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _STEP1_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "printer"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _STEP2_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["imap_server"] == "imap.example.com"
    assert result["data"]["printer_name"] == "TestPrinter"
    # Allowed senders must be stored as a list, not the raw text.
    assert result["data"]["allowed_senders"] == ["sender@example.com"]
    mock_setup_entry.assert_called_once()


async def test_entry_title_includes_printer_name(
    hass: HomeAssistant,
    mock_imap_ok,
    mock_cups_ok,
    mock_setup_entry,
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _STEP1_INPUT
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _STEP2_INPUT
    )
    assert "TestPrinter" in result["title"]


# ---------------------------------------------------------------------------
# Step 1 (IMAP) error branches — user must be able to recover and complete
# ---------------------------------------------------------------------------

async def test_step_user_imap_cannot_connect(
    hass: HomeAssistant,
    mock_cups_ok,
    mock_setup_entry,
) -> None:
    """Connection error on step 1 shows an error; the form is re-shown."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    # First attempt: IMAP server unreachable.
    with patch(
        "custom_components.auto_print.config_flow._test_imap_connection",
        return_value="cannot_connect_imap",
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _STEP1_INPUT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"]["base"] == "cannot_connect_imap"

    # Recovery: fix the server → flow continues.
    with patch(
        "custom_components.auto_print.config_flow._test_imap_connection",
        return_value=None,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _STEP1_INPUT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "printer"


async def test_step_user_imap_invalid_auth(
    hass: HomeAssistant,
    mock_setup_entry,
) -> None:
    """Auth failure on step 1 shows invalid_auth error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(
        "custom_components.auto_print.config_flow._test_imap_connection",
        return_value="invalid_auth",
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _STEP1_INPUT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "invalid_auth"


async def test_step_user_empty_senders_shows_error(
    hass: HomeAssistant,
    mock_setup_entry,
) -> None:
    """An empty allowed_senders field must show a validation error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    bad_input = {**_STEP1_INPUT, "allowed_senders": "   \n   "}
    with patch(
        "custom_components.auto_print.config_flow._test_imap_connection",
        return_value=None,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], bad_input
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert "allowed_senders" in result["errors"]


# ---------------------------------------------------------------------------
# Step 2 (CUPS) error branch — recovery also tested
# ---------------------------------------------------------------------------

async def test_step_printer_cups_unreachable(
    hass: HomeAssistant,
    mock_imap_ok,
    mock_setup_entry,
) -> None:
    """CUPS unreachable on step 2 shows an error; re-entering works."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _STEP1_INPUT
    )
    assert result["step_id"] == "printer"

    # First attempt: CUPS down.
    with patch(
        "custom_components.auto_print.config_flow.async_get_clientsession",
        side_effect=Exception("refused"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _STEP2_INPUT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "printer"
    assert result["errors"]["base"] == "cannot_connect_cups"

    # Recovery: CUPS comes back up.
    from .conftest import _make_cups_session
    with patch(
        "custom_components.auto_print.config_flow.async_get_clientsession",
        return_value=_make_cups_session(200),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _STEP2_INPUT
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY


# ---------------------------------------------------------------------------
# Duplicate entry is rejected
# ---------------------------------------------------------------------------

async def test_duplicate_entry_is_aborted(
    hass: HomeAssistant,
    mock_imap_ok,
    mock_cups_ok,
    mock_setup_entry,
) -> None:
    """Setting up the same IMAP account twice must abort with already_configured."""
    # First entry.
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _STEP1_INPUT
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _STEP2_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    # Second attempt with the same username@server.
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _STEP1_INPUT
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _STEP2_INPUT
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# ---------------------------------------------------------------------------
# Options flow
# ---------------------------------------------------------------------------

async def test_options_flow_updates_options(
    hass: HomeAssistant,
    mock_coordinator_update,
) -> None:
    """Options flow must persist the new values to config_entry.options."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA, options=MOCK_OPTIONS)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    new_options_input = {
        "duplex_mode": "one-sided",
        "booklet_patterns": "Au Puits\nBulletin",
        "auto_delete": False,
        "queue_folder": "/tmp/queue2",
        "poll_interval_minutes": 5,
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], new_options_input
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["duplex_mode"] == "one-sided"
    # booklet_patterns must be stored as a list.
    assert entry.options["booklet_patterns"] == ["Au Puits", "Bulletin"]
    assert entry.options["auto_delete"] is False
    assert entry.options["poll_interval_minutes"] == 5


async def test_options_flow_rejects_poll_interval_below_1(
    hass: HomeAssistant,
    mock_coordinator_update,
) -> None:
    """Poll interval < 1 minute must show a validation error."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA, options=MOCK_OPTIONS)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    bad_input = {
        "duplex_mode": "one-sided",
        "booklet_patterns": "",
        "auto_delete": True,
        "queue_folder": "/tmp/q",
        "poll_interval_minutes": 0,   # invalid
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], bad_input
    )
    assert result["type"] is FlowResultType.FORM
    assert "poll_interval_minutes" in result["errors"]
