"""Action buttons for Wholesale EV Schedule."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import WholesaleEvScheduleCoordinator
from .entity import WholesaleEvScheduleEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: WholesaleEvScheduleCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        EvChargingBoostCancelButton(coordinator),
        EvChargingStopButton(coordinator),
        EvChargingResetButton(coordinator),
    ])


class EvChargingBoostCancelButton(WholesaleEvScheduleEntity, ButtonEntity):
    """Cancel an active boost early; the normal schedule resumes."""

    _attr_translation_key = "boost_cancel"
    _attr_icon = "mdi:cancel"

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "button", "boost_cancel")

    async def async_press(self) -> None:
        await self.coordinator.async_cancel_boost()


class EvChargingStopButton(WholesaleEvScheduleEntity, ButtonEntity):
    """Clear the entire schedule, cancel any boost, and return to idle."""

    _attr_translation_key = "stop"
    _attr_icon = "mdi:stop-circle"

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "button", "stop")

    async def async_press(self) -> None:
        await self.coordinator.async_stop()


class EvChargingResetButton(WholesaleEvScheduleEntity, ButtonEntity):
    """Full reset: also clears ready_by, unlike Stop. Intended for an
    automation triggered when the charger becomes unplugged, so the next
    plug-in starts from a completely clean slate."""

    _attr_translation_key = "reset"
    _attr_icon = "mdi:restart"

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "button", "reset")

    async def async_press(self) -> None:
        await self.coordinator.async_reset()
