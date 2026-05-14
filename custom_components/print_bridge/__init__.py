"""Print Bridge integration setup.

Architecture: this component subscribes to imap_content events fired by HA's
built-in IMAP integration.  When a PDF attachment is detected it calls
imap.fetch_part to retrieve the bytes and sends them to CUPS via IPP.
"""

from __future__ import annotations

import logging
import re
import urllib.parse

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from typing import TypeAlias
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_DIRECT_PRINTER_URL,
    CONF_DUPLEX_MODE,
    DOMAIN,
    DUPLEX_MODES,
    FIELD_BOOKLET,
    FIELD_DUPLEX,
    FIELD_FILE_PATH,
    CONF_AUTO_PRINT_ENABLED,
    SERVICE_CHECK_FILTER,
    SERVICE_CHECK_PRINTER_CAPABILITIES,
    SERVICE_CLEAR_QUEUE,
    SERVICE_PRINT_EMAIL,
    SERVICE_PRINT_FILE,
    SERVICE_PROCESS_IMAP_PART,
    SERVICE_RETRY_JOB,
)
from .coordinator import AutoPrintCoordinator

logger = logging.getLogger(__name__)

PLATFORMS = ["sensor", "binary_sensor", "button", "select", "switch", "text"]

_PRINT_FILE_SCHEMA = vol.Schema(
    {
        vol.Required(FIELD_FILE_PATH): cv.string,
        vol.Optional(FIELD_DUPLEX): vol.In(DUPLEX_MODES),
        vol.Optional(FIELD_BOOKLET, default=False): cv.boolean,
        vol.Optional("copies"): vol.All(int, vol.Range(min=1, max=20)),
        vol.Optional("orientation"): vol.In(("portrait", "landscape")),
        vol.Optional("media"): cv.string,
    }
)

_PROCESS_IMAP_PART_SCHEMA = vol.Schema(
    {
        vol.Required("entry_id"): cv.string,
        vol.Required("uid"): cv.string,
        vol.Required("part_key"): cv.string,
        vol.Optional("filename"): cv.string,
        vol.Optional("duplex"): vol.In(DUPLEX_MODES),
        vol.Optional("booklet", default=False): cv.boolean,
        vol.Optional("attachment_filter"): cv.string,
        vol.Optional("copies"): vol.All(int, vol.Range(min=1, max=20)),
        vol.Optional("orientation"): vol.In(("portrait", "landscape")),
        vol.Optional("media"): cv.string,
        vol.Optional("sender"): cv.string,
        vol.Optional("mail_subject"): cv.string,
        vol.Optional("mail_text"): cv.string,
    }
)

# Type alias for config entries carrying AutoPrintCoordinator as runtime data.
AutoPrintConfigEntry: TypeAlias = ConfigEntry[AutoPrintCoordinator]


def _entity_slug(value: str) -> str:
    """Return the entity-id slug HA used for old IP-derived direct labels."""
    slug = re.sub(r"[^a-z0-9_]+", "_", value.lower())
    return re.sub(r"_+", "_", slug).strip("_")


