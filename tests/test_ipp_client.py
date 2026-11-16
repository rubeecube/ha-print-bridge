"""Tests for the shared IPP aiohttp transport and reachability probe."""

from __future__ import annotations

import struct
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.print_bridge.ipp_client import (
    async_post_ipp,
    async_probe_ipp_endpoint,
)
from custom_components.print_bridge.print_handler import http_url_to_ipp_uri


def _response(body: bytes, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.read = AsyncMock(return_value=body)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _ipp_body(status_code: int = 0x0000) -> bytes:
    return struct.pack(">HHI", 0x0200, status_code, 1) + b"\x03"


async def test_async_post_ipp_posts_packet_and_returns_status_and_body() -> None:
    body = _ipp_body()
    session = MagicMock()
    session.post.return_value = _response(body, status=200)
    timeout = aiohttp.ClientTimeout(total=15, sock_connect=30)

    status, payload = await async_post_ipp(
        session,
        "http://printer.local/ipp/print",
        b"ipp-packet",
        timeout=timeout,
    )

    assert status == 200
    assert payload == body
    session.post.assert_called_once_with(
        "http://printer.local/ipp/print",
        data=b"ipp-packet",
        headers={"Content-Type": "application/ipp"},
        timeout=timeout,
    )


async def test_async_post_ipp_propagates_connection_errors() -> None:
    session = MagicMock()
    session.post.side_effect = aiohttp.ClientError("closed")

    with pytest.raises(aiohttp.ClientError):
        await async_post_ipp(
            session,
            "http://printer.local/ipp/print",
            b"ipp-packet",
            timeout=aiohttp.ClientTimeout(total=5),
        )


async def test_async_probe_ipp_endpoint_posts_get_printer_attributes() -> None:
    """Reachability is Get-Printer-Attributes, not HTTP HEAD.

    Printers such as the Brother debut/1.30 server close HEAD and empty POSTs.
    """
    url = "http://printer.local:631/ipp/print"
    session = MagicMock()
    session.post.return_value = _response(_ipp_body())

    assert await async_probe_ipp_endpoint(session, url, timeout=5) is True

    session.post.assert_called_once()
    args, kwargs = session.post.call_args
    assert args[0] == url
    assert kwargs["headers"] == {"Content-Type": "application/ipp"}
    assert isinstance(kwargs["timeout"], aiohttp.ClientTimeout)
    assert kwargs["timeout"].total == 5
    packet = kwargs["data"]
    assert struct.unpack(">H", packet[2:4])[0] == 0x000B
    assert http_url_to_ipp_uri(url).encode() in packet
    session.head.assert_not_called()


@pytest.mark.parametrize("status_code", [0x0000, 0x040A, 0x0507])
async def test_async_probe_ipp_endpoint_accepts_any_valid_ipp_status(
    status_code: int,
) -> None:
    session = MagicMock()
    session.post.return_value = _response(_ipp_body(status_code))

    assert await async_probe_ipp_endpoint(
        session, "http://printer.local/ipp/print"
    ) is True


async def test_async_probe_ipp_endpoint_ignores_http_status_when_body_is_ipp() -> None:
    session = MagicMock()
    session.post.return_value = _response(_ipp_body(), status=500)

    assert await async_probe_ipp_endpoint(
        session, "http://printer.local/ipp/print"
    ) is True


async def test_async_probe_ipp_endpoint_returns_false_on_non_ipp_body() -> None:
    session = MagicMock()
    session.post.return_value = _response(b"<!DOCTYPE HTML>")

    assert await async_probe_ipp_endpoint(
        session, "http://printer.local/ipp/print"
    ) is False


@pytest.mark.parametrize(
    "exc",
    ["ClientError", "ServerDisconnectedError", "OSError", "TimeoutError"],
)
async def test_async_probe_ipp_endpoint_returns_false_on_connection_errors(
    exc: str,
) -> None:
    session = MagicMock()
    if exc == "TimeoutError":
        err_cls = TimeoutError
    elif exc == "OSError":
        err_cls = OSError
    else:
        err_cls = getattr(aiohttp, exc)
    session.post.side_effect = err_cls("closed")

    assert await async_probe_ipp_endpoint(
        session, "http://printer.local/ipp/print"
    ) is False
