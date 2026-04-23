"""Config flow and options flow for the Auto Print integration.

On first launch the flow auto-discovers CUPS at common addresses and reads any
IMAP integration entries already configured in HA, so that the user can confirm
or override pre-populated values rather than typing everything from scratch.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_ALLOWED_SENDERS,
    CONF_AUTO_DELETE,
    CONF_BOOKLET_PATTERNS,
    CONF_CUPS_URL,
    CONF_DUPLEX_MODE,
    CONF_FOLDER_FILTER,
    CONF_PRINTER_NAME,
    CONF_QUEUE_FOLDER,
    DEFAULT_AUTO_DELETE,
    DEFAULT_CUPS_URL,
    DEFAULT_DUPLEX_MODE,
    DEFAULT_QUEUE_FOLDER,
    DOMAIN,
    DUPLEX_MODES,
)

logger = logging.getLogger(__name__)

# Sentinel option values used in select fields
_SENTINEL_MANUAL = "__manual__"    # user wants to type the printer name
_SENTINEL_SKIP_IMAP = "__skip__"  # user skips IMAP pre-fill

# CUPS addresses probed during auto-discovery
_CUPS_PROBE_URLS = [
    "http://localhost:631",
    "http://homeassistant.local:631",
]


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------

async def _probe_cups(
    session: aiohttp.ClientSession, url: str
) -> tuple[bool, list[str]]:
    """Return (reachable, printer_names) for a CUPS server at *url*.

    Printer queue names are extracted from the /printers/ HTML page.
    A 403 response means CUPS is running but requires auth — we mark it
    as reachable with an empty printer list.
    """
    base = url.rstrip("/")
    try:
        async with session.get(
            f"{base}/printers/",
            timeout=aiohttp.ClientTimeout(total=4),
            headers={"Accept": "text/html"},
        ) as resp:
            if resp.status == 403:
                return True, []
            if resp.status != 200:
                return False, []
            html = await resp.text(errors="replace")
            # CUPS renders printer links as: href="/printers/<queue-name>"
            names = list(dict.fromkeys(
                re.findall(r'href=["\']\/printers\/([^"\'/?#\s]+)', html)
            ))
            return True, names
    except (aiohttp.ClientError, OSError):
        return False, []


async def _discover_cups(
    session: aiohttp.ClientSession,
) -> tuple[str | None, list[str]]:
    """Probe known CUPS locations. Return (url, printer_names) for the first hit."""
    for url in _CUPS_PROBE_URLS:
        reachable, printers = await _probe_cups(session, url)
        if reachable:
            return url, printers
    return None, []


def _imap_choices(entries: list[ConfigEntry]) -> dict[str, str]:
    """Build a {entry_id: display_label} map for IMAP config entries."""
    choices: dict[str, str] = {_SENTINEL_SKIP_IMAP: "Skip — configure senders later"}
    for entry in entries:
        username = entry.data.get("username", entry.title)
        server = entry.data.get("server", "")
        label = f"{username}  ({server})" if server else username
        choices[entry.entry_id] = label
    return choices


def _email_from_imap_entry(
    entry_id: str, entries: list[ConfigEntry]
) -> str | None:
    """Return the username of the IMAP entry with *entry_id*, or None if skipped."""
    if entry_id in (_SENTINEL_SKIP_IMAP, "", None):
        return None
    for entry in entries:
        if entry.entry_id == entry_id:
            return entry.data.get("username") or None
    return None


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------

class AutoPrintConfigFlow(ConfigFlow, domain=DOMAIN):
    """Setup wizard with auto-discovery for CUPS and IMAP pre-fill."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered_cups_url: str | None = None
        self._discovered_printers: list[str] = []
        self._imap_entries: list[ConfigEntry] = []
        self._discovery_done: bool = False
        self._pending_cups_url: str = ""

    # ------------------------------------------------------------------
    # Step 1 — main form with auto-discovery
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show CUPS + optional IMAP pre-fill form, pre-populated from discovery."""
        errors: dict[str, str] = {}

        # Auto-discover once on first render (user_input is None).
        if not self._discovery_done:
            session = async_get_clientsession(self.hass)
            self._discovered_cups_url, self._discovered_printers = (
                await _discover_cups(session)
            )
            self._imap_entries = list(self.hass.config_entries.async_entries("imap"))
            self._discovery_done = True

        if user_input is not None:
            cups_url = user_input[CONF_CUPS_URL].rstrip("/")
            printer_raw: str = user_input[CONF_PRINTER_NAME]
            imap_sel: str = user_input.get("imap_account", _SENTINEL_SKIP_IMAP)

            # User chose to type the printer name → go to sub-step.
            if printer_raw == _SENTINEL_MANUAL:
                self._pending_cups_url = cups_url
                return await self.async_step_manual_printer()

            printer_name = printer_raw.strip()

            await self._validate_cups(cups_url, errors)

            if not errors:
                return await self._create(cups_url, printer_name, imap_sel)

        return self.async_show_form(
            step_id="user",
            data_schema=_build_user_schema(
                self._discovered_cups_url,
                self._discovered_printers,
                self._imap_entries,
            ),
            errors=errors,
            description_placeholders=_build_placeholders(
                self._discovered_cups_url,
                self._discovered_printers,
                self._imap_entries,
            ),
        )

    # ------------------------------------------------------------------
    # Step 2 (conditional) — free-text printer name entry
    # ------------------------------------------------------------------

    async def async_step_manual_printer(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect a free-text printer name when the user chose 'Enter manually'."""
        errors: dict[str, str] = {}

        if user_input is not None:
            printer_name = user_input[CONF_PRINTER_NAME].strip()
            cups_url = self._pending_cups_url

            await self._validate_cups(cups_url, errors)

            if not errors:
                return await self._create(cups_url, printer_name, _SENTINEL_SKIP_IMAP)

        schema = vol.Schema({vol.Required(CONF_PRINTER_NAME): str})
        return self.async_show_form(
            step_id="manual_printer",
            data_schema=schema,
            errors=errors,
            description_placeholders={"cups_url": self._pending_cups_url},
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _validate_cups(self, cups_url: str, errors: dict[str, str]) -> None:
        """HEAD-check the CUPS URL and populate *errors* on failure."""
        try:
            session = async_get_clientsession(self.hass)
            async with session.head(
                cups_url, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status >= 500:
                    errors["base"] = "cannot_connect_cups"
        except (aiohttp.ClientError, OSError):
            errors["base"] = "cannot_connect_cups"
        except Exception:
            errors["base"] = "unknown"

    async def _create(
        self, cups_url: str, printer_name: str, imap_sel: str
    ) -> ConfigFlowResult:
        """Set unique_id, build initial options and create the config entry."""
        initial_options: dict[str, Any] = {}
        email = _email_from_imap_entry(imap_sel, self._imap_entries)
        if email:
            initial_options[CONF_ALLOWED_SENDERS] = [email]

        await self.async_set_unique_id(f"{cups_url}/{printer_name}")
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=f"Auto Print — {printer_name}",
            data={CONF_CUPS_URL: cups_url, CONF_PRINTER_NAME: printer_name},
            options=initial_options,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return AutoPrintOptionsFlow(config_entry)


# ---------------------------------------------------------------------------
# Options flow
# ---------------------------------------------------------------------------

class AutoPrintOptionsFlow(OptionsFlow):
    """Editable options. Offers IMAP entry selection to add senders quickly."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        options = self._config_entry.options
        imap_entries = list(self.hass.config_entries.async_entries("imap"))

        if user_input is not None:
            # Optional: append the username from a selected IMAP entry.
            imap_sel = user_input.pop("imap_account", _SENTINEL_SKIP_IMAP)
            raw_senders: str = user_input[CONF_ALLOWED_SENDERS]
            extra = _email_from_imap_entry(imap_sel, imap_entries)
            if extra and extra.lower() not in raw_senders.lower():
                raw_senders = f"{raw_senders}\n{extra}".strip()

            patterns = [
                p.strip()
                for p in user_input[CONF_BOOKLET_PATTERNS].splitlines()
                if p.strip()
            ]
            senders = [
                s.strip().lower()
                for s in raw_senders.splitlines()
                if s.strip()
            ]
            folders = [
                f.strip()
                for f in user_input.get(CONF_FOLDER_FILTER, "").splitlines()
                if f.strip()
            ]
            return self.async_create_entry(
                title="",
                data={
                    **user_input,
                    CONF_BOOKLET_PATTERNS: patterns,
                    CONF_ALLOWED_SENDERS: senders,
                    CONF_FOLDER_FILTER: folders,
                },
            )

        current_patterns = options.get(CONF_BOOKLET_PATTERNS, [])
        current_senders = options.get(CONF_ALLOWED_SENDERS, [])
        current_folders = options.get(CONF_FOLDER_FILTER, [])

        # Build a hint about which folders the configured IMAP entries monitor.
        imap_folder_hint = ", ".join(
            f"{e.data.get('folder', 'INBOX')} ({e.data.get('username', e.title)})"
            for e in imap_entries
        ) or "none configured yet"

        schema_dict: dict = {
            vol.Required(
                CONF_ALLOWED_SENDERS, default="\n".join(current_senders)
            ): str,
            vol.Required(
                CONF_FOLDER_FILTER, default="\n".join(current_folders)
            ): str,
            vol.Required(
                CONF_DUPLEX_MODE,
                default=options.get(CONF_DUPLEX_MODE, DEFAULT_DUPLEX_MODE),
            ): vol.In(DUPLEX_MODES),
            vol.Required(
                CONF_BOOKLET_PATTERNS, default="\n".join(current_patterns)
            ): str,
            vol.Required(
                CONF_AUTO_DELETE,
                default=options.get(CONF_AUTO_DELETE, DEFAULT_AUTO_DELETE),
            ): bool,
            vol.Required(
                CONF_QUEUE_FOLDER,
                default=options.get(CONF_QUEUE_FOLDER, DEFAULT_QUEUE_FOLDER),
            ): str,
        }

        # Only show the IMAP shortcut if accounts are configured.
        if imap_entries:
            schema_dict[
                vol.Optional("imap_account", default=_SENTINEL_SKIP_IMAP)
            ] = vol.In(_imap_choices(imap_entries))

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={"imap_folders": imap_folder_hint},
        )


# ---------------------------------------------------------------------------
# Schema / placeholder builders
# ---------------------------------------------------------------------------

def _build_user_schema(
    cups_url: str | None,
    printers: list[str],
    imap_entries: list[ConfigEntry],
) -> vol.Schema:
    schema: dict = {
        vol.Required(CONF_CUPS_URL, default=cups_url or DEFAULT_CUPS_URL): str,
    }

    if printers:
        printer_options = {p: p for p in printers}
        printer_options[_SENTINEL_MANUAL] = "Enter name manually…"
        schema[vol.Required(CONF_PRINTER_NAME, default=printers[0])] = vol.In(
            printer_options
        )
    else:
        schema[vol.Required(CONF_PRINTER_NAME)] = str

    if imap_entries:
        schema[
            vol.Optional("imap_account", default=_SENTINEL_SKIP_IMAP)
        ] = vol.In(_imap_choices(imap_entries))

    return vol.Schema(schema)


def _build_placeholders(
    cups_url: str | None,
    printers: list[str],
    imap_entries: list[ConfigEntry],
) -> dict[str, str]:
    if printers:
        cups_info = (
            f"Found {len(printers)} printer(s) at {cups_url}: "
            + ", ".join(printers)
        )
    elif cups_url:
        cups_info = f"CUPS is reachable at {cups_url} but has no printer queues yet."
    else:
        cups_info = (
            "No CUPS server found at common addresses. "
            "Enter the URL manually (e.g. http://10.0.0.23:631)."
        )

    if imap_entries:
        names = ", ".join(e.data.get("username", e.title) for e in imap_entries)
        imap_info = f"Found {len(imap_entries)} IMAP account(s): {names}"
    else:
        imap_info = (
            "No IMAP accounts configured in HA yet. "
            "Add the IMAP integration first so Auto Print can pre-fill allowed senders."
        )

    return {"cups_info": cups_info, "imap_info": imap_info}