def _async_migrate_direct_ip_entity_ids(
    hass: HomeAssistant, entry: AutoPrintConfigEntry
) -> None:
    """Rename old direct-mode entity IDs that embedded the printer IP address."""
    direct_url = entry.data.get(CONF_DIRECT_PRINTER_URL)
    if not direct_url:
        return

    host = urllib.parse.urlparse(direct_url).hostname
    if not host:
        return

    old_slug = _entity_slug(host)
    if not old_slug:
        return

    registry = er.async_get(hass)
    for entity in list(registry.entities.values()):
        if entity.config_entry_id != entry.entry_id:
            continue
        domain, object_id = entity.entity_id.split(".", 1)
        if old_slug not in object_id:
            continue
        new_entity_id = f"{domain}.{object_id.replace(old_slug, 'direct_printer', 1)}"
        if new_entity_id == entity.entity_id or registry.async_is_registered(new_entity_id):
            continue
        registry.async_update_entity(entity.entity_id, new_entity_id=new_entity_id)
        logger.info(
            "Migrated Print Bridge entity_id from %s to %s",
            entity.entity_id,
            new_entity_id,
        )


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Install bundled blueprints into the user's config dir on first run."""
    import shutil
    from pathlib import Path as _Path

    def _install_blueprints() -> None:
        src = _Path(__file__).parent / "blueprints"
        if not src.exists():
            return
        dst = _Path(hass.config.config_dir) / "blueprints"
        for src_file in src.rglob("*.yaml"):
            dst_file = dst / src_file.relative_to(src)
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            old_bytes = dst_file.read_bytes() if dst_file.exists() else None
            new_bytes = src_file.read_bytes()
            if old_bytes != new_bytes:
                shutil.copy2(src_file, dst_file)
                logger.info("Updated Print Bridge blueprint: %s", dst_file.name)

    await hass.async_add_executor_job(_install_blueprints)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: AutoPrintConfigEntry) -> bool:
    """Set up Print Bridge from a config entry."""
    coordinator = AutoPrintCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    # Store coordinator as runtime_data (HA 2024+ recommended pattern).
    entry.runtime_data = coordinator

    _async_migrate_direct_ip_entity_ids(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # First-install notification when auto_print is disabled (default after fresh install).
    if not entry.options.get(CONF_AUTO_PRINT_ENABLED, True):
        try:
            from homeassistant.components.persistent_notification import async_create as _pn_create
            _pn_create(
                hass,
                (
                    "**Print Bridge is installed!** \n\n"
                    "Automatic printing is **disabled** until you choose a mode:\n\n"
                    "1. **Simple auto-print** — go to *Settings → Print Bridge → Configure* "
                    "and turn on *Enable automatic printing*.\n"
                    "2. **Blueprint (advanced)** — "
                    "[import the automation blueprint]"
                    "(https://my.home-assistant.io/redirect/blueprint_import"
                    "?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Frubeecube"
                    "%2Fha-print-bridge%2Fmain%2Fblueprints%2Fautomation%2Fprint_bridge"
                    "%2Fprint_from_email.yaml) "
                    "for per-sender / per-keyword rules.\n\n"
                    "To add the management dashboard, paste "
                    "`lovelace/print_bridge_audit.yaml` into a new dashboard view."
                ),
                title="Print Bridge — Action required",
                notification_id=f"print_bridge_setup_{entry.entry_id}",
            )
        except Exception:
            logger.debug("Could not create setup notification", exc_info=True)

    # Subscribe to imap_content events from HA's built-in IMAP integration.
    entry.async_on_unload(
        hass.bus.async_listen("imap_content", coordinator.async_handle_imap_event)
    )

    # Run the initial filter check after HA has fully started so all IMAP
    # config entries are guaranteed to be loaded.
    async def _initial_filter_check(_event: object = None) -> None:
        try:
            await coordinator.async_check_filter()
        except Exception:
            logger.debug("Initial filter check skipped (IMAP not yet configured)", exc_info=True)

    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
    if hass.is_running:
        # HA already started (e.g. integration reload) — run immediately.
        hass.async_create_task(_initial_filter_check())
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _initial_filter_check)

    # Register domain-level services once; guard against duplicate registration.
    if not hass.services.has_service(DOMAIN, SERVICE_PRINT_FILE):
        _register_services(hass)

    # Refresh entities when options change; coordinator properties read options live.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: AutoPrintConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Remove services only when no entries remain loaded.
    if unload_ok:
        remaining = [
            e
            for e in hass.config_entries.async_entries(DOMAIN)
            if e.entry_id != entry.entry_id
            and e.state is ConfigEntryState.LOADED
        ]
        if not remaining:
            for svc in (
                SERVICE_PRINT_FILE, SERVICE_CLEAR_QUEUE,
                SERVICE_PROCESS_IMAP_PART, SERVICE_CHECK_FILTER,
                SERVICE_CHECK_PRINTER_CAPABILITIES,
                SERVICE_RETRY_JOB, SERVICE_PRINT_EMAIL,
            ):
                hass.services.async_remove(DOMAIN, svc)

    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: AutoPrintConfigEntry) -> None:
    """Refresh entities after options change."""
    if entry.runtime_data is not None and entry.runtime_data.data is not None:
        entry.runtime_data.async_set_updated_data(entry.runtime_data.data)


