"""Constants for the Print Bridge integration."""

DOMAIN = "print_bridge"

# ---------------------------------------------------------------------------
# Config-entry keys — set once during initial setup (printer only)
# ---------------------------------------------------------------------------
CONF_CUPS_URL = "cups_url"
CONF_PRINTER_NAME = "printer_name"

# Optional: direct IPP URL (bypasses CUPS entirely).
# When set, the component sends Print-Job straight to the printer.
# Examples:
#   http://10.0.0.23/ipp/print         (AirPrint, port 80)
#   http://10.0.0.23:631/ipp/print     (CUPS-port on printer)
#   ipp://10.0.0.23/ipp/print          (native IPP scheme)
CONF_DIRECT_PRINTER_URL = "direct_printer_url"

# ---------------------------------------------------------------------------
# Options-flow keys — editable after setup via "Configure"
# ---------------------------------------------------------------------------
CONF_ALLOWED_SENDERS = "allowed_senders"          # list[str]; empty = accept all
CONF_FOLDER_FILTER = "folder_filter"               # list[str]; empty = accept all folders
CONF_DUPLEX_MODE = "duplex_mode"
CONF_BOOKLET_PATTERNS = "booklet_patterns"         # list[str]
CONF_AUTO_DELETE = "auto_delete"
CONF_QUEUE_FOLDER = "queue_folder"
CONF_EMAIL_ACTION = "email_action_after_print"     # what to do with the email after printing
CONF_EMAIL_ARCHIVE_FOLDER = "email_archive_folder" # target folder when action = "move"
CONF_NOTIFY_ON_FAILURE = "notify_on_failure"       # send HA notification when print fails
CONF_NOTIFY_ON_SUCCESS = "notify_on_success"       # send HA notification when print succeeds
CONF_SCHEDULE_ENABLED = "schedule_enabled"         # enable print time window
CONF_SCHEDULE_START = "schedule_start"             # "HH:MM" — start of allowed window
CONF_SCHEDULE_END = "schedule_end"                 # "HH:MM" — end of allowed window

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_CUPS_URL = "http://10.0.0.23:631"
DEFAULT_DUPLEX_MODE = "two-sided-long-edge"
DEFAULT_AUTO_DELETE = True
DEFAULT_QUEUE_FOLDER = "/media/print_queue"
DEFAULT_EMAIL_ACTION = "none"
DEFAULT_EMAIL_ARCHIVE_FOLDER = "INBOX/Printed"
DEFAULT_NOTIFY_ON_FAILURE = True
DEFAULT_NOTIFY_ON_SUCCESS = False
DEFAULT_SCHEDULE_ENABLED = False
DEFAULT_SCHEDULE_START = "07:00"
DEFAULT_SCHEDULE_END = "22:00"

# Choices for the email_action_after_print option
EMAIL_ACTIONS: dict[str, str] = {
    "none": "Do nothing",
    "mark_seen": "Mark as read",
    "move": "Move to archive folder",
    "delete": "Delete from server",
}

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
SERVICE_CHECK_FILTER = "check_filter"
SERVICE_RETRY_JOB = "retry_job"

FIELD_FILE_PATH = "file_path"
FIELD_DUPLEX = "duplex"
FIELD_BOOKLET = "booklet"

# ---------------------------------------------------------------------------
# Entity unique-id suffixes
# ---------------------------------------------------------------------------
SENSOR_QUEUE_DEPTH = "queue_depth"
SENSOR_LAST_JOB = "last_job"
SENSOR_JOB_LOG = "job_log"
SENSOR_FILTER_PREVIEW = "filter_preview"
SENSOR_PENDING_JOBS = "pending_jobs"
BINARY_SENSOR_PRINTER_ONLINE = "printer_online"
BUTTON_TEST_PAGE = "test_page"
BUTTON_CHECK_FILTER = "check_filter"
BUTTON_RETRY_LAST_FAILED = "retry_last_failed"
BUTTON_FLUSH_PENDING = "flush_pending"

# Event fired after each print job — appears in HA Logbook
EVENT_JOB_COMPLETED = "print_bridge_job_completed"

# ---------------------------------------------------------------------------
# State attribute names
# ---------------------------------------------------------------------------
ATTR_LAST_STATUS = "last_status"
ATTR_LAST_FILENAME = "last_filename"
