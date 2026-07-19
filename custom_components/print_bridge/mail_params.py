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
_REVERSE_ORDER_TRUE = {"reverse", "reversed", "last-first", "last-to-first", "desc", "descending"}
_REVERSE_ORDER_FALSE = {"normal", "forward", "first-first", "first-to-last", "asc", "ascending"}
_CONFIG_REQUEST_VALUES = {"config", "configuration", "settings", "parameters", "help"}
_BODY_COPIES_LINE_RE = re.compile(
    r"(?im)^\s*(?:copies?|nb[_\s-]?copies?)\s*[:=]?\s*(?P<copies>\d{1,2})\s*$"
)
_EXTENSION_LIST_RE = re.compile(r"[\s,;]+")
_VALID_EXTENSION_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")

_KEY_ALIASES = {
    "attachment": "attachment_filter",
    "attachment_filter": "attachment_filter",
    "attachment_ignore": "attachment_ignore_filter",
    "attachment_ignore_filter": "attachment_ignore_filter",
    "allowed_ext": "allowed_extensions",
    "allowed_extension": "allowed_extensions",
    "allowed_extensions": "allowed_extensions",
    "allow_ext": "allowed_extensions",
    "allow_extensions": "allowed_extensions",
    "booklet": "booklet",
    "collate": "collate",
    "collated": "collate",
    "command": "command",
    "config": "config_request",
    "config_request": "config_request",
    "configuration": "config_request",
    "copies": "copies",
    "copy": "copies",
    "duplex": "duplex",
    "exclude_attachment": "attachment_ignore_filter",
    "exclude_ext": "ignored_extensions",
    "exclude_extension": "ignored_extensions",
    "exclude_extensions": "ignored_extensions",
    "exclude_file": "attachment_ignore_filter",
    "exclude_filename": "attachment_ignore_filter",
    "exclude_filenames": "attachment_ignore_filter",
    "file": "attachment_filter",
    "ignore_attachment": "attachment_ignore_filter",
    "ignore_attachment_filter": "attachment_ignore_filter",
    "ignore_ext": "ignored_extensions",
    "ignore_extension": "ignored_extensions",
    "ignore_extensions": "ignored_extensions",
    "ignore_file": "attachment_ignore_filter",
    "ignore_filename": "attachment_ignore_filter",
    "ignore_filenames": "attachment_ignore_filter",
    "ignored_ext": "ignored_extensions",
    "ignored_extensions": "ignored_extensions",
    "include_ext": "allowed_extensions",
    "include_extension": "allowed_extensions",
    "include_extensions": "allowed_extensions",
    "media": "media",
    "nb_copies": "copies",
    "nb_copy": "copies",
    "nbcopies": "copies",
    "only_ext": "allowed_extensions",
    "only_extension": "allowed_extensions",
    "only_extensions": "allowed_extensions",
    "orientation": "orientation",
    "order": "order",
    "page_order": "order",
    "paper": "media",
    "print_extensions": "allowed_extensions",
    "quantity": "copies",
    "profile": "print_type",
    "print_type": "print_type",
    "raster_dpi": "raster_dpi",
    "reply": "reply",
    "reverse": "reverse_order",
    "reverse_order": "reverse_order",
    "settings": "config_request",
    "sides": "duplex",
    "skip_attachment": "attachment_ignore_filter",
    "skip_ext": "ignored_extensions",
    "skip_extension": "ignored_extensions",
    "skip_extensions": "ignored_extensions",
    "skip_file": "attachment_ignore_filter",
    "skip_filename": "attachment_ignore_filter",
    "skip_filenames": "attachment_ignore_filter",
    "status_reply": "reply",
    "type": "print_type",
    "dpi": "raster_dpi",
    "quality": "raster_dpi",
}

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

_QUALITY_DPI = {
    "draft": 100,
    "fast": 150,
    "normal": 300,
    "quality": 300,
    "high": 300,
    "best": 600,
}


