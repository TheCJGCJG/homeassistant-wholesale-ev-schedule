"""Manual charge override and optimization-algorithm choice for Wholesale EV Schedule."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CHARGE_OVERRIDE_AUTO,
    CHARGE_OVERRIDE_FORCE_OFF,
    CHARGE_OVERRIDE_FORCE_ON,
    DOMAIN,
    OPTIMIZATION_ALGORITHM_GREEDY,
    OPTIMIZATION_ALGORITHM_HYBRID,
    OPTIMIZATION_ALGORITHM_OPTIMAL,
)
from .coordinator import WholesaleEvScheduleCoordinator
from .entity import WholesaleEvScheduleEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: WholesaleEvScheduleCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EvChargingOverrideSelect(coordinator), EvOptimizationAlgorithmSelect(coordinator)])


class EvChargingOverrideSelect(WholesaleEvScheduleEntity, SelectEntity):
    """Manual override for charging_desired, independent of any particular
    charger's own state — wire charging_desired to whatever actually controls
    your charger, and use this to force it on/off regardless of the computed
    schedule. Deliberately not tied to a charger-specific "work state" entity
    so this integration doesn't need to know anything about your charger brand."""

    _attr_translation_key = "charge_override"
    _attr_icon = "mdi:tune-vertical"
    _attr_options = [CHARGE_OVERRIDE_AUTO, CHARGE_OVERRIDE_FORCE_ON, CHARGE_OVERRIDE_FORCE_OFF]

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "select", "charge_override")

    @property
    def current_option(self) -> str:
        return self.coordinator.charge_override

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_charge_override(option)


class EvOptimizationAlgorithmSelect(WholesaleEvScheduleEntity, SelectEntity):
    """Which find_optimal_slots implementation computes the schedule (see
    scheduler.py and the OPTIMIZATION_ALGORITHM_* constants in const.py): "greedy"
    (the default, fast but occasionally suboptimal per issue #55), "optimal" (an
    exact search, always at least as cheap, more compute on very long price
    horizons), or "hybrid" (a narrowed exact search -- close to greedy's speed,
    much less likely to miss a cheaper single window, but not guaranteed globally
    optimal)."""

    _attr_translation_key = "optimization_algorithm"
    _attr_icon = "mdi:function-variant"
    _attr_options = [OPTIMIZATION_ALGORITHM_GREEDY, OPTIMIZATION_ALGORITHM_OPTIMAL, OPTIMIZATION_ALGORITHM_HYBRID]

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "select", "optimization_algorithm")

    @property
    def current_option(self) -> str:
        return self.coordinator.optimization_algorithm

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_optimization_algorithm(option)
