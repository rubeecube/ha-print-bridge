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
#   http://printer.local/ipp/print       (AirPrint, port 80)
#   http://printer.local:631/ipp/print   (CUPS-port on printer)
#   ipp://printer.local/ipp/print        (native IPP scheme)
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
CONF_STATUS_REPLY_ENABLED = "status_reply_enabled"  # reply to sender with print status
CONF_STATUS_REPLY_NOTIFY_SERVICE = "status_reply_notify_service"
CONF_SCHEDULE_ENABLED = "schedule_enabled"         # enable print time window
CONF_SCHEDULE_START = "schedule_start"             # "HH:MM" — start of allowed window
CONF_SCHEDULE_END = "schedule_end"                 # "HH:MM" — end of allowed window
CONF_SCHEDULE_DAYS = "schedule_days"               # list[str]; empty = every day
CONF_SCHEDULE_TEMPLATE = "schedule_template"       # HA template; truthy = allow printing
CONF_AUTO_PRINT_ENABLED = "auto_print_enabled"     # automatically print on imap_content event
CONF_SELECTED_IMAP_ENTRY_ID = "selected_imap_entry_id"
CONF_SELECTED_PRINTER_ENTRY_ID = "selected_printer_entry_id"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_CUPS_URL = ""  # filled from discovery; user types if not found
DEFAULT_DUPLEX_MODE = "two-sided-long-edge"
DEFAULT_AUTO_DELETE = True
DEFAULT_QUEUE_FOLDER = "/media/print_queue"
DEFAULT_EMAIL_ACTION = "none"
DEFAULT_EMAIL_ARCHIVE_FOLDER = "INBOX/Printed"
DEFAULT_NOTIFY_ON_FAILURE = True
DEFAULT_NOTIFY_ON_SUCCESS = False
DEFAULT_STATUS_REPLY_ENABLED = False
DEFAULT_STATUS_REPLY_NOTIFY_SERVICE = ""
DEFAULT_SCHEDULE_ENABLED = False
DEFAULT_AUTO_PRINT_ENABLED = True  # enabled by default; set False on first install
DEFAULT_SCHEDULE_START = "07:00"
DEFAULT_SCHEDULE_END = "22:00"
DEFAULT_SCHEDULE_DAYS: tuple[str, ...] = ()
DEFAULT_SCHEDULE_TEMPLATE = ""

SCHEDULE_DAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

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
SERVICE_CHECK_PRINTER_CAPABILITIES = "check_printer_capabilities"
SERVICE_RETRY_JOB = "retry_job"
SERVICE_PRINT_EMAIL = "print_email"

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
SENSOR_PRINTER_CAPABILITIES = "printer_capabilities"
SENSOR_PENDING_JOBS = "pending_jobs"
BINARY_SENSOR_PRINTER_ONLINE = "printer_online"
SELECT_IMAP_ACCOUNT = "imap_account"
SELECT_TARGET_PRINTER = "target_printer"
SELECT_DUPLEX_MODE = "default_duplex_mode"
SELECT_EMAIL_ACTION = "email_action_after_print"
SWITCH_AUTO_PRINT_ENABLED = "auto_print"
SWITCH_AUTO_DELETE = "delete_after_printing"
SWITCH_NOTIFY_ON_FAILURE = "notify_on_failure"
SWITCH_NOTIFY_ON_SUCCESS = "notify_on_success"
SWITCH_STATUS_REPLY_ENABLED = "status_reply_enabled"
SWITCH_SCHEDULE_ENABLED = "print_schedule"
TEXT_ALLOWED_SENDERS = "allowed_senders"
TEXT_FOLDER_FILTER = "folder_filter"
TEXT_BOOKLET_PATTERNS = "booklet_patterns"
TEXT_QUEUE_FOLDER = "queue_folder"
TEXT_EMAIL_ARCHIVE_FOLDER = "email_archive_folder"
TEXT_STATUS_REPLY_NOTIFY_SERVICE = "status_reply_notify_service"
TEXT_SCHEDULE_START = "schedule_start"
TEXT_SCHEDULE_END = "schedule_end"
TEXT_SCHEDULE_DAYS = "schedule_days"
TEXT_SCHEDULE_TEMPLATE = "schedule_template"
BUTTON_TEST_PAGE = "test_page"
BUTTON_CHECK_FILTER = "check_filter"
BUTTON_CHECK_PRINTER_CAPABILITIES = "check_printer_capabilities"
BUTTON_RETRY_LAST_FAILED = "retry_last_failed"
BUTTON_FLUSH_PENDING = "flush_pending"
BUTTON_CANCEL_QUEUED_JOBS = "cancel_queued_jobs"
BUTTON_PRINT_EMAIL_PREFIX = "print_email"
BUTTON_PRINT_EMAIL_SLOTS = 5

# Event fired after each print job — appears in HA Logbook
EVENT_JOB_COMPLETED = "print_bridge_job_completed"

# ---------------------------------------------------------------------------
# State attribute names
# ---------------------------------------------------------------------------
ATTR_LAST_STATUS = "last_status"
ATTR_LAST_FILENAME = "last_filename"
