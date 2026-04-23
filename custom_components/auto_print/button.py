"""Button entities for the Auto Print integration."""

from __future__ import annotations

import io
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import BUTTON_TEST_PAGE, DATA_COORDINATOR, DOMAIN
from .coordinator import AutoPrintCoordinator
from .sensor import _device_info

logger = logging.getLogger(__name__)

_TEST_PAGE_CONTENT = b"""\
%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/MediaBox[0 0 595 842]/Parent 2 0 R/Resources<</Font<</F1 4 0 R>>>>>>endobj
4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
5 0 obj<</Length 44>>
stream
BT /F1 24 Tf 200 400 Td (Auto Print test) Tj ET
endstream
endobj
xref
0 6
0000000000 65535 f\r
0000000009 00000 n\r
0000000058 00000 n\r
0000000115 00000 n\r
0000000266 00000 n\r
0000000343 00000 n\r
trailer<</Size 6/Root 1 0 R>>
startxref
440
%%EOF
"""


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AutoPrintCoordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
    async_add_entities([TestPageButton(coordinator, entry)])


class TestPageButton(CoordinatorEntity[AutoPrintCoordinator], ButtonEntity):
    """Send a minimal test-page PDF to the printer."""

    _attr_has_entity_name = True
    _attr_translation_key = BUTTON_TEST_PAGE
    _attr_icon = "mdi:printer-check"

    def __init__(self, coordinator: AutoPrintCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{BUTTON_TEST_PAGE}"
        self._attr_device_info = _device_info(entry)

    async def async_press(self) -> None:
        """Send the built-in test page to the printer."""
        result = await self.coordinator._async_send_print_job(
            filename="auto_print_test_page.pdf",
            pdf_data=_TEST_PAGE_CONTENT,
            duplex_mode="one-sided",
            booklet=False,
        )
        if not result.success:
            raise HomeAssistantError(
                f"Test page failed: {result.error}"
            )
