"""Tests for parsing per-email print parameters."""

from custom_components.print_bridge.mail_params import parse_mail_print_parameters


def test_subject_bracket_params_are_parsed():
    params = parse_mail_print_parameters(
        "[pb duplex=short-edge nb_copies=2 collate=false dpi=150 reply=true] Weekly packet",
        "",
    )

    assert params.duplex == "two-sided-short-edge"
    assert params.copies == 2
    assert params.collate is False
    assert params.raster_dpi == 150
    assert params.reply is True
    assert params.errors == ()


def test_body_print_bridge_line_supports_named_settings():
    params = parse_mail_print_parameters(
        "Print this",
        'Print-Bridge: booklet=true; paper=a4; orientation=landscape; attachment="Au Puits"; '
        'ignore_filename="draft"; only_extensions="pdf docx"; skip_extensions=png',
    )

    assert params.booklet is True
    assert params.media == "iso_a4_210x297mm"
    assert params.orientation == "landscape"
    assert params.attachment_filter == "Au Puits"
    assert params.attachment_ignore_filter == "draft"
    assert params.allowed_extensions == (".pdf", ".docx")
    assert params.ignored_extensions == (".png",)


def test_invalid_params_report_errors():
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
    assert len(params.errors) == 5


def test_quality_alias_sets_raster_dpi():
    params = parse_mail_print_parameters(
        "[pb quality=fast]",
        "Print-Bridge: quality=best",
    )

    assert params.raster_dpi == 600


def test_reverse_order_params_are_parsed():
    params = parse_mail_print_parameters(
        "[pb reverse=false]",
        "Print-Bridge: reverse_order=true",
    )

    assert params.reverse_order is True


def test_order_alias_controls_reverse_order():
    reversed_params = parse_mail_print_parameters("[pb order=reverse]", "")
    normal_params = parse_mail_print_parameters("[pb page_order=normal]", "")

    assert reversed_params.reverse_order is True
    assert normal_params.reverse_order is False


def test_config_request_and_plain_body_copy_number_are_parsed():
    config = parse_mail_print_parameters("[pb config]", "")
    copies = parse_mail_print_parameters("Print", "3")

    assert config.config_request is True
    assert copies.copies == 3


def test_invalid_bool_values_report_errors():
    params = parse_mail_print_parameters("[pb collate=banana reverse=maybe]", "")

    assert params.collate is None
    assert params.reverse_order is None
    assert len(params.errors) == 2


def test_invalid_extension_values_report_errors():
    params = parse_mail_print_parameters('[pb allowed_extensions="pdf ../bad"]', "")

    assert params.allowed_extensions is None
    assert len(params.errors) == 1
    assert "Invalid allowed_extensions value" in params.errors[0]
