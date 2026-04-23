"""Constants for the Auto Print integration."""

DOMAIN = "auto_print"

# ---------------------------------------------------------------------------
# Config-entry keys — set once during initial setup (printer only)
# ---------------------------------------------------------------------------
CONF_CUPS_URL = "cups_url"
CONF_PRINTER_NAME = "printer_name"

# ---------------------------------------------------------------------------
# Options-flow keys — editable after setup via "Configure"
# ---------------------------------------------------------------------------
CONF_ALLOWED_SENDERS = "allowed_senders"   # list[str]; empty = accept all
CONF_DUPLEX_MODE = "duplex_mode"
CONF_BOOKLET_PATTERNS = "booklet_patterns"  # list[str]
CONF_AUTO_DELETE = "auto_delete"
CONF_QUEUE_FOLDER = "queue_folder"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_CUPS_URL = "http://10.0.0.23:631"
DEFAULT_DUPLEX_MODE = "two-sided-long-edge"
DEFAULT_AUTO_DELETE = True
DEFAULT_QUEUE_FOLDER = "/media/print_queue"

# Human-readable labels for duplex mode selector
DUPLEX_MODES: dict[str, str] = {
    "one-sided": "One-sided",
    "two-sided-long-edge": "Two-sided — portrait (long edge)",
    "two-sided-short-edge": "Two-sided — landscape (short edge)",
}

# ---------------------------------------------------------------------------
# Service names and field names
# ---------------------------------------------------------------------------
SERVICE_PRINT_FILE = "print_file"
SERVICE_CLEAR_QUEUE = "clear_queue"
SERVICE_PROCESS_IMAP_PART = "process_imap_part"

FIELD_FILE_PATH = "file_path"
FIELD_DUPLEX = "duplex"
FIELD_BOOKLET = "booklet"

# ---------------------------------------------------------------------------
# Entity unique-id suffixes
# ---------------------------------------------------------------------------
SENSOR_QUEUE_DEPTH = "queue_depth"
SENSOR_LAST_JOB = "last_job"
BINARY_SENSOR_PRINTER_ONLINE = "printer_online"
BUTTON_TEST_PAGE = "test_page"

# ---------------------------------------------------------------------------
# State attribute names
# ---------------------------------------------------------------------------
ATTR_LAST_STATUS = "last_status"
ATTR_LAST_FILENAME = "last_filename"
