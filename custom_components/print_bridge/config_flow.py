"""Config flow and options flow for the Print Bridge integration.

Discovery runs server-side on the HA host using mDNS/Zeroconf, so it finds
LAN printers that broadcast via Bonjour/AirPrint (_ipp._tcp, _ipps._tcp).
CUPS on the HA host is also probed.  All fields are optional — the user can
supply either a Direct IPP URL or a CUPS URL + queue name.
"""

from __future__ import annotations

import asyncio
import logging
import re as _re
import socket
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import TemplateError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.template import Template

from .const import (
    CONF_ALLOWED_SENDERS,
    CONF_AUTO_DELETE,
    CONF_BOOKLET_PATTERNS,
    CONF_CUPS_URL,
    CONF_DIRECT_PRINTER_URL,
    CONF_DUPLEX_MODE,
    CONF_EMAIL_ACTION,
    CONF_EMAIL_ARCHIVE_FOLDER,
    CONF_FOLDER_FILTER,
    CONF_NOTIFY_ON_FAILURE,
    CONF_NOTIFY_ON_SUCCESS,
    CONF_PRINTER_NAME,
    CONF_QUEUE_FOLDER,
    CONF_AUTO_PRINT_ENABLED,
    CONF_SCHEDULE_ENABLED,
    CONF_SCHEDULE_DAYS,
    CONF_SCHEDULE_END,
    CONF_SCHEDULE_START,
    CONF_SCHEDULE_TEMPLATE,
    CONF_STATUS_REPLY_ENABLED,
    CONF_STATUS_REPLY_NOTIFY_SERVICE,
    DEFAULT_AUTO_DELETE,
    DEFAULT_CUPS_URL,
    DEFAULT_DUPLEX_MODE,
    DEFAULT_EMAIL_ACTION,
    DEFAULT_EMAIL_ARCHIVE_FOLDER,
    DEFAULT_NOTIFY_ON_FAILURE,
    DEFAULT_NOTIFY_ON_SUCCESS,
    DEFAULT_QUEUE_FOLDER,
    DEFAULT_AUTO_PRINT_ENABLED,
    DEFAULT_SCHEDULE_ENABLED,
    DEFAULT_SCHEDULE_DAYS,
    DEFAULT_SCHEDULE_END,
    DEFAULT_SCHEDULE_START,
    DEFAULT_SCHEDULE_TEMPLATE,
    DEFAULT_STATUS_REPLY_ENABLED,
    DEFAULT_STATUS_REPLY_NOTIFY_SERVICE,
    DOMAIN,
    DUPLEX_MODES,
    EMAIL_ACTIONS,
    SCHEDULE_DAYS,
)
from .print_handler import http_url_to_ipp_uri

_HHMM_PATTERN = _re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_SCHEDULE_DAY_ALIASES = {
    "mon": "mon",
    "monday": "mon",
    "1": "mon",
    "tue": "tue",
    "tues": "tue",
    "tuesday": "tue",
    "2": "tue",
    "wed": "wed",
    "wednesday": "wed",
    "3": "wed",
    "thu": "thu",
    "thur": "thu",
    "thurs": "thu",
    "thursday": "thu",
    "4": "thu",
    "fri": "fri",
    "friday": "fri",
    "5": "fri",
    "sat": "sat",
    "saturday": "sat",
    "6": "sat",
    "sun": "sun",
    "sunday": "sun",
    "7": "sun",
}

logger = logging.getLogger(__name__)


def _parse_schedule_days(value: Any) -> list[str] | None:
    """Parse user-entered weekday names into canonical tokens."""
    if value is None:
        return []
    if isinstance(value, str):
        raw_days = [
            part.strip().lower()
            for part in _re.split(r"[\s,;]+", value)
            if part.strip()
        ]
    elif isinstance(value, (list, tuple, set)):
        raw_days = [str(day).strip().lower() for day in value if str(day).strip()]
    else:
        return None

    days: list[str] = []
    for raw_day in raw_days:
        day = _SCHEDULE_DAY_ALIASES.get(raw_day)
        if day is None:
            return None
        if day not in days:
            days.append(day)
    return days


