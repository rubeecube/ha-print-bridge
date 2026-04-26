"""Coordinator event-handling tests for the Print Bridge integration.

Covers:
  - imap_content event with a PDF part triggers _async_fetch_and_print.
  - Non-PDF parts are skipped.
  - Sender and folder filtering.
  - Schedule queue: jobs queued outside window, flushed when window opens.
  - Email post-processing: called after immediate print; skipped when queued.
  - Retry: re-fetches by stored IMAP metadata; raises for non-retryable jobs.
  - Booklet flag detection via print_handler.is_booklet_job.
  - async_send_print_job POSTs to the correct CUPS/IPP endpoint.
"""

from __future__ import annotations

import base64
import socket
import struct
from datetime import datetime, time as dt_time
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web
import pytest
from homeassistant.core import Event, HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.print_bridge.const import DOMAIN
from custom_components.print_bridge.coordinator import (
    AutoPrintCoordinator,
    AutoPrintData,
    PendingJob,
    PrintJobResult,
)

from .conftest import MOCK_CONFIG_DATA, MOCK_OPTIONS

_FAKE_PDF = b"%PDF-1.4 fake"
_LOOPBACK_HOST = socket.gethostbyname("localhost")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _setup_coordinator(
    hass: HomeAssistant,
    options: dict | None = None,
    data: dict | None = None,
) -> tuple[MockConfigEntry, AutoPrintCoordinator]:
    with patch(
        "custom_components.print_bridge.coordinator.AutoPrintCoordinator._async_update_data",
        return_value=AutoPrintData(queue_depth=0, printer_online=True),
    ):
        entry = MockConfigEntry(
            domain=DOMAIN,
            data=data if data is not None else MOCK_CONFIG_DATA,
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


def _ipp_response(status_code: int = 0x0000) -> bytes:
    return struct.pack(">HHI", 0x0200, status_code, 1) + b"\x03"


async def _start_fake_ipp_server(status_code: int = 0x0000):
    received: dict = {}

    async def _handle_print(request: web.Request) -> web.Response:
        received["method"] = request.method
        received["path"] = request.path
        received["content_type"] = request.headers.get("Content-Type")
        received["body"] = await request.read()
        return web.Response(
            body=_ipp_response(status_code),
            content_type="application/ipp",
        )

    app = web.Application()
    app.router.add_post("/printers/TestPrinter", _handle_print)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, _LOOPBACK_HOST, 0)
    await site.start()
    sockets = site._server.sockets  # type: ignore[union-attr]
    port = sockets[0].getsockname()[1]
    return f"http://{_LOOPBACK_HOST}:{port}", received, runner


def _register_fake_imap_fetch_part(hass: HomeAssistant) -> None:
    async def _fetch_part(call: ServiceCall) -> dict:
        assert call.data["entry"] == "imap_entry_1"
        assert call.data["uid"] == "99"
        assert call.data["part"] == "1"
        return {
            "part_data": base64.b64encode(_FAKE_PDF).decode(),
            "content_transfer_encoding": "base64",
        }

    hass.services.async_register(
        "imap",
        "fetch_part",
        _fetch_part,
        supports_response=SupportsResponse.ONLY,
    )


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


async def test_display_name_sender_is_processed(hass: HomeAssistant) -> None:
    """Allowed-sender matching accepts display-name formatted event senders."""
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
            _event(sender="Sender Name <sender@example.com>", parts=_pdf_parts())
        )
    mock_fetch.assert_called_once()


# ---------------------------------------------------------------------------
# Schedule queue — job held then flushed
# ---------------------------------------------------------------------------

async def test_job_queued_outside_schedule(hass: HomeAssistant) -> None:
    """When schedule is enabled and current time is outside the window,
    the job must be queued, NOT printed immediately."""
    opts = {
        **MOCK_OPTIONS,
        "schedule_enabled": True,
        "schedule_start": "08:00",
        "schedule_end": "20:00",
    }
    _, coordinator = await _setup_coordinator(hass, options=opts)

    # Mock time to be outside the window (midnight = 00:00).
    with (
        patch(
            "custom_components.print_bridge.coordinator.AutoPrintCoordinator._is_within_schedule",
            return_value=False,
        ),
        patch.object(coordinator, "_async_fetch_and_print", new=AsyncMock()) as mock_fetch,
        patch.object(coordinator, "async_request_refresh", new=AsyncMock()),
    ):
        await coordinator.async_handle_imap_event(_event(parts=_pdf_parts("queued.pdf")))

    mock_fetch.assert_not_called()
    assert len(coordinator._pending_jobs) == 1
    assert coordinator._pending_jobs[0].filename == "queued.pdf"


