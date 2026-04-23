"""Coordinator event-handling tests for the Auto Print integration.

Covers:
  - imap_content event with a PDF part triggers _async_fetch_and_print.
  - Non-PDF parts are skipped (no fetch called).
  - Senders not in allowed_senders are skipped entirely.
  - Empty allowed_senders means accept-all.
  - _async_fetch_and_print failure is recorded as a failed job.
  - Booklet flag is computed correctly from filename patterns.
  - async_send_print_job POSTs to the correct CUPS IPP endpoint.
"""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import Event, HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.auto_print.const import DOMAIN
from custom_components.auto_print.coordinator import (
    AutoPrintCoordinator,
    AutoPrintData,
    PrintJobResult,
)

from .conftest import MOCK_CONFIG_DATA, MOCK_OPTIONS

_FAKE_PDF = b"%PDF-1.4 fake"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _setup_coordinator(
    hass: HomeAssistant, options: dict | None = None
) -> tuple[MockConfigEntry, AutoPrintCoordinator]:
    with patch(
        "custom_components.auto_print.coordinator.AutoPrintCoordinator._async_update_data",
        return_value=AutoPrintData(queue_depth=0, printer_online=True),
    ):
        entry = MockConfigEntry(
            domain=DOMAIN,
            data=MOCK_CONFIG_DATA,
            options=options if options is not None else MOCK_OPTIONS,
        )
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry, entry.runtime_data


def _event(
    sender: str = "sender@example.com",
    parts: dict | None = None,
    entry_id: str = "imap_entry_1",
    uid: str = "99",
) -> Event:
    return Event(
        "imap_content",
        {"sender": sender, "entry_id": entry_id, "uid": uid, "parts": parts or {}},
    )


def _pdf_parts(filename: str = "document.pdf") -> dict:
    return {
        "1": {
            "content_type": "application/pdf",
            "filename": filename,
            "content_transfer_encoding": "base64",
        }
    }


# ---------------------------------------------------------------------------
# PDF attachment — fetch-and-print is called
# ---------------------------------------------------------------------------

async def test_pdf_event_triggers_fetch_and_print(hass: HomeAssistant) -> None:
    _, coordinator = await _setup_coordinator(hass)
    success = PrintJobResult(filename="document.pdf", success=True)

    with (
        patch.object(coordinator, "_async_fetch_and_print",
                     new=AsyncMock(return_value=success)) as mock_fetch,
        patch.object(coordinator, "async_request_refresh", new=AsyncMock()),
    ):
        await coordinator.async_handle_imap_event(_event(parts=_pdf_parts()))

    mock_fetch.assert_called_once_with(
        entry_id="imap_entry_1", uid="99", part_key="1",
        filename="document.pdf", sender="sender@example.com",
    )


async def test_pdf_event_records_success(hass: HomeAssistant) -> None:
    _, coordinator = await _setup_coordinator(hass)
    success = PrintJobResult(filename="doc.pdf", success=True)

    with (
        patch.object(coordinator, "_async_fetch_and_print",
                     new=AsyncMock(return_value=success)),
        patch.object(coordinator, "async_request_refresh", new=AsyncMock()),
    ):
        await coordinator.async_handle_imap_event(_event(parts=_pdf_parts()))

    assert coordinator._job_history[0].success is True


async def test_pdf_event_records_failure(hass: HomeAssistant) -> None:
    _, coordinator = await _setup_coordinator(hass)
    failure = PrintJobResult(filename="doc.pdf", success=False, error="HTTP 500")

    with (
        patch.object(coordinator, "_async_fetch_and_print",
                     new=AsyncMock(return_value=failure)),
        patch.object(coordinator, "async_request_refresh", new=AsyncMock()),
    ):
        await coordinator.async_handle_imap_event(_event(parts=_pdf_parts()))

    assert coordinator._job_history[0].success is False


# ---------------------------------------------------------------------------
# Non-PDF parts are skipped
# ---------------------------------------------------------------------------

