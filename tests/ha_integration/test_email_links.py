"""Linked email documents use the existing conversion and print pipeline."""

import base64
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers.template import Template
from homeassistant.util.yaml import load_yaml
from custom_components.print_bridge.coordinator import PrintJobResult
from .test_coordinator import _make_a4_pdf, _setup_coordinator


@pytest.mark.parametrize("mime", ["text/html", "text/html; charset=UTF-8", "Text/HTML; charset=utf-8"])
async def test_blueprint_allows_hidden_html_links(hass, mime):
    root = Path(__file__).resolve().parents[2]
    blueprint = load_yaml(str(root / "blueprints/automation/print_bridge/print_from_email.yaml"))
    rendered = Template(blueprint["variables"]["_linked_documents_possible"], hass).async_render({
        "print_linked_documents": True, "_mail_text": "Download", "_mail_html": "",
        "_parts": {"0,1": {"content_type": mime}},
    })
    assert rendered is True


async def test_link_only_email_prints_pdf_with_booklet_and_copies(hass):
    _, coordinator = await _setup_coordinator(hass)
    url = "https://storage.googleapis.com/sm-dashboard/events-assets/weekly.pdf"
    with (
        patch("custom_components.print_bridge.coordinator.download_linked_document", new=AsyncMock(return_value=("weekly.pdf", _make_a4_pdf(4), "application/pdf"))) as download,
        patch.object(coordinator, "async_send_print_job", new=AsyncMock(return_value=PrintJobResult(filename="weekly.pdf", success=True))) as send,
        patch.object(coordinator, "async_request_refresh", new=AsyncMock()),
        patch.object(coordinator, "_async_notify_job", new=AsyncMock()),
        patch.object(coordinator, "_async_send_status_reply", new=AsyncMock()),
    ):
        result = await coordinator.async_process_imap_message(
            "imap_entry_1", "52", parts={}, sender="sender@example.com",
            mail_subject="Weekly", mail_text="", mail_html=f'<a href="{url}">PDF</a>',
            print_linked_documents=True, allowed_link_domains="storage.googleapis.com",
            link_url_filter="/sm-dashboard/events-assets/", booklet_override=True, copies=2,
        )
    download.assert_awaited_once_with(url, "storage.googleapis.com")
    assert result.attachments == ["weekly.pdf"]
    assert result.merged_attachment_count == 1
    assert send.await_args.args[3] is True
    assert send.await_args.kwargs["copies"] == 2


async def test_html_body_is_fetched_for_hidden_url(hass):
    _, coordinator = await _setup_coordinator(hass)
    html = b'<a href="https://example.com/weekly.pdf">Download</a>'
    with (
        patch("homeassistant.core.ServiceRegistry.async_call", new=AsyncMock(return_value={"part_data": base64.b64encode(html).decode()})) as fetch,
        patch("custom_components.print_bridge.coordinator.download_linked_document", new=AsyncMock(return_value=("weekly.pdf", _make_a4_pdf(1), "application/pdf"))),
    ):
        result = await coordinator._async_convert_linked_documents(
            "imap_entry_1", "52", {"0,1": {"content_type": "text/html"}},
            "Download", "", "example.com", "", None, None, None, None,
        )
    assert fetch.await_args.args[2]["part"] == "0,1"
    assert result.filenames == ["weekly.pdf"]


async def test_link_download_is_opt_in(hass):
    _, coordinator = await _setup_coordinator(hass)
    with (
        patch("custom_components.print_bridge.coordinator.download_linked_document", new=AsyncMock()) as download,
        patch.object(coordinator, "async_request_refresh", new=AsyncMock()),
        patch.object(coordinator, "_async_send_status_reply", new=AsyncMock()),
    ):
        await coordinator.async_process_imap_message(
            "imap_entry_1", "52", parts={}, sender="sender@example.com",
            mail_subject="Weekly", mail_text="https://example.com/weekly.pdf",
        )
    download.assert_not_awaited()


async def test_failed_link_does_not_block_other_documents(hass):
    _, coordinator = await _setup_coordinator(hass)
    with patch("custom_components.print_bridge.coordinator.download_linked_document", new=AsyncMock(side_effect=[ValueError("HTTP 404"), ("weekly.pdf", _make_a4_pdf(1), "application/pdf")])):
        result = await coordinator._async_convert_linked_documents(
            "imap_entry_1", "52", {}, "https://example.com/missing.pdf https://example.com/weekly.pdf",
            "", "example.com", "", None, None, None, None,
        )
    assert result.filenames == ["weekly.pdf"]
    assert "HTTP 404" in result.skipped[0]