async def test_schedule_days_block_unlisted_weekday(hass: HomeAssistant) -> None:
    """A closed weekday holds jobs even when the hour window is open."""
    opts = {
        **MOCK_OPTIONS,
        "schedule_enabled": True,
        "schedule_start": "00:00",
        "schedule_end": "23:59",
        "schedule_days": ["tue"],
    }
    _, coordinator = await _setup_coordinator(hass, options=opts)

    with patch("homeassistant.util.dt.now", return_value=datetime(2026, 4, 27, 12, 0)):
        assert coordinator._is_within_schedule() is False


async def test_schedule_days_wrapped_window_uses_start_day(
    hass: HomeAssistant,
) -> None:
    """After-midnight hours in a wrapped window belong to the prior schedule day."""
    opts = {
        **MOCK_OPTIONS,
        "schedule_enabled": True,
        "schedule_start": "22:00",
        "schedule_end": "07:00",
        "schedule_days": ["fri"],
    }
    _, coordinator = await _setup_coordinator(hass, options=opts)

    with patch("homeassistant.util.dt.now", return_value=datetime(2026, 4, 25, 1, 0)):
        assert coordinator._is_within_schedule() is True


async def test_schedule_template_blocks_when_false(hass: HomeAssistant) -> None:
    """The optional schedule template can close an otherwise open schedule."""
    opts = {
        **MOCK_OPTIONS,
        "schedule_enabled": True,
        "schedule_start": "00:00",
        "schedule_end": "23:59",
        "schedule_template": "{{ false }}",
    }
    _, coordinator = await _setup_coordinator(hass, options=opts)

    with patch("homeassistant.util.dt.now", return_value=datetime(2026, 4, 27, 12, 0)):
        assert coordinator._is_within_schedule() is False


async def test_schedule_template_can_use_schedule_variables(hass: HomeAssistant) -> None:
    """Schedule templates receive useful variables for custom gates."""
    opts = {
        **MOCK_OPTIONS,
        "schedule_enabled": True,
        "schedule_start": "08:00",
        "schedule_end": "20:00",
        "schedule_days": ["monday"],
        "schedule_template": (
            "{{ schedule_weekday == 'mon' and printer_name == 'TestPrinter' }}"
        ),
    }
    _, coordinator = await _setup_coordinator(hass, options=opts)

    with patch("homeassistant.util.dt.now", return_value=datetime(2026, 4, 27, 12, 0)):
        assert coordinator._is_within_schedule() is True


async def test_flush_pending_prints_and_clears_queue(hass: HomeAssistant) -> None:
    """async_flush_pending must print all queued jobs and empty _pending_jobs."""
    _, coordinator = await _setup_coordinator(hass)

    # Manually add a pending job.
    coordinator._pending_jobs.append(
        PendingJob(
            entry_id="imap_entry_1",
            uid="55",
            part_key="1",
            filename="pending.pdf",
            sender="sender@example.com",
        )
    )
    success = PrintJobResult(filename="pending.pdf", success=True,
                             imap_entry_id="imap_entry_1", imap_uid="55", imap_part_key="1")

    with (
        patch.object(coordinator, "_async_fetch_and_print",
                     new=AsyncMock(return_value=success)) as mock_fetch,
        patch.object(coordinator, "_async_post_process_email", new=AsyncMock()),
        patch.object(coordinator, "async_request_refresh", new=AsyncMock()),
    ):
        count = await coordinator.async_flush_pending()

    assert count == 1
    assert len(coordinator._pending_jobs) == 0
    mock_fetch.assert_called_once()


