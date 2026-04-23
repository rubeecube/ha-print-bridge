"""Constants for the Auto Print integration."""

DOMAIN = "auto_print"

# Config-entry keys (set during initial setup, not editable in options)
CONF_IMAP_SERVER = "imap_server"
CONF_IMAP_PORT = "imap_port"
CONF_IMAP_USE_SSL = "imap_use_ssl"
CONF_IMAP_USERNAME = "imap_username"
CONF_IMAP_PASSWORD = "imap_password"
CONF_IMAP_FOLDER = "imap_folder"
CONF_ALLOWED_SENDERS = "allowed_senders"
CONF_CUPS_URL = "cups_url"
CONF_PRINTER_NAME = "printer_name"

# Options-flow keys (editable after setup via the integration's "Configure" button)
CONF_DUPLEX_MODE = "duplex_mode"
CONF_BOOKLET_PATTERNS = "booklet_patterns"
CONF_AUTO_DELETE = "auto_delete"
CONF_QUEUE_FOLDER = "queue_folder"
CONF_POLL_INTERVAL_MINUTES = "poll_interval_minutes"

# Defaults
DEFAULT_IMAP_PORT = 993
DEFAULT_IMAP_USE_SSL = True
DEFAULT_IMAP_FOLDER = "INBOX"
DEFAULT_CUPS_URL = "http://10.0.0.23:631"
DEFAULT_DUPLEX_MODE = "two-sided-long-edge"
DEFAULT_AUTO_DELETE = True
DEFAULT_QUEUE_FOLDER = "/media/print_queue"
DEFAULT_POLL_INTERVAL_MINUTES = 1

# Human-readable labels for duplex modes (used in config/options flow selectors)
DUPLEX_MODES: dict[str, str] = {
    "one-sided": "One-sided",
    "two-sided-long-edge": "Two-sided — portrait (long edge)",
    "two-sided-short-edge": "Two-sided — landscape (short edge)",
}

# Coordinator data key stored in hass.data
DATA_COORDINATOR = "coordinator"

# Service names
SERVICE_PRINT_FILE = "print_file"
SERVICE_CLEAR_QUEUE = "clear_queue"

# Service field names
FIELD_FILE_PATH = "file_path"
FIELD_DUPLEX = "duplex"
FIELD_BOOKLET = "booklet"

# Sensor / binary-sensor unique-id suffixes
SENSOR_QUEUE_DEPTH = "queue_depth"
SENSOR_LAST_JOB = "last_job"
BINARY_SENSOR_PRINTER_ONLINE = "printer_online"
BUTTON_TEST_PAGE = "test_page"

# Attribute names exposed on sensors
ATTR_LAST_STATUS = "last_status"
ATTR_LAST_FILENAME = "last_filename"
