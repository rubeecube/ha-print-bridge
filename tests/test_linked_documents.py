"""Email link extraction and bounded public-only document downloads."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from linked_documents import (
    MAX_DOCUMENT_BYTES,
    PublicDocumentResolver,
    download_linked_document,
    extract_document_links,
    validate_document_url,
)


def test_extract_hidden_links_and_deduplicate():
    url = "https://storage.googleapis.com/sm-dashboard/events-assets/example.pdf"
    assert extract_document_links(url, f'<a href="{url}">Download PDF</a>') == [url]


@pytest.mark.parametrize("text,expected", [
    ("Download (https://example.com/report.pdf).", "https://example.com/report.pdf"),
    ("Read https://example.com/report.pdf,", "https://example.com/report.pdf"),
    ("https://example.com/report(1).pdf", "https://example.com/report(1).pdf"),
])
def test_plain_text_url_punctuation(text, expected):
    assert extract_document_links(text) == [expected]


@pytest.mark.parametrize("url,domains", [
    ("http://127.0.0.1/doc.pdf", "127.0.0.1"),
    ("http://10.0.0.37/doc.pdf", "10.0.0.37"),
    ("http://[::1]/doc.pdf", "::1"),
    ("file:///config/secrets.yaml", "example.com"),
    ("https://user:secret@example.com/doc.pdf", "example.com"),
    ("https://example.com.evil.test/doc.pdf", "example.com"),
    ("https://example.com:8123/doc.pdf", "example.com"),
    ("https://example.com/doc.pdf", ""),
])
def test_reject_unsafe_or_unapproved_urls(url, domains):
    with pytest.raises(ValueError):
        validate_document_url(url, domains)


async def test_resolver_rejects_private_dns():
    with patch("aiohttp.resolver.DefaultResolver.resolve", new=AsyncMock(return_value=[{"host": "192.168.1.1"}])):
        resolver = PublicDocumentResolver()
        with pytest.raises(ValueError, match="non-public"):
            await resolver.resolve("example.com", 443)
        await resolver.close()


def _response(status=200, headers=None, chunks=(b"%PDF-example",)):
    response = MagicMock()
    response.status = status
    response.headers = headers or {"Content-Type": "application/pdf"}
    response.content_length = None
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)

    async def stream():
        for chunk in chunks:
            yield chunk

    response.content.iter_chunked.side_effect = lambda size: stream()
    return response


def _session(*responses):
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.get.side_effect = responses
    return session


async def test_download_pdf_and_rfc_filename():
    response = _response(headers={
        "Content-Type": "application/pdf",
        "Content-Disposition": "attachment; filename*=UTF-8''weekly%20document.pdf",
    })
    session = _session(response)
    with patch("linked_documents.aiohttp.ClientSession", return_value=session), patch("linked_documents.aiohttp.TCPConnector"):
        filename, data, mime = await download_linked_document("https://example.com/download", "example.com")
    assert (filename, data, mime) == ("weekly document.pdf", b"%PDF-example", "application/pdf")
    assert session.get.call_args.kwargs["allow_redirects"] is False


async def test_redirect_cannot_escape_allowed_public_hosts():
    session = _session(_response(status=302, headers={"Location": "http://127.0.0.1/private.pdf"}))
    with patch("linked_documents.aiohttp.ClientSession", return_value=session), patch("linked_documents.aiohttp.TCPConnector"):
        with pytest.raises(ValueError):
            await download_linked_document("https://example.com/doc.pdf", "example.com 127.0.0.1")
    assert session.get.call_count == 1


async def test_stream_limit_applies_without_content_length():
    session = _session(_response(chunks=(b"x" * (MAX_DOCUMENT_BYTES + 1),)))
    with patch("linked_documents.aiohttp.ClientSession", return_value=session), patch("linked_documents.aiohttp.TCPConnector"):
        with pytest.raises(ValueError, match="20 MiB"):
            await download_linked_document("https://example.com/doc.pdf", "example.com")
