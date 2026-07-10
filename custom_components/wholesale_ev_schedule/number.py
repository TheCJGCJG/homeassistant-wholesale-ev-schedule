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
        EvChargingGambleToleranceNumber(coordinator),
        EvChargingMinBlockHoursNumber(coordinator),
        EvChargingMaxPriceNumber(coordinator),
        EvChargingAssumedChargeKwhNumber(coordinator),
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


class EvChargingGambleToleranceNumber(WholesaleEvScheduleEntity, NumberEntity):
    """How much predicted (non-actual) prices are trusted vs discounted when
    ranking slots. 0 = actual rates only; 100 = all prices at face value."""

    _attr_translation_key = "gamble_tolerance"
    _attr_icon = "mdi:dice-multiple"
    _attr_native_min_value = 0.0
    _attr_native_max_value = 100.0
    _attr_native_step = 5.0
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "number", "gamble_tolerance")

    @property
    def native_value(self) -> float:
        return self.coordinator.gamble_tolerance

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_gamble_tolerance(value)


class EvChargingMinBlockHoursNumber(WholesaleEvScheduleEntity, NumberEntity):
    """Minimum length of any single charging block, to prevent rapid charger
    cycling. 0 means no minimum at all — any block length is fine."""

    _attr_translation_key = "min_block_hours"
    _attr_icon = "mdi:timer-outline"
    _attr_native_min_value = 0.0
    _attr_native_max_value = 24.0
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = "h"
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "number", "min_block_hours")

    @property
    def native_value(self) -> float:
        return self.coordinator.min_block_hours

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_min_block_hours(value)


class EvChargingMaxPriceNumber(WholesaleEvScheduleEntity, NumberEntity):
    """Maximum average price per session — a session-level ceiling, not a
    per-slot one."""

    _attr_translation_key = "max_price"
    _attr_icon = "mdi:cash"
    _attr_native_min_value = 0.0
    _attr_native_max_value = 200.0
    _attr_native_step = 1.0
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "number", "max_price")

    @property
    def native_value(self) -> float:
        return self.coordinator.max_price

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_max_price(value)


class EvChargingAssumedChargeKwhNumber(WholesaleEvScheduleEntity, NumberEntity):
    """Rough assumed energy draw per charging session, used only to compute
    the estimated cost sensors — this integration only ever knows time slots
    and price, never actual delivered kWh. Set this to roughly match your
    car/charger for a more accurate estimate."""

    _attr_translation_key = "assumed_charge_kwh"
    _attr_icon = "mdi:battery-charging"
    _attr_native_min_value = 0.0
    _attr_native_max_value = 150.0
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = "kWh"
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "number", "assumed_charge_kwh")

    @property
    def native_value(self) -> float:
        return self.coordinator.assumed_charge_kwh

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_assumed_charge_kwh(value)
