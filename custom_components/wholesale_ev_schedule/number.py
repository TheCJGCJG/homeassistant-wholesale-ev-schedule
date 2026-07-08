"""Live-adjustable numeric inputs for Wholesale EV Schedule."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import WholesaleEvScheduleCoordinator
from .entity import WholesaleEvScheduleEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: WholesaleEvScheduleCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        EvChargingHoursRequiredNumber(coordinator),
        EvChargingBoostDurationNumber(coordinator),
    ])


class EvChargingHoursRequiredNumber(WholesaleEvScheduleEntity, NumberEntity):
    """Hours of charging needed before ready_by. 0 means idle."""

    _attr_translation_key = "charging_hours_required"
    _attr_icon = "mdi:ev-station"
    _attr_native_min_value = 0.0
    _attr_native_max_value = 24.0
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = "h"
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "number", "charging_hours_required")

    @property
    def native_value(self) -> float:
        return self.coordinator.required_hours

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_required_hours(value)


class EvChargingBoostDurationNumber(WholesaleEvScheduleEntity, NumberEntity):
    """Set to start an immediate boost for that many hours. Resets to 0 once the
    boost is registered, mirroring the pyscript original — this prevents a
    non-zero slider value from silently re-triggering a new boost once the
    current one expires."""

    _attr_translation_key = "boost_duration_hours"
    _attr_icon = "mdi:lightning-bolt"
    _attr_native_min_value = 0.0
    _attr_native_max_value = 8.0
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = "h"
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "number", "boost_duration_hours")
        self._attr_native_value = 0.0

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = 0.0
        self.async_write_ha_state()
        if value > 0:
            await self.coordinator.async_start_boost(value)
