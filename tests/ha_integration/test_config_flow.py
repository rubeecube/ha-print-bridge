"""Config flow tests for the Auto Print integration.

Golden rules applied:
  - Happy path with no discovery: manual form → entry created.
  - Happy path with discovered printers: select shows pre-filled printer.
  - IMAP pre-fill: selecting an IMAP account populates allowed_senders.
  - 'Enter manually' option routes to the manual_printer sub-step.
  - CUPS unreachable → error shown → user can recover and complete.
  - Duplicate unique-id → aborted.
  - Options flow: persists all fields; IMAP shortcut appends sender.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.auto_print.const import CONF_ALLOWED_SENDERS, DOMAIN

from .conftest import MOCK_CONFIG_DATA, MOCK_OPTIONS, _make_cups_session

# Inputs that work for any form variant (no IMAP entry in HA by default).
_USER_INPUT = {
    "cups_url": "http://10.0.0.1:631",
    "printer_name": "TestPrinter",
}

# Sentinel values copied from config_flow (not exported, kept as literals).
_SKIP_IMAP = "__skip__"
_MANUAL = "__manual__"


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _mock_no_discovery():
    """Patch _discover_cups to simulate no CUPS found."""
    return patch(
        "custom_components.auto_print.config_flow._discover_cups",
        new=AsyncMock(return_value=(None, [])),
    )


def _mock_discovery(url: str = "http://localhost:631", printers: list = None):
    """Patch _discover_cups to return *url* and *printers*."""
    return patch(
        "custom_components.auto_print.config_flow._discover_cups",
        new=AsyncMock(return_value=(url, printers or ["LocalPrinter"])),
    )


# ---------------------------------------------------------------------------
# Happy path — no discovery (manual entry)
# ---------------------------------------------------------------------------

async def test_full_flow_no_discovery(
    hass: HomeAssistant,
    mock_cups_ok,
    mock_setup_entry,
) -> None:
    """Flow with no CUPS discovered: manual text fields → entry created."""
    with _mock_no_discovery():
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with mock_cups_ok if not isinstance(mock_cups_ok, type(None)) else _mock_no_discovery():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _USER_INPUT
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["cups_url"] == "http://10.0.0.1:631"
    assert result["data"]["printer_name"] == "TestPrinter"
    mock_setup_entry.assert_called_once()


async def test_entry_title_contains_printer_name(
    hass: HomeAssistant,
    mock_cups_ok,
    mock_setup_entry,
) -> None:
    with _mock_no_discovery():
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _USER_INPUT
    )
    assert "TestPrinter" in result["title"]


# ---------------------------------------------------------------------------
# Happy path — with CUPS discovery
# ---------------------------------------------------------------------------

async def test_discovery_pre_fills_printer_select(
    hass: HomeAssistant,
    mock_cups_ok,
    mock_setup_entry,
) -> None:
    """When CUPS is discovered, user sees a printer select with real names."""
    with _mock_discovery("http://localhost:631", ["DiscoveredPrinter"]):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
    assert result["type"] is FlowResultType.FORM

    # User picks the discovered printer.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"cups_url": "http://localhost:631", "printer_name": "DiscoveredPrinter"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["printer_name"] == "DiscoveredPrinter"


async def test_manual_sentinel_routes_to_sub_step(
    hass: HomeAssistant,
    mock_cups_ok,
    mock_setup_entry,
) -> None:
    """Selecting 'Enter name manually' routes to the manual_printer step."""
    with _mock_discovery("http://localhost:631", ["AutoPrinter"]):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )

    # User picks the _SENTINEL_MANUAL option.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"cups_url": "http://localhost:631", "printer_name": _MANUAL},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "manual_printer"

    # User types a custom printer name.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"printer_name": "MyCustomPrinter"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["printer_name"] == "MyCustomPrinter"


# ---------------------------------------------------------------------------
# IMAP pre-fill
# ---------------------------------------------------------------------------

async def test_imap_account_pre_fills_allowed_senders(
    hass: HomeAssistant,
    mock_cups_ok,
    mock_setup_entry,
) -> None:
    """Selecting an IMAP account pre-populates allowed_senders in options."""
    # Add a fake IMAP config entry.
    imap_entry = MockConfigEntry(
        domain="imap",
        title="print@example.com",
        data={"username": "print@example.com", "server": "imap.example.com"},
    )
    imap_entry.add_to_hass(hass)

    with _mock_no_discovery():
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "cups_url": "http://10.0.0.1:631",
            "printer_name": "TestPrinter",
            "imap_account": imap_entry.entry_id,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # Options must contain the IMAP account's username.
    assert "print@example.com" in result["options"].get(CONF_ALLOWED_SENDERS, [])


async def test_skip_imap_creates_empty_allowed_senders(
    hass: HomeAssistant,
    mock_cups_ok,
    mock_setup_entry,
) -> None:
    """Skipping the IMAP selection leaves allowed_senders empty (accept all)."""
    imap_entry = MockConfigEntry(
        domain="imap",
        data={"username": "user@example.com", "server": "imap.example.com"},
    )
    imap_entry.add_to_hass(hass)

    with _mock_no_discovery():
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**_USER_INPUT, "imap_account": _SKIP_IMAP},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"].get(CONF_ALLOWED_SENDERS, []) == []


# ---------------------------------------------------------------------------
# CUPS error branch — error shown, user can recover
# ---------------------------------------------------------------------------

async def test_cups_unreachable_shows_error_then_recovers(
    hass: HomeAssistant,
    mock_setup_entry,
) -> None:
    import aiohttp as _aiohttp

    with _mock_no_discovery():
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )

    # First attempt: CUPS is down.
    with patch(
        "custom_components.auto_print.config_flow.async_get_clientsession",
        side_effect=_aiohttp.ClientError("refused"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _USER_INPUT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "cannot_connect_cups"

    # Recovery: CUPS is back.
    from .conftest import _make_cups_session
    with patch(
        "custom_components.auto_print.config_flow.async_get_clientsession",
        return_value=_make_cups_session(200),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _USER_INPUT
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY


# ---------------------------------------------------------------------------
# Duplicate entry is rejected
# ---------------------------------------------------------------------------

async def test_duplicate_entry_aborted(
    hass: HomeAssistant,
    mock_cups_ok,
    mock_setup_entry,
) -> None:
    with _mock_no_discovery():
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    with _mock_no_discovery():
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _USER_INPUT
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# ---------------------------------------------------------------------------
# Options flow
# ---------------------------------------------------------------------------

async def test_options_flow_persists_all_fields(
    hass: HomeAssistant,
    mock_coordinator_update,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CONFIG_DATA, options=MOCK_OPTIONS
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    new_input = {
        "allowed_senders": "a@example.com\nb@example.com",
        "duplex_mode": "one-sided",
        "booklet_patterns": "Programme\nBulletin",
        "auto_delete": False,
        "queue_folder": "/tmp/q2",
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], new_input
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["allowed_senders"] == ["a@example.com", "b@example.com"]
    assert entry.options["booklet_patterns"] == ["Programme", "Bulletin"]
    assert entry.options["duplex_mode"] == "one-sided"
    assert entry.options["auto_delete"] is False


async def test_options_flow_imap_shortcut_appends_sender(
    hass: HomeAssistant,
    mock_coordinator_update,
) -> None:
    """Selecting an IMAP account in options appends its address to allowed_senders."""
    imap_entry = MockConfigEntry(
        domain="imap",
        data={"username": "extra@example.com", "server": "imap.example.com"},
    )
    imap_entry.add_to_hass(hass)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG_DATA,
        options={**MOCK_OPTIONS, "allowed_senders": ["existing@example.com"]},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    new_input = {
        "allowed_senders": "existing@example.com",
        "imap_account": imap_entry.entry_id,
        "duplex_mode": "two-sided-long-edge",
        "booklet_patterns": "",
        "auto_delete": True,
        "queue_folder": "/tmp/q",
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], new_input
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    senders = entry.options["allowed_senders"]
    assert "existing@example.com" in senders
    assert "extra@example.com" in senders


async def test_options_flow_empty_senders_means_accept_all(
    hass: HomeAssistant,
    mock_coordinator_update,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CONFIG_DATA, options=MOCK_OPTIONS
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "allowed_senders": "   ",
            "duplex_mode": "two-sided-long-edge",
            "booklet_patterns": "",
            "auto_delete": True,
            "queue_folder": "/tmp/q",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["allowed_senders"] == []
