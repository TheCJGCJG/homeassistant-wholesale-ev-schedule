"""Live-adjustable datetime input for Wholesale EV Schedule."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.datetime import DateTimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import WholesaleEvScheduleCoordinator
from .entity import WholesaleEvScheduleEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: WholesaleEvScheduleCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EvChargingReadyByDateTime(coordinator)])


class EvChargingReadyByDateTime(WholesaleEvScheduleEntity, DateTimeEntity):
    """When charging must be complete by."""

    _attr_translation_key = "ready_by"
    _attr_icon = "mdi:clock-check-outline"

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "datetime", "ready_by")

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.ready_by

    async def async_set_value(self, value: datetime) -> None:
        await self.coordinator.async_set_ready_by(value)