@dataclass(frozen=True)
class MailPrintParameters:
    """Per-email print settings parsed from the message content."""

    duplex: str | None = None
    booklet: bool | None = None
    copies: int | None = None
    collate: bool | None = None
    orientation: str | None = None
    media: str | None = None
    raster_dpi: int | None = None
    attachment_filter: str | None = None
    attachment_ignore_filter: str | None = None
    allowed_extensions: tuple[str, ...] | None = None
    ignored_extensions: tuple[str, ...] | None = None
    reply: bool | None = None
    reverse_order: bool | None = None
    print_type: str | None = None
    config_request: bool = False
    errors: tuple[str, ...] = ()

    @property
    def has_values(self) -> bool:
        return any(
            value is not None
            for value in (
                self.duplex,
                self.booklet,
                self.copies,
                self.collate,
                self.orientation,
                self.media,
                self.raster_dpi,
                self.attachment_filter,
                self.attachment_ignore_filter,
                self.allowed_extensions,
                self.ignored_extensions,
                self.reply,
                self.reverse_order,
                self.print_type,
            )
        )

    def as_dict(self) -> dict[str, str | int | bool]:
        data: dict[str, str | int | bool] = {}
        for key in (
            "duplex",
            "booklet",
            "copies",
            "collate",
            "orientation",
            "media",
            "raster_dpi",
            "attachment_filter",
            "attachment_ignore_filter",
            "allowed_extensions",
            "ignored_extensions",
            "reply",
            "reverse_order",
            "print_type",
        ):
            value = getattr(self, key)
            if value is not None:
                data[key] = ",".join(value) if isinstance(value, tuple) else value
        return data


def parse_mail_print_parameters(subject: str = "", body: str = "") -> MailPrintParameters:
    """Return print parameters declared in an email subject or body.

    Supported forms:
      [pb duplex=short-edge copies=2]
      Print-Bridge: duplex=one-sided; booklet=true; paper=a4; reply=true
    """
    raw_pairs: dict[str, str] = {}
    errors: list[str] = []
    config_request = False
    for source in _iter_parameter_blocks(subject or "", body or ""):
        block = source.strip()
        if _is_config_command(block):
            config_request = True
            continue
        if _is_numeric_copies(block):
            raw_pairs["copies"] = block
            continue
        for match in _KEY_VALUE_RE.finditer(source):
            original_key = match.group("key").strip()
            key = original_key.lower().replace("-", "_")
            value = _strip_quotes(match.group("value").strip())
            canonical_key = _KEY_ALIASES.get(key)
            if canonical_key is None:
                errors.append(f"Unknown print parameter '{original_key}'")
                continue
            raw_pairs[canonical_key] = value

    if "copies" not in raw_pairs:
        body_copies = _copies_from_plain_body(body or "")
        if body_copies is not None:
            raw_pairs["copies"] = body_copies

    command = raw_pairs.get("command")
    if command:
        normalized_command = command.strip().lower().replace("_", "-").replace(" ", "-")
        if normalized_command in _CONFIG_REQUEST_VALUES:
            config_request = True
        else:
            errors.append(
                "Invalid command parameter: expected config, settings, parameters, or help"
            )

    config_value = _parse_bool(
        raw_pairs.get("config_request"),
        "config",
        errors,
    )
    if config_value is True:
        config_request = True
    elif config_value is False:
        config_request = False

    return MailPrintParameters(
        duplex=_parse_duplex(raw_pairs.get("duplex"), errors),
        booklet=_parse_bool(raw_pairs.get("booklet"), "booklet", errors),
        copies=_parse_copies(raw_pairs.get("copies"), errors),
        collate=_parse_bool(raw_pairs.get("collate"), "collate", errors),
        orientation=_parse_orientation(raw_pairs.get("orientation"), errors),
        media=_parse_media(raw_pairs.get("media"), errors),
        raster_dpi=_parse_raster_dpi(
            raw_pairs.get("raster_dpi"),
            errors,
        ),
        attachment_filter=_none_if_empty(raw_pairs.get("attachment_filter")),
        attachment_ignore_filter=_none_if_empty(
            raw_pairs.get("attachment_ignore_filter")
        ),
        allowed_extensions=_parse_extension_list(
            raw_pairs.get("allowed_extensions"),
            "allowed_extensions",
            errors,
        ),
        ignored_extensions=_parse_extension_list(
            raw_pairs.get("ignored_extensions"),
            "ignored_extensions",
            errors,
        ),
        reply=_parse_bool(raw_pairs.get("reply"), "reply", errors),
        reverse_order=_parse_reverse_order(raw_pairs, errors),
        print_type=_parse_print_type(raw_pairs.get("print_type"), errors),
        config_request=config_request,
        errors=tuple(errors),
    )


def _iter_parameter_blocks(subject: str, body: str) -> list[str]:
    blocks = [match.group("body") for match in _BRACKET_RE.finditer(subject)]
    blocks.extend(match.group("body") for match in _PARAM_LINE_RE.finditer(body))
    return blocks


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1].strip()
    return value.strip()


