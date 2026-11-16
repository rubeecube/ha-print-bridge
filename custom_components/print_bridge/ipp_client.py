"""IPP requests over aiohttp for the Print Bridge integration.

Packet construction stays in print_handler (no I/O, no Home Assistant
dependencies).  Print-Job, Get-Printer-Attributes, and the Direct IPP
reachability probe all POST through async_post_ipp.
"""

from __future__ import annotations

import aiohttp

from .print_handler import (
    build_get_printer_attributes_packet,
    http_url_to_ipp_uri,
    ipp_response_is_valid,
)

_IPP_HEADERS = {"Content-Type": "application/ipp"}


async def async_post_ipp(
    session: aiohttp.ClientSession,
    http_url: str,
    packet: bytes,
    *,
    timeout: aiohttp.ClientTimeout,
) -> tuple[int, bytes]:
    """POST *packet* to *http_url* and return ``(http_status, body)``.

    The caller supplies *timeout* and decides how to treat the HTTP status
    and IPP body.  Connection and timeout errors propagate unchanged.
    """
    async with session.post(
        http_url,
        data=packet,
        headers=_IPP_HEADERS,
        timeout=timeout,
    ) as resp:
        return resp.status, await resp.read()


async def async_probe_ipp_endpoint(
    session: aiohttp.ClientSession,
    http_url: str,
    *,
    timeout: float = 5,
) -> bool:
    """Return True if *http_url* answers Get-Printer-Attributes.

    Some printers close the connection on HTTP HEAD, or on a POST with no
    IPP body.  The Brother debut/1.30 HTTP server is one example.  A
    parseable IPP response — status ``0x0000`` or any other IPP status —
    means the endpoint is reachable, whatever the HTTP status was.
    Connection and timeout errors mean it is not.  *timeout* is the total
    request budget in seconds.
    """
    try:
        printer_uri = http_url_to_ipp_uri(http_url)
        packet = build_get_printer_attributes_packet(printer_uri)
        _http_status, body = await async_post_ipp(
            session,
            http_url,
            packet,
            timeout=aiohttp.ClientTimeout(total=timeout),
        )
    except (aiohttp.ClientError, OSError, TimeoutError):
        return False
    return ipp_response_is_valid(body)
