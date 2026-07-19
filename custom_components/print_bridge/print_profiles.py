"""Named print profile parsing and resolution helpers."""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import Any

from .const import DEFAULT_PRINT_TYPE
from .mail_params import MailPrintParameters, parse_mail_print_parameters

_PROFILE_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
_PROFILE_LINE_RE = re.compile(
    r"^\s*(?P<name>[a-zA-Z0-9][a-zA-Z0-9_-]{0,31})\s*=\s*(?P<body>.+?)\s*$"
)

_MERGE_FIELDS = (
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
)


@dataclass(frozen=True)
class PrintProfile:
    """A named set of optional print parameters."""

    name: str
    params: MailPrintParameters
    builtin: bool = False
    raw: str = ""


BUILTIN_PRINT_PROFILES: dict[str, PrintProfile] = {
    "normal": PrintProfile("normal", MailPrintParameters(), builtin=True),
    "simplex": PrintProfile(
        "simplex",
        MailPrintParameters(duplex="one-sided", booklet=False),
        builtin=True,
    ),
    "duplex": PrintProfile(
        "duplex",
        MailPrintParameters(duplex="two-sided-long-edge", booklet=False),
        builtin=True,
    ),
    "booklet": PrintProfile(
        "booklet",
        MailPrintParameters(
            duplex="two-sided-short-edge",
            booklet=True,
            orientation="landscape",
        ),
        builtin=True,
    ),
    "draft": PrintProfile(
        "draft",
        MailPrintParameters(duplex="one-sided", booklet=False, raster_dpi=150),
        builtin=True,
    ),
}


def normalize_print_type(value: Any) -> str:
    """Return a canonical print profile name."""
    return str(value or "").strip().lower().replace(" ", "_")


def parse_print_profiles(value: Any) -> tuple[dict[str, PrintProfile], list[str]]:
    """Return built-in profiles plus valid custom profiles from option lines."""
    profiles = dict(BUILTIN_PRINT_PROFILES)
    errors: list[str] = []
    seen_custom: set[str] = set()

    for raw_line in _iter_profile_lines(value):
        line = raw_line.strip()
        if not line:
            continue
        match = _PROFILE_LINE_RE.match(line)
        if not match:
            errors.append(
                f"Invalid print profile '{line}': expected name=key=value ..."
            )
            continue
        name = normalize_print_type(match.group("name"))
        body = match.group("body").strip()
        if not _PROFILE_NAME_RE.fullmatch(name):
            errors.append(
                f"Invalid print profile name '{match.group('name')}': "
                "use letters, numbers, dashes, or underscores"
            )
            continue
        if name in BUILTIN_PRINT_PROFILES:
            errors.append(
                f"Invalid print profile '{name}': built-in profile names cannot be redefined"
            )
            continue
        if name in seen_custom:
            errors.append(f"Duplicate print profile '{name}'")
            continue
        params = parse_mail_print_parameters("", f"PB: {body}")
        if params.errors:
            errors.extend(f"{name}: {error}" for error in params.errors)
            continue
        if params.print_type:
            errors.append(f"{name}: profiles cannot reference another print type")
            continue
        if params.config_request:
            errors.append(
                f"{name}: config/help commands are not valid profile settings"
            )
            continue
        if not params.has_values:
            errors.append(f"{name}: profile must contain at least one setting")
            continue
        seen_custom.add(name)
        profiles[name] = PrintProfile(name, params, builtin=False, raw=line)

    return profiles, errors


def profile_names(value: Any) -> list[str]:
    """Return profile names in display order."""
    profiles, _errors = parse_print_profiles(value)
    builtin_order = [name for name in BUILTIN_PRINT_PROFILES if name in profiles]
    custom_order = sorted(
        name for name in profiles if name not in BUILTIN_PRINT_PROFILES
    )
    return [*builtin_order, *custom_order]


def resolve_default_print_type(value: Any, profiles: dict[str, PrintProfile]) -> str:
    """Return a configured profile name, falling back to normal."""
    name = normalize_print_type(value)
    if name in profiles:
        return name
    return DEFAULT_PRINT_TYPE


def merge_print_parameters(
    base: MailPrintParameters,
    override: MailPrintParameters,
) -> MailPrintParameters:
    """Return base profile parameters with explicit override values applied."""
    values = {
        field_name: getattr(base, field_name)
        for field_name in _MERGE_FIELDS
    }
    for field_name in _MERGE_FIELDS:
        value = getattr(override, field_name)
        if value is not None:
            values[field_name] = value
    return replace(
        base,
        **values,
        print_type=override.print_type or base.print_type,
        errors=override.errors,
        config_request=override.config_request,
    )


def print_profile_label(name: str) -> str:
    """Return a compact UI label for a print profile name."""
    return name.replace("_", " ").replace("-", " ").title()


def _iter_profile_lines(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return value.splitlines()
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    return [str(value)]
