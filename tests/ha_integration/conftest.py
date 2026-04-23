"""Shared fixtures for the HA-integration test suite.

These tests use pytest-homeassistant-custom-component, which provides a real
HomeAssistant async instance.  All external I/O (CUPS/HTTP) is mocked.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

# ── path setup ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent.parent

# Workspace root must be on sys.path so `custom_components.print_bridge` imports work.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── HA config dir — points to workspace root so custom_components/ is found ───

@pytest.fixture
def hass_config_dir() -> str:
    return str(ROOT)


# ── auto-enable custom integrations for every test in this package ─────────────

@pytest.fixture(autouse=True)
def _auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Ensure custom integrations are loadable in every test here."""


# ── shared entry data (CUPS-only — no IMAP fields) ────────────────────────────

MOCK_CONFIG_DATA: dict = {
    "cups_url": "http://10.0.0.1:631",
    "printer_name": "TestPrinter",
}

MOCK_OPTIONS: dict = {
    "allowed_senders": ["sender@example.com"],
    "duplex_mode": "two-sided-long-edge",
    "booklet_patterns": ["Programme"],
    "auto_delete": True,
    "queue_folder": "/tmp/auto_print_queue",
}


# ── coordinator mock ──────────────────────────────────────────────────────────

@pytest.fixture
def mock_coordinator_update():
    """Patch AutoPrintCoordinator._async_update_data to avoid real HTTP calls."""
    from custom_components.print_bridge.coordinator import AutoPrintData

    with patch(
        "custom_components.print_bridge.coordinator.AutoPrintCoordinator._async_update_data",
        return_value=AutoPrintData(queue_depth=0, printer_online=True),
    ) as patched:
        yield patched


# ── CUPS HEAD check mock ───────────────────────────────────────────────────────

def _make_cups_session(status: int = 200) -> MagicMock:
    """Mock aiohttp session where both HEAD and GET return *status*."""
    resp = MagicMock()
    resp.status = status
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    # GET /printers/ returns empty HTML (no printer names to parse)
    resp.text = AsyncMock(return_value="<html></html>")
    session = MagicMock()
    session.head.return_value = resp
    session.get.return_value = resp
    return session


@pytest.fixture
def mock_cups_ok():
    """Mock reachable CUPS; patch both _discover_cups and async_get_clientsession."""
    with (
        patch(
            "custom_components.print_bridge.config_flow._discover_cups",
            new=AsyncMock(return_value=(None, [])),
        ),
        patch(
            "custom_components.print_bridge.config_flow.async_get_clientsession",
            return_value=_make_cups_session(200),
        ),
    ):
        yield


@pytest.fixture
def mock_cups_unreachable():
    import aiohttp
    with (
        patch(
            "custom_components.print_bridge.config_flow._discover_cups",
            new=AsyncMock(return_value=(None, [])),
        ),
        patch(
            "custom_components.print_bridge.config_flow.async_get_clientsession",
            side_effect=aiohttp.ClientError("refused"),
        ),
    ):
        yield


# ── mock_setup_entry — prevents coordinator setup in config-flow tests ─────────

@pytest.fixture
def mock_setup_entry():
    with patch(
        "custom_components.print_bridge.async_setup_entry",
        return_value=True,
    ) as mock_fn:
        yield mock_fn
