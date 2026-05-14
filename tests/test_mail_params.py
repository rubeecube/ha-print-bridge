"""Tests for parsing per-email print parameters."""

from custom_components.print_bridge.mail_params import parse_mail_print_parameters


def test_subject_bracket_params_are_parsed():
    params = parse_mail_print_parameters(
        "[pb duplex=short-edge copies=2 dpi=150 reply=true] Weekly packet",
        "",
    )

    assert params.duplex == "two-sided-short-edge"
    assert params.copies == 2
    assert params.raster_dpi == 150
    assert params.reply is True


def test_body_print_bridge_line_supports_named_settings():
    params = parse_mail_print_parameters(
        "Print this",
        'Print-Bridge: booklet=true; paper=a4; orientation=landscape; attachment="Au Puits"',
    )

    assert params.booklet is True
    assert params.media == "iso_a4_210x297mm"
    assert params.orientation == "landscape"
    assert params.attachment_filter == "Au Puits"


def test_invalid_params_are_ignored():
    params = parse_mail_print_parameters(
        "[pb copies=30 orientation=sideways booklet=maybe dpi=1000]",
        "PB: sides=unknown; reply=no",
    )

    assert params.copies is None
    assert params.orientation is None
    assert params.booklet is None
    assert params.duplex is None
    assert params.raster_dpi is None
    assert params.reply is False


def test_quality_alias_sets_raster_dpi():
    params = parse_mail_print_parameters(
        "[pb quality=fast]",
        "Print-Bridge: quality=best",
    )

    assert params.raster_dpi == 600
