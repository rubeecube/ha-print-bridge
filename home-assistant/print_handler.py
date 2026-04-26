"""Send a PDF file to a CUPS printer via a raw IPP/2.0 Print-Job request.

Usage:
    python3 print_handler.py <pdf_path> <duplex_state>

    pdf_path     — absolute path to the PDF file
    duplex_state — "on"  → two-sided-long-edge
                   "off" → one-sided

Environment variables (override compiled-in defaults):
    AUTO_PRINT_PRINTER_NAME   — CUPS queue name  (default: Canon_MG3600_series)
    AUTO_PRINT_CUPS_URL       — base URL for CUPS (default: http://localhost:631)
    AUTO_PRINT_BOOKLET_MARKER — substring that flags a file as a booklet job
                                (default: Programme)
    AUTO_PRINT_LOG_FILE       — path for the append-only log file (optional)
"""

import logging
import os
import struct
import subprocess
import sys
import traceback

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Runtime configuration — all overridable via environment variables.
# ---------------------------------------------------------------------------
PRINTER_NAME: str = os.environ.get("AUTO_PRINT_PRINTER_NAME", "Canon_MG3600_series")
CUPS_BASE_URL: str = os.environ.get("AUTO_PRINT_CUPS_URL", "http://localhost:631")
IPP_URL: str = f"{CUPS_BASE_URL.rstrip('/')}/printers/{PRINTER_NAME}"
BOOKLET_MARKER: str = os.environ.get("AUTO_PRINT_BOOKLET_MARKER", "Programme")
LOG_FILE: str | None = os.environ.get("AUTO_PRINT_LOG_FILE")


def _configure_logging() -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if LOG_FILE:
        handlers.append(logging.FileHandler(LOG_FILE))
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        handlers=handlers,
    )


# ---------------------------------------------------------------------------
# IPP packet construction
# ---------------------------------------------------------------------------

def _ipp_attr(tag: int, name: str, value: str) -> bytes:
    """Encode a single IPP attribute: tag + name-length + name + value-length + value."""
    name_bytes = name.encode()
    value_bytes = value.encode()
    return (
        struct.pack(">BH", tag, len(name_bytes))
        + name_bytes
        + struct.pack(">H", len(value_bytes))
        + value_bytes
    )


def _cups_printer_uri(cups_base_url: str, printer_name: str) -> str:
    base = cups_base_url.rstrip("/")
    if base.startswith("http://"):
        ipp_base = "ipp://" + base[len("http://"):]
    elif base.startswith("https://"):
        ipp_base = "ipps://" + base[len("https://"):]
    else:
        ipp_base = base
    return f"{ipp_base}/printers/{printer_name}"


def _ipp_status(response: bytes) -> tuple[bool, str]:
    if len(response) < 4:
        return False, "Invalid IPP response: shorter than 4 bytes"
    if response[0] not in (1, 2):
        return False, f"Invalid IPP response version: {response[0]}.{response[1]}"
    status_code = struct.unpack(">H", response[2:4])[0]
    return status_code < 0x0100, f"IPP 0x{status_code:04x}"


def build_ipp_packet(printer_uri: str, file_name: str, sides: str, pdf_data: bytes) -> bytes:
    """Construct a valid IPP 2.0 Print-Job request packet."""
    # 2-byte version (2.0) + 2-byte operation-id (0x0002 = Print-Job) + 4-byte request-id
    header = struct.pack(">HHI", 0x0200, 0x0002, 0x00000001)

    # Operation-attributes group (tag 0x01)
    header += b"\x01"
    header += _ipp_attr(0x47, "attributes-charset", "utf-8")
    header += _ipp_attr(0x48, "attributes-natural-language", "en")
    header += _ipp_attr(0x45, "printer-uri", printer_uri)
    header += _ipp_attr(0x42, "job-name", file_name)
    header += _ipp_attr(0x49, "document-format", "application/pdf")

    # Job-attributes group (tag 0x02)
    header += b"\x02"
    header += _ipp_attr(0x44, "sides", sides)

    # End-of-attributes tag (0x03)
    header += b"\x03"

    return header + pdf_data


def _determine_sides(duplex_input: str, is_booklet: bool) -> str:
    if is_booklet:
        return "two-sided-short-edge"
    return "two-sided-long-edge" if duplex_input == "on" else "one-sided"


# ---------------------------------------------------------------------------
# Main printing logic
# ---------------------------------------------------------------------------

def print_pdf(file_path: str, duplex_input: str) -> bool:
    """Download, optionally convert to booklet, and send to printer.

    Returns True on success, False on failure.
    """
    file_name = os.path.basename(file_path)
    logger.debug("Print attempt: %s  duplex=%s", file_name, duplex_input)

    if not os.path.exists(file_path):
        logger.error("File not found: %s", file_path)
        return False

    print_file = file_path
    is_booklet = BOOKLET_MARKER in file_name

    if is_booklet:
        logger.debug("Booklet mode detected for '%s'", file_name)
        try:
            result = subprocess.run(
                [sys.executable, "/config/booklet_maker.py", file_path],
                capture_output=True,
                text=True,
                check=True,
            )
            print_file = result.stdout.strip()
        except subprocess.CalledProcessError as exc:
            logger.error("Booklet conversion failed: %s", exc.stderr)
            return False

    sides = _determine_sides(duplex_input.lower(), is_booklet)
    logger.debug("Sending '%s' with sides=%s to %s", os.path.basename(print_file), sides, IPP_URL)

    try:
        with open(print_file, "rb") as f:
            pdf_data = f.read()

        packet = build_ipp_packet(
            _cups_printer_uri(CUPS_BASE_URL, PRINTER_NAME),
            file_name,
            sides,
            pdf_data,
        )

        response = requests.post(
            IPP_URL,
            data=packet,
            headers={"Content-Type": "application/ipp"},
            timeout=30,
        )

        body_prefix = response.content[:256].lstrip().lower()
        ipp_ok, ipp_status = _ipp_status(response.content)
        if (
            response.status_code == 200
            and not body_prefix.startswith((b"<!doctype html", b"<html"))
            and ipp_ok
        ):
            logger.debug("Job accepted by CUPS for '%s'", file_name)
            return True

        logger.error(
            "CUPS rejected job for '%s': HTTP %d %s",
            file_name, response.status_code, ipp_status,
        )
        return False

    except requests.RequestException:
        logger.exception("Network error sending job to CUPS for '%s'", file_name)
        return False
    finally:
        if print_file != file_path and os.path.exists(print_file):
            os.remove(print_file)


def main() -> None:
    _configure_logging()
    logger.debug("=" * 40)
    if len(sys.argv) < 3:
        logger.error("Usage: print_handler.py <path> <duplex_state>")
        sys.exit(1)

    success = print_pdf(sys.argv[1], sys.argv[2])
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