async def test_auto_flush_when_window_opens(hass: HomeAssistant) -> None:
    """_async_update_data must flush pending jobs when transitioning closed→open."""
    opts = {**MOCK_OPTIONS, "schedule_enabled": True, "schedule_start": "07:00", "schedule_end": "22:00"}
    _, coordinator = await _setup_coordinator(hass, options=opts)

    coordinator._pending_jobs.append(
        PendingJob(entry_id="imap_entry_1", uid="77", part_key="1",
                   filename="auto.pdf", sender="sender@example.com")
    )
    coordinator._last_schedule_state = False  # simulate window was closed

    with (
        patch.object(coordinator, "_is_within_schedule", return_value=True),
        patch.object(coordinator, "async_flush_pending", new=AsyncMock(return_value=1)) as mock_flush,
        patch.object(coordinator, "_async_check_printer_online", return_value=True),
        patch.object(coordinator, "_count_queue_files", return_value=0),
    ):
        await coordinator._async_update_data()

    mock_flush.assert_called_once()


async def test_auto_flush_on_startup_with_pending(hass: HomeAssistant) -> None:
    """When _last_schedule_state is None (first run) and window is open, must also flush."""
    _, coordinator = await _setup_coordinator(hass)
    coordinator._pending_jobs.append(
        PendingJob(entry_id="imap_entry_1", uid="88", part_key="1",
                   filename="startup.pdf", sender="sender@example.com")
    )
    assert coordinator._last_schedule_state is None  # initial state

    with (
        patch.object(coordinator, "_is_within_schedule", return_value=True),
        patch.object(coordinator, "async_flush_pending", new=AsyncMock(return_value=1)) as mock_flush,
        patch.object(coordinator, "_async_check_printer_online", return_value=True),
        patch.object(coordinator, "_count_queue_files", return_value=0),
    ):
        await coordinator._async_update_data()

    mock_flush.assert_called_once()


# ---------------------------------------------------------------------------
# Email post-processing
# ---------------------------------------------------------------------------

async def test_post_process_called_after_immediate_print(hass: HomeAssistant) -> None:
    """Post-processing must run when a job is printed immediately (not queued)."""
    _, coordinator = await _setup_coordinator(hass)
    success = PrintJobResult(filename="doc.pdf", success=True)

    with (
        patch.object(coordinator, "_is_within_schedule", return_value=True),
        patch.object(coordinator, "_async_fetch_and_print",
                     new=AsyncMock(return_value=success)),
        patch.object(coordinator, "_async_post_process_email", new=AsyncMock()) as mock_pp,
        patch.object(coordinator, "async_request_refresh", new=AsyncMock()),
    ):
        await coordinator.async_handle_imap_event(_event(parts=_pdf_parts()))

    mock_pp.assert_called_once()


async def test_post_process_skipped_when_all_jobs_queued(hass: HomeAssistant) -> None:
    """Post-processing must NOT run when all PDFs were queued by the schedule."""
    _, coordinator = await _setup_coordinator(hass)

    with (
        patch.object(coordinator, "_is_within_schedule", return_value=False),
        patch.object(coordinator, "_async_fetch_and_print", new=AsyncMock()) as mock_fetch,
        patch.object(coordinator, "_async_post_process_email", new=AsyncMock()) as mock_pp,
        patch.object(coordinator, "async_request_refresh", new=AsyncMock()),
    ):
        await coordinator.async_handle_imap_event(_event(parts=_pdf_parts()))

    mock_fetch.assert_not_called()
    mock_pp.assert_not_called()  # must NOT run — email not yet printed


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------

async def test_retry_job_refetches_and_prints(hass: HomeAssistant) -> None:
    """async_retry_job must call _async_fetch_and_print with stored IMAP metadata."""
    _, coordinator = await _setup_coordinator(hass)

    original_job = PrintJobResult(
        filename="retry.pdf",
        success=False,
        error="HTTP 503",
        imap_entry_id="imap_entry_1",
        imap_uid="42",
        imap_part_key="1",
        sender="sender@example.com",
    )
    assert original_job.can_retry is True

    retry_result = PrintJobResult(filename="retry.pdf", success=True,
                                  imap_entry_id="imap_entry_1", imap_uid="42", imap_part_key="1")

    with (
        patch.object(coordinator, "_async_fetch_and_print",
                     new=AsyncMock(return_value=retry_result)) as mock_fetch,
        patch.object(coordinator, "_async_notify_job", new=AsyncMock()),
        patch.object(coordinator, "async_request_refresh", new=AsyncMock()),
    ):
        result = await coordinator.async_retry_job(original_job)

    assert result.success is True
    # original_job.duplex is None, so duplex_override passes None and the
    # coordinator's _duplex_mode default is applied inside _async_fetch_and_print.
    mock_fetch.assert_called_once_with(
        entry_id="imap_entry_1",
        uid="42",
        part_key="1",
        filename="retry.pdf",
        duplex_override=None,
        booklet_override=False,
        sender="sender@example.com",
    )


