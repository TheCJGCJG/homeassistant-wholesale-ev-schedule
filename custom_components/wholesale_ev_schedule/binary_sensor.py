"""Output binary sensor for Wholesale EV Schedule."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import WholesaleEvScheduleCoordinator
from .entity import WholesaleEvScheduleEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: WholesaleEvScheduleCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EvChargingDesiredBinarySensor(coordinator)])


class EvChargingDesiredBinarySensor(WholesaleEvScheduleEntity, BinarySensorEntity):
    """On when the charger should be running: car connected and in a charging slot
    (or boosting)."""

    _attr_translation_key = "charging_desired"

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "binary_sensor", "charging_desired")

    @property
    def is_on(self) -> bool | None:
        if not self.coordinator.data:
            return None
        return bool(self.coordinator.data.get("desired"))

    @property
    def icon(self) -> str:
        return "mdi:ev-station" if self.is_on else "mdi:power-off"