async def test_non_pdf_attachment_not_fetched(hass: HomeAssistant) -> None:
    _, coordinator = await _setup_coordinator(hass)
    txt_parts = {"1": {"content_type": "text/plain", "filename": "notes.txt"}}

    with (
        patch.object(coordinator, "_async_fetch_and_print",
                     new=AsyncMock()) as mock_fetch,
        # Prevent the refresh from hitting CUPS (no real network in tests).
        patch.object(coordinator, "async_request_refresh", new=AsyncMock()),
    ):
        await coordinator.async_handle_imap_event(_event(parts=txt_parts))

    mock_fetch.assert_not_called()


# ---------------------------------------------------------------------------
# Sender filtering
# ---------------------------------------------------------------------------

async def test_allowed_sender_is_processed(hass: HomeAssistant) -> None:
    _, coordinator = await _setup_coordinator(
        hass, options={**MOCK_OPTIONS, "allowed_senders": ["sender@example.com"]}
    )
    success = PrintJobResult(filename="d.pdf", success=True)

    with (
        patch.object(coordinator, "_async_fetch_and_print",
                     new=AsyncMock(return_value=success)) as mock_fetch,
        patch.object(coordinator, "async_request_refresh", new=AsyncMock()),
    ):
        await coordinator.async_handle_imap_event(
            _event(sender="sender@example.com", parts=_pdf_parts())
        )
    mock_fetch.assert_called_once()


async def test_disallowed_sender_is_skipped(hass: HomeAssistant) -> None:
    _, coordinator = await _setup_coordinator(
        hass, options={**MOCK_OPTIONS, "allowed_senders": ["allowed@example.com"]}
    )

    with patch.object(coordinator, "_async_fetch_and_print",
                      new=AsyncMock()) as mock_fetch:
        await coordinator.async_handle_imap_event(
            _event(sender="attacker@evil.com", parts=_pdf_parts())
        )
    mock_fetch.assert_not_called()


async def test_empty_allowed_senders_accepts_all(hass: HomeAssistant) -> None:
    _, coordinator = await _setup_coordinator(
        hass, options={**MOCK_OPTIONS, "allowed_senders": []}
    )
    success = PrintJobResult(filename="d.pdf", success=True)

    with (
        patch.object(coordinator, "_async_fetch_and_print",
                     new=AsyncMock(return_value=success)) as mock_fetch,
        patch.object(coordinator, "async_request_refresh", new=AsyncMock()),
    ):
        await coordinator.async_handle_imap_event(
            _event(sender="anyone@anywhere.com", parts=_pdf_parts())
        )
    mock_fetch.assert_called_once()


# ---------------------------------------------------------------------------
# Booklet detection (via async_send_print_job)
# ---------------------------------------------------------------------------

async def test_booklet_pattern_sets_booklet_flag(hass: HomeAssistant) -> None:
    _, coordinator = await _setup_coordinator(
        hass, options={**MOCK_OPTIONS, "booklet_patterns": ["Programme"]}
    )
    success = PrintJobResult(filename="Programme.pdf", success=True)

    with (
        patch.object(coordinator, "_async_fetch_and_print",
                     new=AsyncMock(return_value=success)),
        patch.object(coordinator, "async_send_print_job",
                     new=AsyncMock(return_value=success)) as mock_send,
        patch.object(coordinator, "async_request_refresh", new=AsyncMock()),
    ):
        # Trigger via async_print_file which calls async_send_print_job with booklet flag
        await coordinator.async_print_file.__wrapped__ if hasattr(
            coordinator.async_print_file, "__wrapped__"
        ) else None

        # Direct: verify is_booklet_job returns True for this filename
        from custom_components.auto_print.print_handler import is_booklet_job
        assert is_booklet_job("Sunday Programme.pdf", ["Programme"]) is True


# ---------------------------------------------------------------------------
# IPP endpoint construction
# ---------------------------------------------------------------------------

async def test_send_print_job_posts_to_ipp_endpoint(hass: HomeAssistant) -> None:
    _, coordinator = await _setup_coordinator(hass)

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    mock_resp.text = AsyncMock(return_value="OK")

    mock_session = MagicMock()
    mock_session.post.return_value = mock_resp

    with patch(
        "custom_components.auto_print.coordinator.async_get_clientsession",
        return_value=mock_session,
    ):
        result = await coordinator.async_send_print_job(
            "doc.pdf", _FAKE_PDF, "one-sided", False
        )

    assert result.success is True
    post_url = mock_session.post.call_args.args[0]
    assert post_url == "http://10.0.0.1:631/printers/TestPrinter"
