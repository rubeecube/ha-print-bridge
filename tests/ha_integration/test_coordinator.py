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

import asyncio
import base64
import io
import socket
import struct
from datetime import datetime, time as dt_time
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web
import pytest
from homeassistant.core import Event, HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from pypdf import PdfWriter
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.print_bridge.const import CONF_DIRECT_PRINTER_URL, DOMAIN
from custom_components.print_bridge.coordinator import (
    AutoPrintCoordinator,
    AutoPrintData,
    PendingJob,
    PrintJobResult,
    _decode_mime_filename,
)
from custom_components.print_bridge.mail_params import parse_mail_print_parameters

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
    subject: str = "",
    text: str = "",
) -> Event:
    return Event(
        "imap_content",
        {
            "sender": sender,
            "entry_id": entry_id,
            "uid": uid,
            "subject": subject,
            "text": text,
            "parts": parts or {},
        },
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


def _ipp_attr(tag: int, name: str, value: bytes) -> bytes:
    name_b = name.encode()
    return (
        struct.pack(">BH", tag, len(name_b))
        + name_b
        + struct.pack(">H", len(value))
        + value
    )


def _ipp_more_attr(tag: int, value: bytes) -> bytes:
    return struct.pack(">BH", tag, 0) + struct.pack(">H", len(value)) + value


def _printer_attributes_response(
    formats: tuple[str, ...] = ("application/pdf",),
    raster_types: tuple[str, ...] = (),
    resolution_dpi: int = 300,
) -> bytes:
    body = struct.pack(">HHI", 0x0200, 0x0000, 1) + b"\x04"
    for index, value in enumerate(formats):
        if index == 0:
            body += _ipp_attr(0x49, "document-format-supported", value.encode())
        else:
            body += _ipp_more_attr(0x49, value.encode())
    for index, value in enumerate(raster_types):
        if index == 0:
            body += _ipp_attr(
                0x44, "pwg-raster-document-type-supported", value.encode()
            )
        else:
            body += _ipp_more_attr(0x44, value.encode())
    body += _ipp_attr(
        0x32,
        "pwg-raster-document-resolution-supported",
        struct.pack(">IIB", resolution_dpi, resolution_dpi, 3),
    )
    body += _ipp_attr(0x44, "pwg-raster-document-sheet-back", b"rotated")
    return body + b"\x03"


def _make_a4_pdf(page_count: int = 4) -> bytes:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _c_string(data: bytes, offset: int, length: int = 64) -> str:
    return data[offset: offset + length].split(b"\0", 1)[0].decode("ascii")


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack(">I", data[offset: offset + 4])[0]


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
        duplex_override=None,
        booklet_override=None,
        copies=None,
        orientation=None,
        media=None,
        raster_dpi=None,
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


async def test_mail_params_override_imap_event_print_settings(
    hass: HomeAssistant,
) -> None:
    _, coordinator = await _setup_coordinator(hass)
    success = PrintJobResult(filename="doc.pdf", success=True)

    with (
        patch.object(coordinator, "_async_fetch_and_print",
                     new=AsyncMock(return_value=success)) as mock_fetch,
        patch.object(coordinator, "async_request_refresh", new=AsyncMock()),
    ):
        await coordinator.async_handle_imap_event(
            _event(
                parts=_pdf_parts("Au Puits booklet.pdf"),
                subject="[pb duplex=short-edge copies=2 dpi=150]",
                text="Print-Bridge: booklet=true; paper=a4; orientation=landscape",
            )
        )

    mock_fetch.assert_called_once_with(
        entry_id="imap_entry_1",
        uid="99",
        part_key="1",
        filename="Au Puits booklet.pdf",
        sender="sender@example.com",
        duplex_override="two-sided-short-edge",
        booklet_override=True,
        copies=2,
        orientation="landscape",
        media="iso_a4_210x297mm",
        raster_dpi=150,
    )


async def test_mail_attachment_filter_skips_non_matching_pdfs(
    hass: HomeAssistant,
) -> None:
    _, coordinator = await _setup_coordinator(hass)

    parts = {
        "1": {"content_type": "application/pdf", "filename": "invoice.pdf"},
        "2": {"content_type": "application/pdf", "filename": "Au Puits.pdf"},
    }

    with (
        patch.object(coordinator, "_async_fetch_and_print",
                     new=AsyncMock(return_value=PrintJobResult(filename="Au Puits.pdf", success=True))) as mock_fetch,
        patch.object(coordinator, "async_request_refresh", new=AsyncMock()),
    ):
        await coordinator.async_handle_imap_event(
            _event(parts=parts, text="PB: attachment='Au Puits'")
        )

    mock_fetch.assert_called_once()
    assert mock_fetch.call_args.kwargs["part_key"] == "2"


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


async def test_status_reply_uses_configured_notify_service(
    hass: HomeAssistant,
) -> None:
    opts = {
        **MOCK_OPTIONS,
        "status_reply_enabled": True,
        "status_reply_notify_service": "notify.smtp",
    }
    _, coordinator = await _setup_coordinator(hass, options=opts)
    captured: dict = {}

    async def _notify(call: ServiceCall) -> None:
        captured.update(call.data)

    hass.services.async_register("notify", "smtp", _notify)

    await coordinator._async_send_status_reply(
        sender="sender@example.com",
        subject="Weekly PDF",
        results=[
            PrintJobResult(
                filename="booklet.pdf",
                success=True,
                duplex="two-sided-short-edge",
                booklet=True,
                copies=2,
                orientation="landscape",
                media="iso_a4_210x297mm",
                sides="two-sided-short-edge",
                document_format="application/pdf",
                status_code="IPP 0x0000",
                status="successful-ok",
            )
        ],
        params=parse_mail_print_parameters(
            "[pb copies=2]",
            "Print-Bridge: booklet=true; paper=a4; reply=true",
        ),
    )

    assert captured["target"] == ["sender@example.com"]
    assert captured["title"] == "Re: Weekly PDF"
    assert "Status code: IPP 0x0000" in captured["message"]
    assert "Booklet: yes" in captured["message"]
    assert "Orientation: landscape" in captured["message"]
    assert "Media: iso_a4_210x297mm" in captured["message"]


async def test_status_reply_falls_back_to_imap_account_smtp(
    hass: HomeAssistant,
) -> None:
    opts = {**MOCK_OPTIONS, "status_reply_enabled": True}
    _, coordinator = await _setup_coordinator(hass, options=opts)
    imap_entry = MockConfigEntry(
        domain="imap",
        data={
            "server": "mail.example.com",
            "username": "print@example.com",
            "password": "secret",
            "ssl": True,
        },
    )
    imap_entry.add_to_hass(hass)

    with patch(
        "custom_components.print_bridge.coordinator._send_status_email_smtp"
    ) as mock_send:
        await coordinator._async_send_status_reply(
            imap_entry_id=imap_entry.entry_id,
            sender="sender@example.com",
            subject="Weekly PDF",
            results=[PrintJobResult(filename="booklet.pdf", success=True)],
            params=parse_mail_print_parameters("", "Print-Bridge: reply=true"),
        )

    mock_send.assert_called_once()
    assert mock_send.call_args.kwargs["server"] == "mail.example.com"
    assert mock_send.call_args.kwargs["port"] == 465
    assert mock_send.call_args.kwargs["sender"] == "print@example.com"
    assert mock_send.call_args.kwargs["recipient"] == "sender@example.com"


async def test_process_imap_part_fetches_email_context_for_old_blueprints(
    hass: HomeAssistant,
) -> None:
    _, coordinator = await _setup_coordinator(hass)

    async def _fetch(call: ServiceCall) -> dict:
        return {
            "sender": "Sender Name <sender@example.com>",
            "subject": "Old blueprint subject",
            "text": "Print-Bridge: reply=true; copies=2",
        }

    hass.services.async_register(
        "imap",
        "fetch",
        _fetch,
        supports_response=SupportsResponse.ONLY,
    )

    result = PrintJobResult(filename="booklet.pdf", success=True)
    with (
        patch.object(
            coordinator,
            "_async_fetch_and_print",
            new=AsyncMock(return_value=result),
        ) as mock_fetch,
        patch.object(
            coordinator,
            "_async_send_status_reply",
            new=AsyncMock(),
        ) as mock_reply,
        patch.object(coordinator, "async_request_refresh", new=AsyncMock()),
    ):
        await coordinator.async_process_imap_part(
            entry_id="imap_entry_1",
            uid="99",
            part_key="1",
            filename="booklet.pdf",
            booklet_override=True,
        )

    assert mock_fetch.call_args.kwargs["sender"] == "sender@example.com"
    assert mock_fetch.call_args.kwargs["copies"] == 2
    mock_reply.assert_awaited_once()
    assert mock_reply.call_args.kwargs["sender"] == "sender@example.com"
    assert mock_reply.call_args.kwargs["subject"] == "Old blueprint subject"


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
        copies=None,
        orientation=None,
        media=None,
        raster_dpi=None,
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


async def test_send_print_job_reports_timeout_context_and_sanitizes_filename(
    hass: HomeAssistant,
) -> None:
    """TimeoutError string is blank, so surface type, endpoint, and clean name."""
    _, coordinator = await _setup_coordinator(hass)

    mock_session = MagicMock()
    mock_session.post.side_effect = asyncio.TimeoutError()

    with patch(
        "custom_components.print_bridge.coordinator.async_get_clientsession",
        return_value=mock_session,
    ):
        result = await coordinator.async_send_print_job(
            "\u200f" * 20 + "שיחת שבוע בצרפתית 294 - אמור.pdf",
            _FAKE_PDF,
            "one-sided",
            False,
        )

    assert result.success is False
    assert result.filename == "שיחת שבוע בצרפתית 294 - אמור.pdf"
    assert "TimeoutError" in result.error
    assert "http://cups.local:631/printers/TestPrinter" in result.error
    assert "timeout=300s" in result.error


async def test_direct_printer_timeout_after_post_is_treated_as_submitted(
    hass: HomeAssistant,
) -> None:
    """Some direct IPP printers print the job but never return a final response."""
    _, coordinator = await _setup_coordinator(
        hass,
        data={CONF_DIRECT_PRINTER_URL: "http://printer.local:631/ipp/print"},
    )

    mock_session = MagicMock()
    mock_session.post.side_effect = asyncio.TimeoutError()

    with (
        patch(
            "custom_components.print_bridge.coordinator.async_get_clientsession",
            return_value=mock_session,
        ),
        patch(
            "custom_components.print_bridge.coordinator.create_booklet",
            return_value=_FAKE_PDF,
        ) as mock_booklet,
        patch.object(
            coordinator,
            "_async_prepare_document_for_printing",
            new=AsyncMock(return_value=("application/pdf", _FAKE_PDF)),
        ),
    ):
        result = await coordinator.async_send_print_job(
            "Au Puits de La Paracha - Emor-Lag Baomer 5786 A4.pdf",
            _FAKE_PDF,
            "two-sided-short-edge",
            True,
        )

    assert result.success is True
    assert result.booklet is True
    assert result.duplex == "two-sided-short-edge"
    assert result.error.startswith("submitted; TimeoutError")
    assert "http://printer.local:631/ipp/print" in result.error
    mock_booklet.assert_called_once_with(_FAKE_PDF)
    packet = mock_session.post.call_args.kwargs["data"]
    assert _ipp_attr(0x23, "orientation-requested", struct.pack(">i", 4)) in packet
    assert b"print-scaling" in packet
    assert b"fit" in packet


def test_decode_mime_filename_removes_windows_1255_direction_marks() -> None:
    encoded = (
        "=?windows-1255?B?/v7+/v7+?= "
        "=?windows-1255?B?+enn+iD54eXyIOH2+PT66fogMjk0IC0g4O7l+C5wZGY=?="
    )

    assert _decode_mime_filename(encoded) == "שיחת שבוע בצרפתית 294 - אמור.pdf"


async def test_check_printer_capabilities_reads_supported_formats(
    hass: HomeAssistant,
) -> None:
    _, coordinator = await _setup_coordinator(
        hass,
        data={CONF_DIRECT_PRINTER_URL: "http://printer.local:631/ipp/print"},
    )

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    mock_resp.read = AsyncMock(
        return_value=_printer_attributes_response(
            formats=("application/octet-stream", "image/pwg-raster"),
            raster_types=("srgb_8",),
        )
    )
    mock_session = MagicMock()
    mock_session.post.return_value = mock_resp

    with patch(
        "custom_components.print_bridge.coordinator.async_get_clientsession",
        return_value=mock_session,
    ):
        capabilities = await coordinator.async_check_printer_capabilities()

    assert capabilities.document_formats == [
        "application/octet-stream",
        "image/pwg-raster",
    ]
    assert capabilities.selected_document_format == "image/pwg-raster"
    assert capabilities.conversion_required is True
    assert coordinator.data.printer_capabilities is capabilities


async def test_check_cups_capabilities_prefers_pdf(
    hass: HomeAssistant,
) -> None:
    _, coordinator = await _setup_coordinator(hass)

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    mock_resp.read = AsyncMock(
        return_value=_printer_attributes_response(
            formats=("application/octet-stream", "application/pdf", "image/pwg-raster"),
            raster_types=("srgb_8",),
        )
    )
    mock_session = MagicMock()
    mock_session.post.return_value = mock_resp

    with patch(
        "custom_components.print_bridge.coordinator.async_get_clientsession",
        return_value=mock_session,
    ):
        capabilities = await coordinator.async_check_printer_capabilities()

    assert capabilities.endpoint == "http://cups.local:631/printers/TestPrinter"
    assert capabilities.printer_uri == "ipp://cups.local:631/printers/TestPrinter"
    assert capabilities.pdf_supported is True
    assert capabilities.selected_document_format == "application/pdf"
    assert capabilities.conversion_required is False


async def test_direct_printer_converts_pdf_to_pwg_when_pdf_not_supported(
    hass: HomeAssistant,
) -> None:
    _, coordinator = await _setup_coordinator(
        hass,
        data={CONF_DIRECT_PRINTER_URL: "http://printer.local:631/ipp/print"},
    )

    caps_resp = MagicMock()
    caps_resp.status = 200
    caps_resp.__aenter__ = AsyncMock(return_value=caps_resp)
    caps_resp.__aexit__ = AsyncMock(return_value=False)
    caps_resp.read = AsyncMock(
        return_value=_printer_attributes_response(
            formats=("application/octet-stream", "image/pwg-raster"),
            raster_types=("srgb_8",),
        )
    )
    print_resp = MagicMock()
    print_resp.status = 200
    print_resp.__aenter__ = AsyncMock(return_value=print_resp)
    print_resp.__aexit__ = AsyncMock(return_value=False)
    print_resp.read = AsyncMock(return_value=_ipp_response())

    mock_session = MagicMock()
    mock_session.post.side_effect = [caps_resp, print_resp]

    with (
        patch(
            "custom_components.print_bridge.coordinator.async_get_clientsession",
            return_value=mock_session,
        ),
        patch(
            "custom_components.print_bridge.coordinator.convert_pdf_to_pwg_raster",
            return_value=b"PWG",
        ) as mock_convert,
    ):
        result = await coordinator.async_send_print_job(
            "doc.pdf", _FAKE_PDF, "one-sided", False
        )

    assert result.success is True
    assert result.raster_dpi == 150
    mock_convert.assert_called_once()
    assert mock_convert.call_args.kwargs["dpi"] == 150
    print_body = mock_session.post.call_args_list[1].kwargs["data"]
    assert b"document-format" in print_body
    assert b"image/pwg-raster" in print_body
    assert print_body.endswith(b"PWG")


async def test_booklet_pwg_job_is_pre_rotated_without_ipp_orientation(
    hass: HomeAssistant,
) -> None:
    _, coordinator = await _setup_coordinator(
        hass,
        data={CONF_DIRECT_PRINTER_URL: "http://printer.local:631/ipp/print"},
    )

    caps_resp = MagicMock()
    caps_resp.status = 200
    caps_resp.__aenter__ = AsyncMock(return_value=caps_resp)
    caps_resp.__aexit__ = AsyncMock(return_value=False)
    caps_resp.read = AsyncMock(
        return_value=_printer_attributes_response(
            formats=("application/octet-stream", "image/pwg-raster"),
            raster_types=("srgb_8",),
        )
    )
    print_resp = MagicMock()
    print_resp.status = 200
    print_resp.__aenter__ = AsyncMock(return_value=print_resp)
    print_resp.__aexit__ = AsyncMock(return_value=False)
    print_resp.read = AsyncMock(return_value=_ipp_response())

    mock_session = MagicMock()
    mock_session.post.side_effect = [caps_resp, print_resp]

    with (
        patch(
            "custom_components.print_bridge.coordinator.async_get_clientsession",
            return_value=mock_session,
        ),
        patch(
            "custom_components.print_bridge.coordinator.create_booklet",
            return_value=_FAKE_PDF,
        ),
        patch(
            "custom_components.print_bridge.coordinator.convert_pdf_to_pwg_raster",
            return_value=b"PWG",
        ),
    ):
        result = await coordinator.async_send_print_job(
            "booklet.pdf", _FAKE_PDF, "two-sided-short-edge", True
        )

    assert result.success is True
    assert result.orientation == "landscape"
    print_body = mock_session.post.call_args_list[1].kwargs["data"]
    assert (
        _ipp_attr(0x23, "orientation-requested", struct.pack(">i", 4))
        not in print_body
    )
    assert b"print-scaling" in print_body
    assert b"fit" in print_body


async def test_pwg_only_printer_receives_rotated_booklet_raster(
    hass: HomeAssistant,
) -> None:
    _, coordinator = await _setup_coordinator(
        hass,
        data={CONF_DIRECT_PRINTER_URL: "http://printer.local:631/ipp/print"},
    )

    caps_resp = MagicMock()
    caps_resp.status = 200
    caps_resp.__aenter__ = AsyncMock(return_value=caps_resp)
    caps_resp.__aexit__ = AsyncMock(return_value=False)
    caps_resp.read = AsyncMock(
        return_value=_printer_attributes_response(
            formats=("application/octet-stream", "image/pwg-raster"),
            raster_types=("srgb_8",),
            resolution_dpi=72,
        )
    )
    print_resp = MagicMock()
    print_resp.status = 200
    print_resp.__aenter__ = AsyncMock(return_value=print_resp)
    print_resp.__aexit__ = AsyncMock(return_value=False)
    print_resp.read = AsyncMock(return_value=_ipp_response())

    mock_session = MagicMock()
    mock_session.post.side_effect = [caps_resp, print_resp]

    with patch(
        "custom_components.print_bridge.coordinator.async_get_clientsession",
        return_value=mock_session,
    ):
        result = await coordinator.async_send_print_job(
            "booklet.pdf", _make_a4_pdf(), "two-sided-long-edge", True,
            raster_dpi=72,
        )

    assert result.success is True
    assert result.document_format == "image/pwg-raster"
    assert result.raster_dpi == 72
    assert result.orientation == "landscape"
    assert result.sides == "two-sided-short-edge"
    assert result.media == "iso_a4_210x297mm"

    print_body = mock_session.post.call_args_list[1].kwargs["data"]
    assert b"image/pwg-raster" in print_body
    assert b"print-scaling" in print_body
    assert b"fit" in print_body
    assert b"orientation-requested" not in print_body

    raster = print_body[print_body.index(b"RaS2"):]
    header_offset = 4
    assert _c_string(raster, header_offset) == "PwgRaster"
    assert _c_string(raster, header_offset + 1732) == "iso_a4_210x297mm"
    assert _u32(raster, header_offset + 272) == 1
    assert _u32(raster, header_offset + 368) == 1
    assert _u32(raster, header_offset + 352) == 595
    assert _u32(raster, header_offset + 356) == 842
    assert _u32(raster, header_offset + 372) == 595
    assert _u32(raster, header_offset + 376) == 842


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
