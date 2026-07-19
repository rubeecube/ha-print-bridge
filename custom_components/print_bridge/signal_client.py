"""Signal REST helpers for Print Bridge.

The integration targets signal-cli-rest-api style services.  Payload shapes
vary slightly between normal and json-rpc modes, so parsing is intentionally
tolerant while the coordinator applies the security policy.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import json
import os
import re
from typing import Any
from urllib.parse import quote

import aiohttp

from .document_converter import extension_for_document
from .print_handler import sanitize_ipp_job_name

_DATA_URI_RE = re.compile(
    r"^data:(?P<mime>[^;,]+)?(?:;filename=(?P<filename>[^;,]+))?;base64,(?P<data>.+)$",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class SignalAttachment:
    """One attachment referenced by a received Signal message."""

    filename: str
    content_type: str | None = None
    attachment_id: str | None = None
    data: bytes | None = None


@dataclass(frozen=True)
class SignalMessage:
    """Normalized Signal message metadata relevant to Print Bridge."""

    message_id: str
    sender: str
    sender_name: str | None = None
    sender_uuid: str | None = None
    group_id: str | None = None
    group_name: str | None = None
    text: str = ""
    timestamp: str = ""
    attachments: tuple[SignalAttachment, ...] = field(default_factory=tuple)


class SignalRestClient:
    """Small async client for signal-cli-rest-api compatible endpoints."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        base_url: str,
        account: str,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._account = account

    async def receive(self) -> Any:
        """Fetch one batch of received Signal events."""
        path = f"/v1/receive/{quote(self._account, safe='')}"
        url = f"{self._base_url}{path}"
        try:
            async with self._session.get(
                url,
                headers={"Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"Signal receive failed: HTTP {resp.status}")
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError):
            # json-rpc mode exposes the same endpoint as a WebSocket stream.
            return await self._receive_one_websocket_event(path)

    async def _receive_one_websocket_event(self, path: str) -> Any:
        ws_base = self._base_url
        if ws_base.startswith("https://"):
            ws_base = "wss://" + ws_base[len("https://"):]
        elif ws_base.startswith("http://"):
            ws_base = "ws://" + ws_base[len("http://"):]
        url = f"{ws_base}{path}"
        async with self._session.ws_connect(
            url,
            timeout=10,
            receive_timeout=30,
        ) as ws:
            msg = await ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                return json.loads(msg.data)
            if msg.type == aiohttp.WSMsgType.BINARY:
                return json.loads(msg.data.decode("utf-8", errors="replace"))
            if msg.type == aiohttp.WSMsgType.ERROR:
                raise RuntimeError(f"Signal websocket failed: {ws.exception()}")
            return []

    async def list_groups(self) -> list[dict[str, Any]]:
        """Return groups known to the Signal account."""
        url = f"{self._base_url}/v1/groups/{quote(self._account, safe='')}"
        async with self._session.get(
            url,
            headers={"Accept": "application/json"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"Signal group check failed: HTTP {resp.status}")
            payload = await resp.json(content_type=None)
        return normalise_signal_groups(payload)

    async def get_attachment(self, attachment_id: str) -> bytes:
        """Download a received attachment by ID."""
        url = f"{self._base_url}/v1/attachments/{quote(attachment_id, safe='')}"
        async with self._session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            if resp.status >= 400:
                raise RuntimeError(
                    f"Signal attachment download failed for {attachment_id}: HTTP {resp.status}"
                )
            return await resp.read()

    async def send_message(self, recipients: list[str], message: str) -> None:
        """Send a plain Signal message to contacts or groups."""
        if not recipients:
            return
        url = f"{self._base_url}/v2/send"
        async with self._session.post(
            url,
            json={
                "message": message,
                "number": self._account,
                "recipients": recipients,
            },
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"Signal send failed: HTTP {resp.status}")


def parse_signal_messages(payload: Any) -> list[SignalMessage]:
    """Normalize a receive payload into SignalMessage objects."""
    messages: list[SignalMessage] = []
    for envelope in _iter_envelopes(payload):
        message = _message_from_envelope(envelope)
        if message is not None:
            messages.append(message)
    return messages


def normalise_signal_groups(payload: Any) -> list[dict[str, Any]]:
    """Return a compact list of Signal groups from REST payloads."""
    if isinstance(payload, dict):
        candidates = (
            payload.get("groups")
            or payload.get("result")
            or payload.get("data")
            or payload.get("items")
            or []
        )
    else:
        candidates = payload
    if isinstance(candidates, dict):
        candidates = candidates.get("groups") or candidates.get("items") or []
    if not isinstance(candidates, list):
        return []

    groups: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        group_id = _first_text(
            item,
            "id",
            "groupId",
            "group_id",
            "internal_id",
            "masterKey",
        )
        if not group_id:
            continue
        groups.append(
            {
                "id": group_id,
                "name": _first_text(item, "name", "title") or "",
                "members": item.get("members", []),
                "blocked": bool(item.get("blocked", False)),
                "pending": bool(item.get("pending", False)),
            }
        )
    return groups


def _iter_envelopes(payload: Any):
    if payload is None:
        return
    if isinstance(payload, list):
        for item in payload:
            yield from _iter_envelopes(item)
        return
    if not isinstance(payload, dict):
        return

    for key in ("envelopes", "messages", "items", "events"):
        value = payload.get(key)
        if isinstance(value, list):
            for item in value:
                yield from _iter_envelopes(item)
            return

    result = payload.get("result")
    if isinstance(result, (dict, list)):
        yield from _iter_envelopes(result)
        return

    envelope = payload.get("envelope")
    if isinstance(envelope, dict):
        yield envelope
        return

    yield payload


def _message_from_envelope(envelope: dict[str, Any]) -> SignalMessage | None:
    data_message = _first_dict(
        envelope,
        "dataMessage",
        "data_message",
        "message",
    )
    sync_message = _first_dict(envelope, "syncMessage", "sync_message")
    if data_message is None and sync_message is not None:
        data_message = _first_dict(sync_message, "sentMessage", "sent_message")
    if data_message is None:
        data_message = envelope

    sender = (
        _first_text(envelope, "sourceNumber", "source", "sender", "from")
        or _first_text(data_message, "sourceNumber", "source", "sender", "from")
        or ""
    )
    sender_uuid = (
        _first_text(envelope, "sourceUuid", "sourceUUID", "source_uuid")
        or _first_text(data_message, "sourceUuid", "sourceUUID", "source_uuid")
    )
    sender_name = (
        _first_text(envelope, "sourceName", "source_name", "senderName")
        or _first_text(data_message, "sourceName", "source_name", "senderName")
    )
    group_info = (
        _first_dict(data_message, "groupInfo", "group_info", "groupV2")
        or _first_dict(envelope, "groupInfo", "group_info", "groupV2")
        or {}
    )
    group_id = _first_text(group_info, "groupId", "group_id", "id")
    if group_id and not group_id.startswith("group."):
        group_id = f"group.{group_id}"
    group_name = _first_text(group_info, "name", "title")
    text = (
        _first_text(data_message, "message", "body", "text")
        or _first_text(envelope, "message", "body", "text")
        or ""
    )
    timestamp = str(
        _first_text(data_message, "timestamp", "serverTimestamp")
        or _first_text(envelope, "timestamp", "serverTimestamp")
        or ""
    )
    message_id = (
        _first_text(envelope, "id", "messageId", "message_id")
        or _first_text(data_message, "id", "messageId", "message_id")
        or f"{sender or sender_uuid or 'unknown'}:{group_id or 'direct'}:{timestamp}"
    )
    raw_attachments = (
        data_message.get("attachments")
        or envelope.get("attachments")
        or []
    )
    attachments = tuple(_parse_attachments(raw_attachments, message_id))
    if not (sender or sender_uuid or group_id or text or attachments):
        return None
    return SignalMessage(
        message_id=str(message_id),
        sender=str(sender or sender_uuid or ""),
        sender_name=sender_name,
        sender_uuid=sender_uuid,
        group_id=group_id,
        group_name=group_name,
        text=str(text or ""),
        timestamp=timestamp,
        attachments=attachments,
    )


def _parse_attachments(raw_attachments: Any, message_id: str) -> list[SignalAttachment]:
    if isinstance(raw_attachments, dict):
        raw_items = list(raw_attachments.values())
    elif isinstance(raw_attachments, list):
        raw_items = raw_attachments
    else:
        raw_items = []

    attachments: list[SignalAttachment] = []
    for index, item in enumerate(raw_items, start=1):
        attachment = _parse_attachment(item, message_id, index)
        if attachment is not None:
            attachments.append(attachment)
    return attachments


def _parse_attachment(
    item: Any,
    message_id: str,
    index: int,
) -> SignalAttachment | None:
    if isinstance(item, str):
        data_uri = _DATA_URI_RE.match(item.strip())
        if data_uri:
            content_type = data_uri.group("mime") or "application/octet-stream"
            filename = data_uri.group("filename") or _generated_filename(
                message_id, index, content_type
            )
            return SignalAttachment(
                filename=sanitize_ipp_job_name(filename),
                content_type=content_type,
                data=base64.b64decode(data_uri.group("data")),
            )
        return SignalAttachment(
            filename=_generated_filename(message_id, index, None),
            attachment_id=item,
        )
    if not isinstance(item, dict):
        return None

    content_type = _first_text(item, "contentType", "content_type", "mimeType", "mime")
    filename = (
        _first_text(item, "filename", "fileName", "name")
        or _filename_from_path(_first_text(item, "path", "localPath", "storedFilename"))
        or _generated_filename(message_id, index, content_type)
    )
    attachment_id = _first_text(item, "id", "attachmentId", "attachment_id", "digest")
    inline_data = _first_text(item, "data", "content", "base64", "body")
    data: bytes | None = None
    if inline_data:
        data_match = _DATA_URI_RE.match(inline_data.strip())
        if data_match:
            content_type = content_type or data_match.group("mime")
            filename = data_match.group("filename") or filename
            data = base64.b64decode(data_match.group("data"))
        else:
            data = base64.b64decode(inline_data)

    return SignalAttachment(
        filename=sanitize_ipp_job_name(filename),
        content_type=content_type,
        attachment_id=attachment_id,
        data=data,
    )


def _generated_filename(
    message_id: str,
    index: int,
    content_type: str | None,
) -> str:
    ext = extension_for_document(None, content_type) or ".bin"
    safe_message = sanitize_ipp_job_name(str(message_id)).replace(".", "_")[:32]
    return f"signal_{safe_message}_{index}{ext}"


def _filename_from_path(path: str | None) -> str | None:
    if not path:
        return None
    return os.path.basename(path)


def _first_dict(source: dict[str, Any], *keys: str) -> dict[str, Any] | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, dict):
            return value
    return None


def _first_text(source: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = source.get(key)
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            continue
        text = str(value).strip()
        if text:
            return text
    return None
