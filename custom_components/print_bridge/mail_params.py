"""Parse per-email Print Bridge parameters from mail subject/body text."""

from __future__ import annotations

from dataclasses import dataclass
import re

from .const import DUPLEX_MODES

_PARAM_LINE_RE = re.compile(
    r"(?im)^\s*(?:print[-_\s]?bridge|pb)\s*:\s*(?P<body>.+?)\s*$"
)
_BRACKET_RE = re.compile(
    r"(?i)\[(?:print[-_\s]?bridge|pb)\s+(?P<body>[^\]]+)\]"
)
_KEY_VALUE_RE = re.compile(
    r"(?P<key>[a-z][a-z0-9_-]*)\s*=\s*"
    r"(?P<value>\"[^\"]*\"|'[^']*'|.*?)(?=(?:\s+[a-z][a-z0-9_-]*\s*=)|[;,\n]|$)",
    re.IGNORECASE,
)

_TRUE_VALUES = {"1", "true", "yes", "y", "on"}
_FALSE_VALUES = {"0", "false", "no", "n", "off"}

_DUPLEX_ALIASES = {
    "one": "one-sided",
    "single": "one-sided",
    "simplex": "one-sided",
    "one-sided": "one-sided",
    "onesided": "one-sided",
    "long": "two-sided-long-edge",
    "long-edge": "two-sided-long-edge",
    "longedge": "two-sided-long-edge",
    "portrait": "two-sided-long-edge",
    "two-sided-long-edge": "two-sided-long-edge",
    "short": "two-sided-short-edge",
    "short-edge": "two-sided-short-edge",
    "shortedge": "two-sided-short-edge",
    "landscape": "two-sided-short-edge",
    "two-sided-short-edge": "two-sided-short-edge",
}

_MEDIA_ALIASES = {
    "a4": "iso_a4_210x297mm",
    "iso-a4": "iso_a4_210x297mm",
    "iso_a4": "iso_a4_210x297mm",
    "letter": "na_letter_8.5x11in",
    "us-letter": "na_letter_8.5x11in",
    "legal": "na_legal_8.5x14in",
    "us-legal": "na_legal_8.5x14in",
}


@dataclass(frozen=True)
class MailPrintParameters:
    """Per-email print settings parsed from the message content."""

    duplex: str | None = None
    booklet: bool | None = None
    copies: int | None = None
    orientation: str | None = None
    media: str | None = None
    attachment_filter: str | None = None
    reply: bool | None = None

    @property
    def has_values(self) -> bool:
        return any(
            value is not None
            for value in (
                self.duplex,
                self.booklet,
                self.copies,
                self.orientation,
                self.media,
                self.attachment_filter,
                self.reply,
            )
        )

    def as_dict(self) -> dict[str, str | int | bool]:
        data: dict[str, str | int | bool] = {}
        for key in (
            "duplex",
            "booklet",
            "copies",
            "orientation",
            "media",
            "attachment_filter",
            "reply",
        ):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return data


def parse_mail_print_parameters(subject: str = "", body: str = "") -> MailPrintParameters:
    """Return print parameters declared in an email subject or body.

    Supported forms:
      [pb duplex=short-edge copies=2]
      Print-Bridge: duplex=one-sided; booklet=true; paper=a4; reply=true
    """
    raw_pairs: dict[str, str] = {}
    for source in _iter_parameter_blocks(subject or "", body or ""):
        for match in _KEY_VALUE_RE.finditer(source):
            key = match.group("key").strip().lower().replace("-", "_")
            value = _strip_quotes(match.group("value").strip())
            raw_pairs[key] = value

    return MailPrintParameters(
        duplex=_parse_duplex(raw_pairs.get("duplex") or raw_pairs.get("sides")),
        booklet=_parse_bool(raw_pairs.get("booklet")),
        copies=_parse_copies(raw_pairs.get("copies")),
        orientation=_parse_orientation(raw_pairs.get("orientation")),
        media=_parse_media(raw_pairs.get("media") or raw_pairs.get("paper")),
        attachment_filter=(
            raw_pairs.get("attachment")
            or raw_pairs.get("attachment_filter")
            or raw_pairs.get("file")
        ),
        reply=_parse_bool(raw_pairs.get("reply") or raw_pairs.get("status_reply")),
    )


def _iter_parameter_blocks(subject: str, body: str) -> list[str]:
    blocks = [match.group("body") for match in _BRACKET_RE.finditer(subject)]
    blocks.extend(match.group("body") for match in _PARAM_LINE_RE.finditer(body))
    return blocks


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1].strip()
    return value.strip()


def _parse_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return None


def _parse_duplex(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower().replace("_", "-")
    duplex = _DUPLEX_ALIASES.get(normalized)
    return duplex if duplex in DUPLEX_MODES else None


def _parse_copies(value: str | None) -> int | None:
    if not value:
        return None
    try:
        copies = int(value)
    except ValueError:
        return None
    return copies if 1 <= copies <= 20 else None


def _parse_orientation(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower().replace("_", "-")
    if normalized in {"portrait", "landscape"}:
        return normalized
    return None


def _parse_media(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower().replace(" ", "-")
    return _MEDIA_ALIASES.get(normalized, normalized)
