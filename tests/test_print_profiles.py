"""Tests for named Print Bridge print profiles."""

from custom_components.print_bridge.mail_params import MailPrintParameters
from custom_components.print_bridge.print_profiles import (
    merge_print_parameters,
    parse_print_profiles,
)


def test_builtin_and_custom_print_profiles_are_parsed():
    profiles, errors = parse_print_profiles(
        [
            "weekly=booklet=true copies=2 media=a4",
            "fast_one=duplex=one-sided quality=fast",
        ]
    )

    assert errors == []
    assert set(("normal", "simplex", "duplex", "booklet", "draft")) <= set(profiles)
    assert profiles["weekly"].params.booklet is True
    assert profiles["weekly"].params.copies == 2
    assert profiles["weekly"].params.media == "iso_a4_210x297mm"
    assert profiles["fast_one"].params.raster_dpi == 150


def test_invalid_print_profiles_report_errors():
    profiles, errors = parse_print_profiles(
        [
            "booklet=copies=2",
            "bad line",
            "nested=type=duplex",
        ]
    )

    assert "normal" in profiles
    assert len(errors) == 3


def test_merge_print_parameters_keeps_explicit_overrides():
    base = MailPrintParameters(duplex="one-sided", copies=1, collate=True)
    override = MailPrintParameters(copies=3, booklet=True)

    merged = merge_print_parameters(base, override)

    assert merged.duplex == "one-sided"
    assert merged.copies == 3
    assert merged.collate is True
    assert merged.booklet is True
