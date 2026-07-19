"""Signal REST payload parsing tests."""

from __future__ import annotations

import base64

from custom_components.print_bridge.signal_client import (
    normalise_signal_groups,
    parse_signal_messages,
)


def test_parse_signal_group_message_with_attachment() -> None:
    payload = [
        {
            "envelope": {
                "sourceNumber": "+15550100",
                "sourceUuid": "source-uuid",
                "sourceName": "Sender",
                "timestamp": 123456,
                "dataMessage": {
                    "message": "please print",
                    "attachments": [
                        {
                            "id": "att-1",
                            "filename": "document.pdf",
                            "contentType": "application/pdf",
                        }
                    ],
                    "groupInfo": {
                        "groupId": "abc123",
                        "name": "Print Group",
                    },
                },
            }
        }
    ]

    messages = parse_signal_messages(payload)

    assert len(messages) == 1
    message = messages[0]
    assert message.sender == "+15550100"
    assert message.sender_uuid == "source-uuid"
    assert message.group_id == "group.abc123"
    assert message.group_name == "Print Group"
    assert message.attachments[0].filename == "document.pdf"
    assert message.attachments[0].attachment_id == "att-1"


def test_parse_inline_data_attachment() -> None:
    encoded = base64.b64encode(b"hello").decode()
    payload = {
        "envelope": {
            "source": "+15550100",
            "timestamp": 1,
            "dataMessage": {
                "attachments": [
                    {
                        "filename": "note.txt",
                        "contentType": "text/plain",
                        "data": encoded,
                    }
                ]
            },
        }
    }

    message = parse_signal_messages(payload)[0]

    assert message.attachments[0].data == b"hello"


def test_normalise_signal_groups() -> None:
    payload = {
        "groups": [
            {"id": "group.alpha", "name": "Alpha", "members": ["+1"]},
            {"groupId": "beta", "title": "Beta"},
            {"name": "missing id"},
        ]
    }

    groups = normalise_signal_groups(payload)

    assert groups == [
        {
            "id": "group.alpha",
            "name": "Alpha",
            "members": ["+1"],
            "blocked": False,
            "pending": False,
        },
        {
            "id": "beta",
            "name": "Beta",
            "members": [],
            "blocked": False,
            "pending": False,
        },
    ]
