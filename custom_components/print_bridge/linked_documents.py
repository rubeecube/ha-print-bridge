"""Extract and download public document links from email bodies."""

from __future__ import annotations

import ipaddress
import re
import socket
from email.message import Message
from urllib.parse import unquote, urljoin, urlsplit

import aiohttp
from bs4 import BeautifulSoup

MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
MAX_LINKS = 10


def extract_document_links(text: str, html: str = "") -> list[str]:
    """Extract HTTP links in document order without executing email HTML."""
    soup = BeautifulSoup(html or (text if "<" in text else ""), "html.parser")
    candidates = [a["href"] for a in soup.find_all("a", href=True)]
    for match in re.findall(r'https?://[^\s<>"\']+', text):
        url = match.rstrip(".,;!")
        for closing, opening in ((")", "("), ("]", "[")):
            while url.endswith(closing) and url.count(closing) > url.count(opening):
                url = url[:-1].rstrip(".,;!")
        candidates.append(url)
    return list(dict.fromkeys(str(url).strip() for url in candidates))


def validate_document_url(url: str, allowed_domains: str) -> str:
    """Require HTTP(S), an explicit allowed host, and public literal IPs."""
    parsed = urlsplit(url)
    domains = {item.strip().lower() for item in re.split(r"[\s,;]+", allowed_domains) if item.strip()}
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        raise ValueError("Document URL must be HTTP(S) without embedded credentials")
    if host not in domains:
        raise ValueError("Document host is not in allowed_link_domains")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError("Document URL must use a public address")
    if parsed.port not in {None, 80, 443}:
        raise ValueError("Document URL must use port 80 or 443")
    return host


class PublicDocumentResolver(aiohttp.resolver.DefaultResolver):
    """Validate the actual addresses used by the HTTP connector, including redirects."""

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        records = await super().resolve(host, port, family)
        if not records or any(not ipaddress.ip_address(record["host"]).is_global for record in records):
            raise ValueError("Document host resolves to a non-public address")
        return records


async def download_linked_document(url: str, allowed_domains: str) -> tuple[str, bytes, str]:
    """Download at most 20 MiB; validate every redirect and avoid HA session credentials."""
    connector = aiohttp.TCPConnector(resolver=PublicDocumentResolver())
    async with aiohttp.ClientSession(
        connector=connector,
        cookie_jar=aiohttp.DummyCookieJar(),
        timeout=aiohttp.ClientTimeout(total=60),
        trust_env=False,
        auto_decompress=False,
    ) as session:
        for _ in range(6):
            validate_document_url(url, allowed_domains)
            async with session.get(
                url, allow_redirects=False, headers={"Accept-Encoding": "identity"}
            ) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise ValueError("Document redirect has no destination")
                    url = urljoin(url, location)
                    continue
                response.raise_for_status()
                if response.content_length and response.content_length > MAX_DOCUMENT_BYTES:
                    raise ValueError("Linked document exceeds 20 MiB")
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise ValueError("Compressed HTTP document responses are unsupported")
                disposition = Message()
                disposition["Content-Disposition"] = response.headers.get("Content-Disposition", "")
                filename = disposition.get_filename() or unquote(urlsplit(url).path.rsplit("/", 1)[-1])
                filename = filename.replace("\\", "/").rsplit("/", 1)[-1] or "linked-document"
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                data = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    data.extend(chunk)
                    if len(data) > MAX_DOCUMENT_BYTES:
                        raise ValueError("Linked document exceeds 20 MiB")
                return filename, bytes(data), content_type
        raise ValueError("Too many document redirects")