def _none_if_empty(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _is_config_command(value: str) -> bool:
    normalized = value.strip().lower().replace("_", "-").replace(" ", "-")
    return normalized in _CONFIG_REQUEST_VALUES


def _is_numeric_copies(value: str) -> bool:
    normalized = value.strip()
    if not normalized.isdigit():
        return False
    copies = int(normalized)
    return 1 <= copies <= 20


def _copies_from_plain_body(body: str) -> str | None:
    stripped = body.strip()
    if _is_numeric_copies(stripped):
        return stripped
    matches = list(_BODY_COPIES_LINE_RE.finditer(body))
    if not matches:
        return None
    return matches[-1].group("copies")


def _parse_bool(
    value: str | None,
    name: str,
    errors: list[str],
) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    errors.append(f"Invalid {name} value '{value}': expected true/false or 1/0")
    return None


def _parse_reverse_order(raw_pairs: dict[str, str], errors: list[str]) -> bool | None:
    explicit = _parse_bool(raw_pairs.get("reverse_order"), "reverse_order", errors)
    if explicit is not None:
        return explicit

    order = raw_pairs.get("order")
    if not order:
        return None
    normalized = order.strip().lower().replace("_", "-").replace(" ", "-")
    if normalized in _REVERSE_ORDER_TRUE:
        return True
    if normalized in _REVERSE_ORDER_FALSE:
        return False
    errors.append(
        f"Invalid order value '{order}': expected reverse or normal"
    )
    return None


def _parse_print_type(value: str | None, errors: list[str]) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower().replace(" ", "_")
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", normalized):
        return normalized
    errors.append(
        f"Invalid print_type value '{value}': expected a profile name using letters, numbers, dashes, or underscores"
    )
    return None


def _parse_duplex(value: str | None, errors: list[str]) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower().replace("_", "-")
    duplex = _DUPLEX_ALIASES.get(normalized)
    if duplex in DUPLEX_MODES:
        return duplex
    errors.append(
        f"Invalid duplex value '{value}': expected one-sided, long-edge, or short-edge"
    )
    return None


def _parse_copies(value: str | None, errors: list[str]) -> int | None:
    if not value:
        return None
    try:
        copies = int(value)
    except ValueError:
        errors.append(f"Invalid copies value '{value}': expected 1 through 20")
        return None
    if 1 <= copies <= 20:
        return copies
    errors.append(f"Invalid copies value '{value}': expected 1 through 20")
    return None


def _parse_orientation(value: str | None, errors: list[str]) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower().replace("_", "-")
    if normalized in {"portrait", "landscape"}:
        return normalized
    errors.append(
        f"Invalid orientation value '{value}': expected portrait or landscape"
    )
    return None


def _parse_media(value: str | None, errors: list[str]) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower().replace(" ", "-")
    if any(character.isspace() for character in normalized):
        errors.append(f"Invalid media value '{value}': expected an IPP media keyword")
        return None
    return _MEDIA_ALIASES.get(normalized, normalized)


def _parse_raster_dpi(value: str | None, errors: list[str]) -> int | None:
    if not value:
        return None
    normalized = value.strip().lower()
    if normalized in _QUALITY_DPI:
        return _QUALITY_DPI[normalized]
    normalized = normalized.removesuffix("dpi").strip()
    try:
        dpi = int(normalized)
    except ValueError:
        errors.append(f"Invalid dpi value '{value}': expected 72 through 600")
        return None
    if 72 <= dpi <= 600:
        return dpi
    errors.append(f"Invalid dpi value '{value}': expected 72 through 600")
    return None


def _parse_extension_list(
    value: str | None,
    name: str,
    errors: list[str],
) -> tuple[str, ...] | None:
    if not value:
        return None

    extensions: list[str] = []
    invalid: list[str] = []
    for raw_token in _EXTENSION_LIST_RE.split(value):
        token = raw_token.strip().lower()
        if not token:
            continue
        if token.startswith("*."):
            token = token[2:]
        token = token.removeprefix(".")
        if not token or not _VALID_EXTENSION_RE.fullmatch(token):
            invalid.append(raw_token)
            continue
        extension = f".{token}"
        if extension not in extensions:
            extensions.append(extension)

    if invalid:
        errors.append(
            f"Invalid {name} value '{value}': expected extensions like pdf, .docx, or *.png"
        )
        return None
    return tuple(extensions) or None