def _schedule_days_to_text(value: Any) -> str:
    """Format stored weekday tokens for the options form."""
    days = _parse_schedule_days(value)
    if days is None:
        return ""
    return "\n".join(day for day in SCHEDULE_DAYS if day in days)


# Sentinel option values used in select fields
_SENTINEL_MANUAL = "__manual__"    # user wants to type the printer name
_SENTINEL_SKIP_IMAP = "__skip__"  # user skips IMAP pre-fill

# CUPS addresses probed during auto-discovery (HA host only)
_CUPS_PROBE_URLS = [
    "http://localhost:631",
    "http://homeassistant.local:631",
]

# mDNS service types for IPP/AirPrint printers.
# NOTE: _printer._tcp.local. is the LPD/LPR protocol (port 515) — NOT IPP. Excluded intentionally.
_IPP_SERVICE_TYPES = [
    "_ipp._tcp.local.",
    "_ipps._tcp.local.",
]

# How long to wait for mDNS responses (seconds).
# Most printers respond within 1-2 s; allow extra time for slow mDNS stacks.
_MDNS_TIMEOUT = 5


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
            names = list(dict.fromkeys(
                _re.findall(r'href=["\']\/printers\/([^"\'/?#\s]+)', html)
            ))
            return True, names
    except (aiohttp.ClientError, OSError):
        return False, []


async def _discover_cups(
    session: aiohttp.ClientSession,
) -> tuple[str | None, list[str]]:
    """Probe known localhost CUPS locations. Return (url, printer_names) for the first hit."""
    for url in _CUPS_PROBE_URLS:
        reachable, printers = await _probe_cups(session, url)
        if reachable:
            return url, printers
    return None, []


