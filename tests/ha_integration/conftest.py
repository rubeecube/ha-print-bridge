"""Shared fixtures for the HA-integration test suite.

These tests use pytest-homeassistant-custom-component which provides a real
Home Assistant instance.  All external I/O (IMAP, CUPS/HTTP) is mocked.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

# ── workspace root ────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent.parent

# Add workspace root to sys.path so `custom_components.auto_print` is importable
# as a regular Python package (needed for direct imports in test files).
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── HA config directory ───────────────────────────────────────────────────────

@pytest.fixture
def hass_config_dir() -> str:
    """Point HA's config dir at the workspace root.

    This makes HA discover custom_components/auto_print/ automatically.
    """
    return str(ROOT)


# ── auto-enable custom integrations for every test in this package ────────────

@pytest.fixture(autouse=True)
def _auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Ensure custom integrations are loadable in every test here."""


# ── shared entry data ─────────────────────────────────────────────────────────

MOCK_CONFIG_DATA: dict = {
    "imap_server": "imap.example.com",
    "imap_port": 993,
    "imap_use_ssl": True,
    "imap_username": "print@example.com",
    "imap_password": "secret",
    "imap_folder": "INBOX",
    "allowed_senders": ["sender@example.com"],
    "cups_url": "http://10.0.0.1:631",
    "printer_name": "TestPrinter",
}

MOCK_OPTIONS: dict = {
    "duplex_mode": "two-sided-long-edge",
    "booklet_patterns": ["Au Puits"],
    "auto_delete": True,
    "queue_folder": "/tmp/auto_print_queue",
    "poll_interval_minutes": 1,
}


# ── reusable coordinator mock ─────────────────────────────────────────────────

@pytest.fixture
def mock_coordinator_update():
    """Patch AutoPrintCoordinator._async_update_data to return empty data.

    This prevents real IMAP / CUPS connections during setup.
    """
    from custom_components.auto_print.coordinator import AutoPrintData

    with patch(
        "custom_components.auto_print.coordinator.AutoPrintCoordinator._async_update_data",
        return_value=AutoPrintData(queue_depth=2, printer_online=True),
    ) as patched:
        yield patched


# ── mock for the blocking IMAP connection check in config_flow ────────────────

@pytest.fixture
def mock_imap_ok():
    """Mock a successful IMAP connection test."""
    with patch(
        "custom_components.auto_print.config_flow._test_imap_connection",
        return_value=None,  # None = success
    ):
        yield


@pytest.fixture
def mock_imap_auth_error():
    """Mock an IMAP authentication failure."""
    with patch(
        "custom_components.auto_print.config_flow._test_imap_connection",
        return_value="invalid_auth",
    ):
        yield


@pytest.fixture
def mock_imap_connect_error():
    """Mock an IMAP server connection failure."""
    with patch(
        "custom_components.auto_print.config_flow._test_imap_connection",
        return_value="cannot_connect_imap",
    ):
        yield


# ── mock for the CUPS HEAD check in config_flow ───────────────────────────────

def _make_cups_session(status: int = 200) -> MagicMock:
    """Return a mock aiohttp session whose HEAD returns *status*."""
    resp = MagicMock()
    resp.status = status
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.head.return_value = resp
    return session


@pytest.fixture
def mock_cups_ok():
    """Mock a reachable CUPS server (HTTP 200)."""
    with patch(
        "custom_components.auto_print.config_flow.async_get_clientsession",
        return_value=_make_cups_session(200),
    ):
        yield


@pytest.fixture
def mock_cups_unreachable():
    """Mock an unreachable CUPS server (OSError)."""
    with patch(
        "custom_components.auto_print.config_flow.async_get_clientsession",
        side_effect=Exception("connection refused"),
    ):
        yield


# ── mock_setup_entry — prevents coordinator setup in config-flow tests ─────────

@pytest.fixture
def mock_setup_entry():
    """Prevent async_setup_entry from running during config-flow tests."""
    with patch(
        "custom_components.auto_print.async_setup_entry",
        return_value=True,
    ) as mock_fn:
        yield mock_fn