async def test_retry_raises_when_no_imap_metadata(hass: HomeAssistant) -> None:
    """async_retry_job must raise HomeAssistantError for jobs without IMAP metadata
    (e.g. jobs printed via the print_file service)."""
    _, coordinator = await _setup_coordinator(hass)

    file_job = PrintJobResult(filename="from_disk.pdf", success=False, error="CUPS down")
    assert file_job.can_retry is False

    with pytest.raises(HomeAssistantError, match="IMAP metadata"):
        await coordinator.async_retry_job(file_job)


# ---------------------------------------------------------------------------
# Booklet detection
# ---------------------------------------------------------------------------

async def test_booklet_pattern_matches_filename() -> None:
    """is_booklet_job must return True when filename contains a booklet pattern."""
    from custom_components.print_bridge.print_handler import is_booklet_job

    assert is_booklet_job("Sunday Programme.pdf", ["Programme"]) is True
    assert is_booklet_job("invoice.pdf", ["Programme"]) is False
    assert is_booklet_job("SUNDAY PROGRAMME.pdf", ["programme"]) is True  # case-insensitive


# ---------------------------------------------------------------------------
# IPP endpoint construction
# ---------------------------------------------------------------------------

async def test_send_print_job_posts_to_ipp_endpoint(hass: HomeAssistant) -> None:
    """async_send_print_job must POST to the CUPS printer endpoint."""
    _, coordinator = await _setup_coordinator(hass)

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    mock_resp.read = AsyncMock(return_value=_ipp_response())

    mock_session = MagicMock()
    mock_session.post.return_value = mock_resp

    with patch(
        "custom_components.print_bridge.coordinator.async_get_clientsession",
        return_value=mock_session,
    ):
        result = await coordinator.async_send_print_job(
            "doc.pdf", _FAKE_PDF, "one-sided", False
        )

    assert result.success is True
    post_url = mock_session.post.call_args.args[0]
    assert post_url == "http://cups.local:631/printers/TestPrinter"


async def test_send_print_job_rejects_ipp_error_body(hass: HomeAssistant) -> None:
    """HTTP 200 is not enough; the IPP response status must also be successful."""
    _, coordinator = await _setup_coordinator(hass)

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    mock_resp.read = AsyncMock(return_value=_ipp_response(0x040A))

    mock_session = MagicMock()
    mock_session.post.return_value = mock_resp

    with patch(
        "custom_components.print_bridge.coordinator.async_get_clientsession",
        return_value=mock_session,
    ):
        result = await coordinator.async_send_print_job(
            "doc.pdf", _FAKE_PDF, "one-sided", False
        )

    assert result.success is False
    assert "document-format-not-supported" in result.error


async def test_imap_event_posts_pdf_to_fake_ipp_printer(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """Exercise the event -> IMAP fetch_part -> IPP POST path over local HTTP."""
    cups_url, received, runner = await _start_fake_ipp_server()
    _register_fake_imap_fetch_part(hass)
    try:
        _, coordinator = await _setup_coordinator(
            hass,
            options={**MOCK_OPTIONS, "allowed_senders": ["sender@example.com"]},
            data={"cups_url": cups_url, "printer_name": "TestPrinter"},
        )

        parts = {
            "1": {
                "content_type": "application/pdf; name=document.pdf",
                "filename": "document.pdf",
                "content_transfer_encoding": "base64",
            }
        }
        await coordinator.async_handle_imap_event(
            _event(sender="Sender Name <sender@example.com>", parts=parts)
        )

        port = cups_url.rsplit(":", 1)[1]
        assert coordinator._job_history[0].success is True
        assert received["path"] == "/printers/TestPrinter"
        assert received["content_type"] == "application/ipp"
        assert b"printer-uri" in received["body"]
        assert (
            f"ipp://{_LOOPBACK_HOST}:{port}/printers/TestPrinter".encode()
            in received["body"]
        )
        assert received["body"].endswith(_FAKE_PDF)
    finally:
        await runner.cleanup()