def _register_services(hass: HomeAssistant) -> None:
    """Register integration-level services."""

    async def _handle_print_file(call: ServiceCall) -> None:
        file_path: str = call.data[FIELD_FILE_PATH]
        duplex: str | None = call.data.get(FIELD_DUPLEX)
        booklet: bool = call.data.get(FIELD_BOOKLET, False)
        copies: int | None = call.data.get("copies")
        orientation: str | None = call.data.get("orientation")
        media: str | None = call.data.get("media")

        coordinator = _get_any_coordinator(hass).selected_printer_coordinator
        result = await coordinator.async_print_file(
            file_path,
            duplex,
            booklet,
            copies=copies,
            orientation=orientation,
            media=media,
        )
        if not result.success:
            raise HomeAssistantError(
                f"Print job failed for '{result.filename}': {result.error}"
            )

    async def _handle_clear_queue(call: ServiceCall) -> None:
        coordinator = _get_any_coordinator(hass).selected_printer_coordinator
        deleted = await coordinator.async_clear_queue()
        logger.debug("Cleared %d file(s) from the print queue", deleted)

    async def _handle_process_imap_part(call: ServiceCall) -> None:
        """Service called by blueprints/automations to fetch an IMAP part and print it."""
        coordinator = _get_any_coordinator(hass).selected_printer_coordinator
        result = await coordinator.async_process_imap_part(
            entry_id=call.data["entry_id"],
            uid=call.data["uid"],
            part_key=call.data["part_key"],
            filename=call.data.get("filename"),
            duplex_override=call.data.get("duplex"),
            booklet_override=call.data.get("booklet", False) or None,
            attachment_filter=call.data.get("attachment_filter"),
            copies=call.data.get("copies"),
            orientation=call.data.get("orientation"),
            media=call.data.get("media"),
            sender=call.data.get("sender"),
            mail_subject=call.data.get("mail_subject"),
            mail_text=call.data.get("mail_text"),
        )
        if not result.success:
            raise HomeAssistantError(
                f"Print job failed for '{result.filename}': {result.error}"
            )

    hass.services.async_register(
        DOMAIN, SERVICE_PRINT_FILE, _handle_print_file, schema=_PRINT_FILE_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_CLEAR_QUEUE, _handle_clear_queue)
    hass.services.async_register(
        DOMAIN,
        SERVICE_PROCESS_IMAP_PART,
        _handle_process_imap_part,
        schema=_PROCESS_IMAP_PART_SCHEMA,
    )

    async def _handle_check_filter(call: ServiceCall) -> dict:
        """Run a filter preview and return the results as a service response."""
        coordinator = _get_any_coordinator(hass)
        result = await coordinator.async_check_filter(
            imap_entry_id=call.data.get("imap_entry_id"),
        )
        return {
            "checked_at": result.checked_at,
            "imap_account": result.imap_account,
            "total_found": result.total_found,
            "matching_filter": result.matching,
            "with_pdf": result.with_pdf,
            "emails": [e.as_dict() for e in result.emails],
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_CHECK_FILTER,
        _handle_check_filter,
        schema=vol.Schema(
            {vol.Optional("imap_entry_id"): cv.string}
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def _handle_check_printer_capabilities(call: ServiceCall) -> dict:
        """Query the selected target printer's IPP capabilities."""
        coordinator = _get_any_coordinator(hass).selected_printer_coordinator
        result = await coordinator.async_check_printer_capabilities(force=True)
        return result.as_dict()

    hass.services.async_register(
        DOMAIN,
        SERVICE_CHECK_PRINTER_CAPABILITIES,
        _handle_check_printer_capabilities,
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def _handle_retry_job(call: ServiceCall) -> dict:
        coordinator = _get_any_coordinator(hass).selected_printer_coordinator
        job_index: int | None = call.data.get("job_index")
        imap_uid: str | None = call.data.get("uid")
        duplex: str | None = call.data.get("duplex")
        booklet: bool | None = call.data.get("booklet")

        if job_index is not None:
            history = coordinator._job_history
            if job_index < 0 or job_index >= len(history):
                raise HomeAssistantError(
                    f"job_index {job_index} is out of range (history has {len(history)} entries)."
                )
            job = history[job_index]
        elif imap_uid:
            job = next(
                (j for j in coordinator._job_history if j.imap_uid == imap_uid),
                None,
            )
            if job is None:
                raise HomeAssistantError(
                    f"No job with uid='{imap_uid}' found in history."
                )
        else:
            # Default: retry last failed
            job = next(
                (j for j in coordinator._job_history if not j.success and j.can_retry),
                None,
            )
            if job is None:
                raise HomeAssistantError("No failed retryable job found in history.")

        result = await coordinator.async_retry_job(
            job,
            duplex_override=duplex,
            booklet_override=booklet,
        )
        return {
            "filename": result.filename,
            "success": result.success,
            "error": result.error,
            "timestamp": result.timestamp,
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_RETRY_JOB,
        _handle_retry_job,
        schema=vol.Schema(
            {
                vol.Optional("job_index"): vol.All(int, vol.Range(min=0)),
                vol.Optional("uid"): cv.string,
                vol.Optional("duplex"): vol.In(DUPLEX_MODES),
                vol.Optional("booklet"): cv.boolean,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def _handle_print_email(call: ServiceCall) -> dict:
        """Print all PDF attachments of a specific email from the IMAP server.

        This lets users trigger a print job for any email in their mailbox —
        ideal for on-demand printing from the Lovelace dashboard or automations.
        """
        uid: str = call.data["uid"]
        imap_entry_id: str | None = call.data.get("imap_entry_id")
        duplex: str | None = call.data.get("duplex")
        booklet: bool = call.data.get("booklet", False)
        attachment_filter: str | None = call.data.get("attachment_filter")
        copies: int | None = call.data.get("copies")
        orientation: str | None = call.data.get("orientation")
        media: str | None = call.data.get("media")

        controller = _get_any_coordinator(hass)
        coordinator = controller.selected_printer_coordinator
        return await coordinator.async_print_email(
            uid=uid,
            imap_entry_id=imap_entry_id or controller.selected_imap_entry_id,
            duplex=duplex,
            booklet=booklet,
            attachment_filter=attachment_filter,
            copies=copies,
            orientation=orientation,
            media=media,
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_PRINT_EMAIL,
        _handle_print_email,
        schema=vol.Schema(
            {
                vol.Required("uid"): cv.string,
                vol.Optional("imap_entry_id"): cv.string,
                vol.Optional("duplex"): vol.In(DUPLEX_MODES),
                vol.Optional("booklet", default=False): cv.boolean,
                vol.Optional("attachment_filter"): cv.string,
                vol.Optional("copies"): vol.All(int, vol.Range(min=1, max=20)),
                vol.Optional("orientation"): vol.In(("portrait", "landscape")),
                vol.Optional("media"): cv.string,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )


def _get_any_coordinator(hass: HomeAssistant) -> AutoPrintCoordinator:
    """Return the coordinator for the first loaded Print Bridge entry."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED and entry.runtime_data is not None:
            return entry.runtime_data  # type: ignore[return-value]
    raise HomeAssistantError("Print Bridge is not configured or not yet loaded")