async def _discover_printers_mdns(hass: HomeAssistant) -> list[dict[str, str]]:
    """Discover IPP/AirPrint printers via mDNS from the HA host.

    Uses a sync ServiceBrowser running in HA's executor thread pool — more
    reliable than AsyncServiceBrowser because it avoids event-loop scheduling
    issues with HaZeroconf.  Browses _ipp._tcp.local., _ipps._tcp.local.,
    _printer._tcp.local. and resolves each service record to a URL inside
    the add_service callback (while the browser is still live).

    The printer MUST be turned on and advertising mDNS for discovery to work.

    Returns list of {name, url} dicts with no hardcoded addresses.
    """
    import threading

    found: dict[str, dict[str, str]] = {}
    lock = threading.Lock()

    def _resolve_and_add(zc_inst: Any, type_: str, name: str) -> None:
        """Resolve one discovered service and add it to *found* (thread-safe)."""
        from zeroconf import ServiceInfo
        info: ServiceInfo | None = zc_inst.get_service_info(type_, name, timeout=2000)
        if not info or not info.addresses:
            return
        host: str | None = None
        for addr in info.addresses:
            try:
                host = socket.inet_ntoa(addr)   # IPv4
                break
            except OSError:
                pass
        if host is None:
            return
        port: int = info.port or 631
        rp_raw = info.properties.get(b"rp", b"ipp/print")
        rp: str = (
            rp_raw.decode("utf-8", errors="replace")
            if isinstance(rp_raw, bytes)
            else "ipp/print"
        )

        # Skip CUPS-specific virtual paths that won't work for direct IPP.
        _BAD_PATHS = {"ipp/auto", "auto"}
        if rp.lower().strip("/") in _BAD_PATHS:
            logger.debug("Skipping mDNS printer %s — path '%s' is a CUPS virtual queue", host, rp)
            return
        if rp.lower().startswith("printers/"):
            # CUPS queue path; use standard AirPrint path instead.
            rp = "ipp/print"

        # For IPPS (_ipps._tcp.local.) use HTTP on port 80 — home printers have self-signed certs
        # that fail SSL verification, but the same printer accepts plain IPP on port 80.
        if "ipps" in type_:
            url = f"http://{host}/ipp/print"
        else:
            url = f"http://{host}:{port}/{rp.lstrip('/')}"
        display = name.rstrip(".").split(".")[0]
        with lock:
            if url not in found:
                found[url] = {"name": display, "url": url}

    def _sync_browse(zc: Any) -> None:
        """Browse for IPP services in a thread-pool worker.

        Using sync ServiceBrowser avoids event-loop scheduling issues that
        AsyncServiceBrowser can have with HaZeroconf.
        """
        import time
        from zeroconf import ServiceBrowser

        class _Listener:
            def add_service(self, zc_inst: Any, type_: str, name: str) -> None:
                # Resolve immediately while the browser is live.
                _resolve_and_add(zc_inst, type_, name)

            def remove_service(self, *_: Any) -> None:
                pass

            def update_service(self, *_: Any) -> None:
                pass

        listener = _Listener()
        browsers = [ServiceBrowser(zc, stype, listener) for stype in _IPP_SERVICE_TYPES]
        time.sleep(_MDNS_TIMEOUT)
        for b in browsers:
            b.cancel()

    try:
        from homeassistant.components.zeroconf import async_get_async_instance
        aiozc = await async_get_async_instance(hass)
        await hass.async_add_executor_job(_sync_browse, aiozc.zeroconf)
    except Exception:
        logger.debug("mDNS printer discovery failed", exc_info=True)

    return list(found.values())


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
    """Setup wizard with mDNS + CUPS auto-discovery and optional IMAP pre-fill."""

    VERSION = 1

    def __init__(self) -> None:
        # CUPS discovery (localhost only)
        self._discovered_cups_url: str | None = None
        self._discovered_printers: list[str] = []
        # mDNS-discovered direct IPP printers (LAN)
        self._discovered_mdns: list[dict[str, str]] = []
        self._imap_entries: list[ConfigEntry] = []
        self._discovery_done: bool = False
        self._pending_cups_url: str = ""
        self._pending_imap_sel: str = _SENTINEL_SKIP_IMAP

    # ------------------------------------------------------------------
    # Step 1 — main form with auto-discovery
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show setup form, pre-populated from mDNS + CUPS discovery."""
        errors: dict[str, str] = {}

        # Run discovery on first render OR when the user asks to rescan.
        rescan_requested = (
            user_input is not None and user_input.pop("rescan", False)
        )
        if not self._discovery_done or rescan_requested:
            session = async_get_clientsession(self.hass)
            # Run CUPS probe and mDNS scan concurrently.
            cups_task = asyncio.create_task(_discover_cups(session))
            mdns_task = asyncio.create_task(_discover_printers_mdns(self.hass))
            (
                (self._discovered_cups_url, self._discovered_printers),
                self._discovered_mdns,
            ) = await asyncio.gather(cups_task, mdns_task)
            self._imap_entries = list(self.hass.config_entries.async_entries("imap"))
            self._discovery_done = True

            if rescan_requested:
                # Re-render the form with fresh results; don't validate.
                return self.async_show_form(
                    step_id="user",
                    data_schema=_build_user_schema(
                        self._discovered_cups_url,
                        self._discovered_printers,
                        self._discovered_mdns,
                        self._imap_entries,
                    ),
                    errors={},
                    description_placeholders=_build_placeholders(
                        self._discovered_cups_url,
                        self._discovered_printers,
                        self._discovered_mdns,
                        self._imap_entries,
                    ),
                )

        if user_input is not None:
            cups_url = (user_input.get(CONF_CUPS_URL) or "").strip().rstrip("/")
            printer_raw: str = (user_input.get(CONF_PRINTER_NAME) or "").strip()
            direct_url: str = (user_input.get(CONF_DIRECT_PRINTER_URL) or "").strip()
            imap_sel: str = user_input.get("imap_account", _SENTINEL_SKIP_IMAP)

            # Direct IPP URL takes precedence over CUPS setup.
            if direct_url:
                check_url = direct_url
                if check_url.startswith("ipp://"):
                    check_url = "http://" + check_url[len("ipp://"):]
                elif check_url.startswith("ipps://"):
                    check_url = "https://" + check_url[len("ipps://"):]
                try:
                    # verify_ssl=False: home printers use self-signed/no certificates
                    session = async_get_clientsession(self.hass, verify_ssl=False)
                    async with session.head(
                        check_url, timeout=aiohttp.ClientTimeout(total=5)
                    ) as resp:
                        if resp.status >= 500:
                            errors[CONF_DIRECT_PRINTER_URL] = "cannot_connect_cups"
                except (aiohttp.ClientError, OSError):
                    errors[CONF_DIRECT_PRINTER_URL] = "cannot_connect_cups"
                except Exception:
                    errors[CONF_DIRECT_PRINTER_URL] = "unknown"

                if not errors:
                    return await self._create_direct(direct_url, imap_sel)

            elif cups_url and printer_raw:
                # CUPS mode: user provided both URL and printer name.
                if printer_raw == _SENTINEL_MANUAL:
                    self._pending_cups_url = cups_url
                    self._pending_imap_sel = imap_sel
                    return await self.async_step_manual_printer()

                await self._validate_cups(cups_url, errors)
                if not errors:
                    return await self._create(cups_url, printer_raw, imap_sel)

            else:
                # Neither direct URL nor CUPS+name provided.
                errors["base"] = "cups_or_direct_required"

        return self.async_show_form(
            step_id="user",
            data_schema=_build_user_schema(
                self._discovered_cups_url,
                self._discovered_printers,
                self._discovered_mdns,
                self._imap_entries,
            ),
            errors=errors,
            description_placeholders=_build_placeholders(
                self._discovered_cups_url,
                self._discovered_printers,
                self._discovered_mdns,
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
                return await self._create(cups_url, printer_name, self._pending_imap_sel)

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
        if not cups_url:
            errors["base"] = "cups_or_direct_required"
            return
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

    async def _create_direct(
        self, direct_url: str, imap_sel: str
    ) -> ConfigFlowResult:
        """Create a config entry for direct IPP mode (no CUPS)."""
        initial_options: dict[str, Any] = {CONF_AUTO_PRINT_ENABLED: False}  # disabled until user configures
        email = _email_from_imap_entry(imap_sel, self._imap_entries)
        if email:
            initial_options[CONF_ALLOWED_SENDERS] = [email]

        await self.async_set_unique_id(f"direct/{direct_url}")
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title="Print Bridge — Direct Printer",
            data={CONF_DIRECT_PRINTER_URL: direct_url},
            options=initial_options,
        )

    async def _create(
        self, cups_url: str, printer_name: str, imap_sel: str
    ) -> ConfigFlowResult:
        """Set unique_id, build initial options and create the config entry."""
        initial_options: dict[str, Any] = {CONF_AUTO_PRINT_ENABLED: False}  # disabled until user configures
        email = _email_from_imap_entry(imap_sel, self._imap_entries)
        if email:
            initial_options[CONF_ALLOWED_SENDERS] = [email]

        await self.async_set_unique_id(f"{cups_url}/{printer_name}")
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=f"Print Bridge — {printer_name}",
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
        errors: dict[str, str] = {}

        if user_input is not None:
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
            schedule_days = _parse_schedule_days(
                user_input.get(CONF_SCHEDULE_DAYS, "")
            )
            schedule_template = str(
                user_input.get(CONF_SCHEDULE_TEMPLATE, "") or ""
            ).strip()

            for time_key in (CONF_SCHEDULE_START, CONF_SCHEDULE_END):
                val = user_input.get(time_key, "")
                if val and not _HHMM_PATTERN.match(val):
                    errors[time_key] = "invalid_time_format"

            if schedule_days is None:
                errors[CONF_SCHEDULE_DAYS] = "invalid_schedule_days"

            if schedule_template:
                try:
                    Template(schedule_template, self.hass).ensure_valid()
                except TemplateError:
                    errors[CONF_SCHEDULE_TEMPLATE] = "invalid_template"

            if not errors:
                return self.async_create_entry(
                    title="",
                    data={
                        **user_input,
                        CONF_BOOKLET_PATTERNS: patterns,
                        CONF_ALLOWED_SENDERS: senders,
                        CONF_FOLDER_FILTER: folders,
                        CONF_SCHEDULE_DAYS: schedule_days or [],
                        CONF_SCHEDULE_TEMPLATE: schedule_template,
                    },
                )

        current_patterns = options.get(CONF_BOOKLET_PATTERNS, [])
        current_senders = options.get(CONF_ALLOWED_SENDERS, [])
        current_folders = options.get(CONF_FOLDER_FILTER, [])
        current_schedule_days = _schedule_days_to_text(
            options.get(CONF_SCHEDULE_DAYS, DEFAULT_SCHEDULE_DAYS)
        )

        imap_folder_hint = ", ".join(
            f"{e.data.get('folder', 'INBOX')} ({e.data.get('username', e.title)})"
            for e in imap_entries
        ) or "none configured yet"

        schema_dict: dict = {
            vol.Required(
                CONF_AUTO_PRINT_ENABLED,
                default=options.get(CONF_AUTO_PRINT_ENABLED, DEFAULT_AUTO_PRINT_ENABLED),
            ): bool,
            vol.Optional(
                CONF_ALLOWED_SENDERS, default="\n".join(current_senders)
            ): str,
            vol.Optional(
                CONF_FOLDER_FILTER, default="\n".join(current_folders)
            ): str,
        }

        if imap_entries:
            schema_dict[
                vol.Optional("imap_account", default=_SENTINEL_SKIP_IMAP)
            ] = vol.In(_imap_choices(imap_entries))

        schema_dict.update({
            vol.Required(
                CONF_DUPLEX_MODE,
                default=options.get(CONF_DUPLEX_MODE, DEFAULT_DUPLEX_MODE),
            ): vol.In(DUPLEX_MODES),
            vol.Optional(
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
            vol.Required(
                CONF_EMAIL_ACTION,
                default=options.get(CONF_EMAIL_ACTION, DEFAULT_EMAIL_ACTION),
            ): vol.In(EMAIL_ACTIONS),
            vol.Optional(
                CONF_EMAIL_ARCHIVE_FOLDER,
                default=options.get(CONF_EMAIL_ARCHIVE_FOLDER, DEFAULT_EMAIL_ARCHIVE_FOLDER),
            ): str,
            vol.Required(
                CONF_NOTIFY_ON_FAILURE,
                default=options.get(CONF_NOTIFY_ON_FAILURE, DEFAULT_NOTIFY_ON_FAILURE),
            ): bool,
            vol.Required(
                CONF_NOTIFY_ON_SUCCESS,
                default=options.get(CONF_NOTIFY_ON_SUCCESS, DEFAULT_NOTIFY_ON_SUCCESS),
            ): bool,
            vol.Required(
                CONF_STATUS_REPLY_ENABLED,
                default=options.get(
                    CONF_STATUS_REPLY_ENABLED, DEFAULT_STATUS_REPLY_ENABLED
                ),
            ): bool,
            vol.Optional(
                CONF_STATUS_REPLY_NOTIFY_SERVICE,
                default=options.get(
                    CONF_STATUS_REPLY_NOTIFY_SERVICE,
                    DEFAULT_STATUS_REPLY_NOTIFY_SERVICE,
                ),
            ): str,
            vol.Required(
                CONF_SCHEDULE_ENABLED,
                default=options.get(CONF_SCHEDULE_ENABLED, DEFAULT_SCHEDULE_ENABLED),
            ): bool,
            vol.Optional(
                CONF_SCHEDULE_START,
                default=options.get(CONF_SCHEDULE_START, DEFAULT_SCHEDULE_START),
            ): str,
            vol.Optional(
                CONF_SCHEDULE_END,
                default=options.get(CONF_SCHEDULE_END, DEFAULT_SCHEDULE_END),
            ): str,
            vol.Optional(
                CONF_SCHEDULE_DAYS,
                default=current_schedule_days,
            ): str,
            vol.Optional(
                CONF_SCHEDULE_TEMPLATE,
                default=options.get(CONF_SCHEDULE_TEMPLATE, DEFAULT_SCHEDULE_TEMPLATE),
            ): str,
        })

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
            description_placeholders={"imap_folders": imap_folder_hint},
        )


# ---------------------------------------------------------------------------
# Schema / placeholder builders
# ---------------------------------------------------------------------------

def _build_user_schema(
    cups_url: str | None,
    printers: list[str],
    mdns_printers: list[dict[str, str]],
    imap_entries: list[ConfigEntry],
) -> vol.Schema:
    """Build the setup form schema.

    CUPS fields are only included when CUPS was discovered on this host
    (cups_url is not None).  When CUPS is absent the form focuses on
    Direct IPP; the description placeholders explain how to add CUPS.
    """
    schema: dict = {}

    # ── Direct IPP — mDNS discovered + manual entry ────────────────────────
    if mdns_printers:
        direct_options: dict[str, str] = {"": "None — configure CUPS below"}
        for p in mdns_printers:
            direct_options[p["url"]] = f"{p['name']}  ({p['url']})"
        schema[vol.Optional(CONF_DIRECT_PRINTER_URL, default="")] = vol.In(direct_options)
    else:
        schema[vol.Optional(CONF_DIRECT_PRINTER_URL, default="")] = str

    # ── CUPS — shown only when CUPS is running on this HA host ────────────
    # cups_url is None when no CUPS was found during discovery.
    if cups_url is not None:
        schema[vol.Optional(CONF_CUPS_URL, default=cups_url)] = str
        if printers:
            printer_options = {p: p for p in printers}
            printer_options[_SENTINEL_MANUAL] = "Enter name manually…"
            schema[vol.Optional(CONF_PRINTER_NAME, default=printers[0])] = vol.In(
                printer_options
            )
        else:
            schema[vol.Optional(CONF_PRINTER_NAME, default="")] = str

    # ── IMAP pre-fill ──────────────────────────────────────────────────────
    if imap_entries:
        schema[
            vol.Optional("imap_account", default=_SENTINEL_SKIP_IMAP)
        ] = vol.In(_imap_choices(imap_entries))

    # ── Scan again ─────────────────────────────────────────────────────────
    schema[vol.Optional("rescan", default=False)] = bool

    return vol.Schema(schema)


def _build_placeholders(
    cups_url: str | None,
    printers: list[str],
    mdns_printers: list[dict[str, str]],
    imap_entries: list[ConfigEntry],
) -> dict[str, str]:
    if mdns_printers:
        names = ", ".join(p["name"] for p in mdns_printers)
        direct_info = f"Found {len(mdns_printers)} printer(s) on the network: {names}"
    else:
        direct_info = (
            "No printers found on the network. "
            "Checklist: "
            "(1) Make sure the printer is turned ON and connected to the same Wi-Fi. "
            "(2) Check 'Scan again' below and submit to retry. "
            "(3) If still not found, type the IPP URL manually — "
            "check your router's connected-devices list for the printer's IP, "
            "then try http://PRINTER-IP/ipp/print."
        )

    if cups_url is None:
        # CUPS not found — explain the option without showing config fields
        cups_info = (
            "CUPS was not found on this host. "
            "Print Bridge works without CUPS via Direct IPP / AirPrint (select a printer above). "
            "To use CUPS, install the CUPS add-on and then click Scan again. "
            "See the documentation for details."
        )
    elif printers:
        cups_info = (
            f"CUPS found at {cups_url} with {len(printers)} printer(s): "
            + ", ".join(printers)
        )
    else:
        cups_info = f"CUPS is reachable at {cups_url} but has no printer queues configured yet."

    if imap_entries:
        names = ", ".join(e.data.get("username", e.title) for e in imap_entries)
        imap_info = f"Found {len(imap_entries)} IMAP account(s): {names}"
    else:
        imap_info = (
            "No IMAP accounts configured. "
            "Add the HA IMAP integration first."
        )

    return {
        "direct_info": direct_info,
        "cups_info": cups_info,
        "imap_info": imap_info,
    }
