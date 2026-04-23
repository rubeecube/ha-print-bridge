"""Config flow and options flow for the Auto Print integration."""

from __future__ import annotations

import imaplib
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.helpers.config_validation as cv

from .const import (
    CONF_ALLOWED_SENDERS,
    CONF_AUTO_DELETE,
    CONF_BOOKLET_PATTERNS,
    CONF_CUPS_URL,
    CONF_DUPLEX_MODE,
    CONF_IMAP_FOLDER,
    CONF_IMAP_PASSWORD,
    CONF_IMAP_PORT,
    CONF_IMAP_SERVER,
    CONF_IMAP_USE_SSL,
    CONF_IMAP_USERNAME,
    CONF_POLL_INTERVAL_MINUTES,
    CONF_PRINTER_NAME,
    CONF_QUEUE_FOLDER,
    DEFAULT_AUTO_DELETE,
    DEFAULT_CUPS_URL,
    DEFAULT_DUPLEX_MODE,
    DEFAULT_IMAP_FOLDER,
    DEFAULT_IMAP_PORT,
    DEFAULT_IMAP_USE_SSL,
    DEFAULT_POLL_INTERVAL_MINUTES,
    DEFAULT_QUEUE_FOLDER,
    DOMAIN,
    DUPLEX_MODES,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Validation helpers (blocking — run in executor inside the flow)
# ---------------------------------------------------------------------------

def _test_imap_connection(
    server: str, port: int, use_ssl: bool, username: str, password: str, folder: str
) -> str | None:
    """Try to log in to the IMAP server and select the folder.

    Returns an error key string on failure, None on success.
    """
    try:
        if use_ssl:
            mail = imaplib.IMAP4_SSL(server, port)
        else:
            mail = imaplib.IMAP4(server, port)
    except OSError:
        return "cannot_connect_imap"

    try:
        status, _ = mail.login(username, password)
        if status != "OK":
            return "invalid_auth"
        status, _ = mail.select(folder, readonly=True)
        if status != "OK":
            return "cannot_connect_imap"
        return None
    except imaplib.IMAP4.error:
        return "invalid_auth"
    finally:
        try:
            mail.logout()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Config flow — two steps: mail server → printer
# ---------------------------------------------------------------------------

class AutoPrintConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup UI for Auto Print."""

    VERSION = 1

    def __init__(self) -> None:
        self._mail_data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: IMAP server + credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Deduplicate and normalise the allowed_senders text area.
            senders = [
                s.strip().lower()
                for s in user_input[CONF_ALLOWED_SENDERS].splitlines()
                if s.strip()
            ]
            if not senders:
                errors[CONF_ALLOWED_SENDERS] = "allowed_senders_empty"
            else:
                error = await self.hass.async_add_executor_job(
                    _test_imap_connection,
                    user_input[CONF_IMAP_SERVER],
                    user_input[CONF_IMAP_PORT],
                    user_input[CONF_IMAP_USE_SSL],
                    user_input[CONF_IMAP_USERNAME],
                    user_input[CONF_IMAP_PASSWORD],
                    user_input[CONF_IMAP_FOLDER],
                )
                if error:
                    errors["base"] = error
                else:
                    self._mail_data = {**user_input, CONF_ALLOWED_SENDERS: senders}
                    return await self.async_step_printer()

        schema = vol.Schema(
            {
                vol.Required(CONF_IMAP_SERVER): str,
                vol.Required(CONF_IMAP_PORT, default=DEFAULT_IMAP_PORT): int,
                vol.Required(CONF_IMAP_USE_SSL, default=DEFAULT_IMAP_USE_SSL): bool,
                vol.Required(CONF_IMAP_USERNAME): str,
                vol.Required(CONF_IMAP_PASSWORD): str,
                vol.Required(CONF_IMAP_FOLDER, default=DEFAULT_IMAP_FOLDER): str,
                vol.Required(CONF_ALLOWED_SENDERS): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_printer(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: CUPS URL + printer name."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Quick reachability check against the CUPS root URL.
            cups_url = user_input[CONF_CUPS_URL].rstrip("/")
            try:
                session = async_get_clientsession(self.hass)
                async with session.head(cups_url, timeout=5) as resp:
                    if resp.status >= 500:
                        errors["base"] = "cannot_connect_cups"
            except Exception:
                errors["base"] = "cannot_connect_cups"

            if not errors:
                await self.async_set_unique_id(
                    f"{self._mail_data[CONF_IMAP_USERNAME]}@{self._mail_data[CONF_IMAP_SERVER]}"
                )
                self._abort_if_unique_id_configured()

                config_data = {**self._mail_data, **user_input}
                return self.async_create_entry(
                    title=f"{self._mail_data[CONF_IMAP_USERNAME]} → {user_input[CONF_PRINTER_NAME]}",
                    data=config_data,
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_CUPS_URL, default=DEFAULT_CUPS_URL): str,
                vol.Required(CONF_PRINTER_NAME): str,
            }
        )
        return self.async_show_form(step_id="printer", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return AutoPrintOptionsFlow(config_entry)


# ---------------------------------------------------------------------------
# Options flow — editable after setup
# ---------------------------------------------------------------------------

class AutoPrintOptionsFlow(OptionsFlow):
    """Allow the user to adjust runtime options without re-running setup."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Single-step options form."""
        options = self._config_entry.options
        errors: dict[str, str] = {}

        if user_input is not None:
            patterns = [
                p.strip()
                for p in user_input[CONF_BOOKLET_PATTERNS].splitlines()
                if p.strip()
            ]
            poll = user_input[CONF_POLL_INTERVAL_MINUTES]
            if poll < 1:
                errors[CONF_POLL_INTERVAL_MINUTES] = "poll_interval_too_low"
            else:
                return self.async_create_entry(
                    title="",
                    data={**user_input, CONF_BOOKLET_PATTERNS: patterns},
                )

        current_patterns = options.get(CONF_BOOKLET_PATTERNS, [])
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_DUPLEX_MODE,
                    default=options.get(CONF_DUPLEX_MODE, DEFAULT_DUPLEX_MODE),
                ): vol.In(DUPLEX_MODES),
                vol.Required(
                    CONF_BOOKLET_PATTERNS,
                    default="\n".join(current_patterns),
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
                    CONF_POLL_INTERVAL_MINUTES,
                    default=options.get(
                        CONF_POLL_INTERVAL_MINUTES, DEFAULT_POLL_INTERVAL_MINUTES
                    ),
                ): int,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
