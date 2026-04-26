"""Config flow tests for the Print Bridge integration.

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

from custom_components.print_bridge.const import CONF_ALLOWED_SENDERS, DOMAIN

from .conftest import MOCK_CONFIG_DATA, MOCK_OPTIONS, _make_cups_session

# Inputs that work for any form variant (no IMAP entry in HA by default).
_USER_INPUT = {
    "cups_url": "http://cups.local:631",
    "printer_name": "TestPrinter",
}

# Sentinel values copied from config_flow (not exported, kept as literals).
_SKIP_IMAP = "__skip__"
_MANUAL = "__manual__"


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _mock_no_discovery():
    """Patch discovery to simulate nothing found (no CUPS, no mDNS)."""
    from contextlib import ExitStack
    stack = ExitStack()
    stack.enter_context(patch(
        "custom_components.print_bridge.config_flow._discover_cups",
        new=AsyncMock(return_value=(None, [])),
    ))
    stack.enter_context(patch(
        "custom_components.print_bridge.config_flow._discover_printers_mdns",
        new=AsyncMock(return_value=[]),
    ))
    return stack


def _mock_discovery(url: str = "http://localhost:631", printers: list = None):
    """Patch _discover_cups to return *url* and *printers*; no mDNS.

    Note: use this (not _mock_no_discovery) in tests that submit CUPS
    fields, because CUPS fields only appear in the schema when cups_url
    is not None (i.e. when CUPS is discovered).
    """
    from contextlib import ExitStack
    stack = ExitStack()
    stack.enter_context(patch(
        "custom_components.print_bridge.config_flow._discover_cups",
        new=AsyncMock(return_value=(url, printers or ["LocalPrinter"])),
    ))
    stack.enter_context(patch(
        "custom_components.print_bridge.config_flow._discover_printers_mdns",
        new=AsyncMock(return_value=[]),
    ))
    return stack


def _mock_mdns_discovery(printers: list | None = None):
    """Patch _discover_printers_mdns to return discovered printers; no CUPS."""
    from contextlib import ExitStack
    stack = ExitStack()
    stack.enter_context(patch(
        "custom_components.print_bridge.config_flow._discover_cups",
        new=AsyncMock(return_value=(None, [])),
    ))
    stack.enter_context(patch(
        "custom_components.print_bridge.config_flow._discover_printers_mdns",
        new=AsyncMock(return_value=printers or [
            {"name": "MyPrinter", "url": "http://printer.local/ipp/print"}
        ]),
    ))
    return stack


# ---------------------------------------------------------------------------
# Happy path — no discovery (manual entry)
# ---------------------------------------------------------------------------

async def test_full_flow_cups_discovered(
    hass: HomeAssistant,
    mock_cups_ok,
    mock_setup_entry,
) -> None:
    """When CUPS is discovered, CUPS fields appear and the entry is created."""
    # Use _mock_discovery so cups_url is not None → CUPS fields shown in form.
    with _mock_discovery("http://cups.local:631", ["TestPrinter"]):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["cups_url"] == "http://cups.local:631"
    assert result["data"]["printer_name"] == "TestPrinter"
    mock_setup_entry.assert_called_once()


async def test_no_cups_form_has_no_cups_fields(
    hass: HomeAssistant,
    mock_setup_entry,
) -> None:
    """When CUPS is NOT found, CUPS fields must NOT appear in the form schema."""
    with _mock_no_discovery():
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
    assert result["type"] is FlowResultType.FORM
    # The schema keys are the field names; CUPS fields must be absent.
    schema_keys = [str(k) for k in result["data_schema"].schema.keys()]
    assert not any("cups_url" in k for k in schema_keys), (
        "CUPS URL field must not appear when CUPS is not installed"
    )
    assert not any("printer_name" in k for k in schema_keys), (
        "Printer Name field must not appear when CUPS is not installed"
    )


async def test_no_cups_description_mentions_documentation(
    hass: HomeAssistant,
    mock_setup_entry,
) -> None:
    """When CUPS is NOT found, the description must explain how to add it."""
    with _mock_no_discovery():
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
    cups_info = result.get("description_placeholders", {}).get("cups_info", "")
    assert "CUPS" in cups_info
    assert "add-on" in cups_info.lower() or "documentation" in cups_info.lower()


async def test_entry_title_contains_printer_name(
    hass: HomeAssistant,
    mock_cups_ok,
    mock_setup_entry,
) -> None:
    with _mock_discovery("http://cups.local:631", ["TestPrinter"]):
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

    # Use _mock_discovery so CUPS fields appear in schema.
    with _mock_discovery("http://cups.local:631", ["TestPrinter"]):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "cups_url": "http://cups.local:631",
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

    # Use _mock_discovery so CUPS fields appear in schema.
    with _mock_discovery("http://cups.local:631", ["TestPrinter"]):
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

    # Need CUPS "found" so CUPS fields appear in schema.
    with _mock_discovery("http://cups.local:631", ["TestPrinter"]):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )

    # First attempt: CUPS is down.
    with patch(
        "custom_components.print_bridge.config_flow.async_get_clientsession",
        side_effect=_aiohttp.ClientError("refused"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _USER_INPUT
        )
    assert result["type"] is FlowResultType.FORM
    assert "base" in result["errors"]

    # Recovery: CUPS is back.
    from .conftest import _make_cups_session
    with patch(
        "custom_components.print_bridge.config_flow.async_get_clientsession",
        return_value=_make_cups_session(200),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _USER_INPUT
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_no_printer_specified_shows_error(
    hass: HomeAssistant,
    mock_setup_entry,
) -> None:
    """Submitting with no direct URL (and no CUPS found) must show an error."""
    with _mock_no_discovery():
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )

    # No cups_url in input (field not in schema when CUPS not found); direct URL empty too.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"direct_printer_url": ""},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"].get("base") == "cups_or_direct_required"


async def test_mdns_discovery_pre_fills_direct_url(
    hass: HomeAssistant,
    mock_setup_entry,
) -> None:
    """When mDNS finds a printer, its URL should be available in the dropdown."""
    with _mock_mdns_discovery([{"name": "CanonMG", "url": "http://canonmg.local/ipp/print"}]):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
    assert result["type"] is FlowResultType.FORM
    # The description placeholder must mention the discovered printer.
    placeholders = result.get("description_placeholders", {})
    assert "CanonMG" in placeholders.get("direct_info", "")


async def test_rescan_rerenders_form(
    hass: HomeAssistant,
    mock_setup_entry,
) -> None:
    """Submitting with rescan=True must re-run discovery and re-show the form."""
    with _mock_no_discovery():
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )

    # Submit with rescan=True — should re-show the form, not create an entry.
    # No CUPS fields in input when CUPS not found (not in schema).
    with _mock_no_discovery():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"direct_printer_url": "", "rescan": True},
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


# ---------------------------------------------------------------------------
# Duplicate entry is rejected
# ---------------------------------------------------------------------------

async def test_duplicate_entry_aborted(
    hass: HomeAssistant,
    mock_cups_ok,
    mock_setup_entry,
) -> None:
    # Must use _mock_discovery so CUPS fields are present in schema.
    with _mock_discovery("http://cups.local:631", ["TestPrinter"]):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    with _mock_discovery("http://cups.local:631", ["TestPrinter"]):
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
        "schedule_enabled": True,
        "schedule_start": "09:00",
        "schedule_end": "17:30",
        "schedule_days": "mon\nwednesday\n5",
        "schedule_template": "{{ true }}",
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], new_input
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["allowed_senders"] == ["a@example.com", "b@example.com"]
    assert entry.options["booklet_patterns"] == ["Programme", "Bulletin"]
    assert entry.options["duplex_mode"] == "one-sided"
    assert entry.options["auto_delete"] is False
    assert entry.options["schedule_enabled"] is True
    assert entry.options["schedule_start"] == "09:00"
    assert entry.options["schedule_end"] == "17:30"
    assert entry.options["schedule_days"] == ["mon", "wed", "fri"]
    assert entry.options["schedule_template"] == "{{ true }}"


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


async def test_options_flow_rejects_invalid_schedule_days(
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
            "allowed_senders": "sender@example.com",
            "duplex_mode": "two-sided-long-edge",
            "booklet_patterns": "",
            "auto_delete": True,
            "queue_folder": "/tmp/q",
            "schedule_days": "monday\nfunday",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["schedule_days"] == "invalid_schedule_days"


async def test_options_flow_rejects_invalid_schedule_template(
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
            "allowed_senders": "sender@example.com",
            "duplex_mode": "two-sided-long-edge",
            "booklet_patterns": "",
            "auto_delete": True,
            "queue_folder": "/tmp/q",
            "schedule_template": "{% if %}",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["schedule_template"] == "invalid_template"


# ---------------------------------------------------------------------------
# Direct IPP mode — no CUPS required
# ---------------------------------------------------------------------------

async def test_direct_ipp_flow_creates_entry(
    hass: HomeAssistant,
    mock_setup_entry,
) -> None:
    """When a Direct IPP URL is provided, the entry is created without CUPS fields."""
    with _mock_no_discovery():
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )

    # Supply a direct IPP URL — no CUPS fields in schema when CUPS not found.
    direct_url = "http://direct-printer.local/ipp/print"
    with patch(
        "custom_components.print_bridge.config_flow.async_get_clientsession",
        return_value=_make_cups_session(200),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"direct_printer_url": direct_url},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["direct_printer_url"] == direct_url
    # CUPS fields must NOT be stored when direct mode is used.
    assert "printer_name" not in result["data"]
    assert "cups_url" not in result["data"]
    assert result["title"] == "Print Bridge — Direct Printer"
