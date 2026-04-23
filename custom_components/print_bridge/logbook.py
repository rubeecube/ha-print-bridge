"""Logbook integration for Print Bridge.

Registers a human-readable descriptor for print_bridge_job_completed events
so that every print job appears in the HA Logbook as a legible sentence
rather than a raw event payload.

Example Logbook entries:
  Print Bridge | Printed 'invoice.pdf' · two-sided · from billing@example.com · Canon_MG3600_series
  Print Bridge | Print failed for 'bad.pdf': HTTP 503 · from sender@example.com
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN, EVENT_JOB_COMPLETED


def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: ...,
) -> None:
    """Register descriptors for Print Bridge events in the HA Logbook."""

    @callback
    def _describe_job_completed(event) -> dict[str, str]:
        data = event.data
        filename: str = data.get("filename") or "unknown"
        success: bool = bool(data.get("success"))
        error: str = data.get("error") or ""
        sender: str = data.get("sender") or ""
        duplex: str = data.get("duplex") or ""
        booklet: bool = bool(data.get("booklet"))
        printer: str = data.get("printer") or ""

        if success:
            mode = "booklet" if booklet else (duplex or "")
            parts = [f"Printed '{filename}'"]
            if mode:
                parts.append(mode)
        else:
            parts = [f"Print failed for '{filename}'"]
            if error:
                parts[0] += f": {error}"

        if sender:
            parts.append(f"from {sender}")
        if printer:
            parts.append(printer)

        return {
            "name": "Print Bridge",
            "message": "  ·  ".join(parts),
        }

    async_describe_event(DOMAIN, EVENT_JOB_COMPLETED, _describe_job_completed)
