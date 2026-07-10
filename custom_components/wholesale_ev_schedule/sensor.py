"""Output sensors for Wholesale EV Schedule.

Split into two groups: the primary sensors (state/schedule/next-slot/hours
remaining/time remaining/boost countdown) are visible by default since
they're what you'd put on a dashboard. Everything below "Diagnostics" is
`entity_category=DIAGNOSTIC` and hidden by default (still fully available for
automations/history/graphs — just not cluttering the default dashboard) —
block counts, further upcoming blocks, and the market-data summary (price
range, source breakdown, active providers) that answers "what did the
optimizer have to choose from".
"""
from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import WholesaleEvScheduleCoordinator
from .entity import WholesaleEvScheduleEntity
from .scheduler import parse_dt


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: WholesaleEvScheduleCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        EvChargingStateSensor(coordinator),
        EvChargingScheduleSensor(coordinator),
        EvChargingNextSlotStartSensor(coordinator),
        EvChargingNextSlotEndSensor(coordinator),
        EvChargingNextSlotAveragePriceSensor(coordinator),
        EvChargingNextSlotEstimatedCostSensor(coordinator),
        EvChargingHoursRemainingSensor(coordinator),
        EvChargingTimeRemainingSensor(coordinator),
        EvChargingBoostEndsAtSensor(coordinator),
        # Diagnostics — hidden by default.
        EvChargingBlockCountSensor(coordinator),
        EvChargingUpcomingBlock2StartSensor(coordinator),
        EvChargingUpcomingBlock2EndSensor(coordinator),
        EvChargingUpcomingBlock3StartSensor(coordinator),
        EvChargingUpcomingBlock3EndSensor(coordinator),
        EvChargingCandidatePricePointsSensor(coordinator),
        EvChargingCheapestAvailablePriceSensor(coordinator),
        EvChargingMostExpensiveAvailablePriceSensor(coordinator),
        EvChargingAveragePriceNext24hSensor(coordinator),
        EvChargingAveragePriceAllDataSensor(coordinator),
        EvChargingPriceDataSourcesSensor(coordinator),
        EvChargingActiveProvidersSensor(coordinator),
    ])


def _upcoming_slot(coordinator: WholesaleEvScheduleCoordinator, index: int) -> dict | None:
    if not coordinator.data:
        return None
    upcoming = coordinator.data.get("upcoming_slots") or []
    return upcoming[index] if len(upcoming) > index else None


def _price_summary(coordinator: WholesaleEvScheduleCoordinator) -> dict:
    if not coordinator.data:
        return {}
    return coordinator.data.get("price_summary") or {}


def _next_slot_average_price(coordinator: WholesaleEvScheduleCoordinator) -> float | None:
    if not coordinator.data:
        return None
    next_slot = coordinator.data.get("next_slot")
    return next_slot["avg_price"] if next_slot else None


# =============================================================================
# Primary sensors
# =============================================================================

class EvChargingStateSensor(WholesaleEvScheduleEntity, SensorEntity):
    """Current state machine state."""

    _attr_translation_key = "charging_state"
    _attr_icon = "mdi:state-machine"

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "sensor", "charging_state")

    @property
    def native_value(self) -> str | None:
        return self.coordinator.data.get("state") if self.coordinator.data else None

    @property
    def extra_state_attributes(self) -> dict:
        if not self.coordinator.data:
            return {}
        return {
            "error_reason": self.coordinator.data.get("error_reason"),
            "boost_ends_at": self.coordinator.data.get("boost_end"),
        }


class EvChargingScheduleSensor(WholesaleEvScheduleEntity, SensorEntity):
    """Schedule state with the full session list — every proposed/committed
    slot, not just the next one — as an attribute."""

    _attr_translation_key = "charging_schedule"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "sensor", "charging_schedule")

    @property
    def native_value(self) -> str | None:
        return self.coordinator.data.get("state") if self.coordinator.data else None

    @property
    def extra_state_attributes(self) -> dict:
        if not self.coordinator.data:
            return {}
        data = self.coordinator.data
        sessions = data.get("sessions", [])
        return {
            "slots": sessions,
            "future_slots": data.get("upcoming_slots", []),
            "active_slot": data.get("active_slot"),
            "next_slot": data.get("next_slot"),
            "block_count": data.get("block_count", 0),
            "total_committed_hours": sum(s.get("duration_hours", 0.0) for s in sessions),
            "hours_remaining": round(data.get("hours_remaining", 0.0), 2),
            "calculated_at": data.get("calculated_at"),
        }


class EvChargingNextSlotStartSensor(WholesaleEvScheduleEntity, SensorEntity):
    """ISO datetime of the next scheduled slot start."""

    _attr_translation_key = "next_slot_start"
    _attr_icon = "mdi:clock-start"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "sensor", "next_slot_start")

    @property
    def native_value(self) -> datetime | None:
        if not self.coordinator.data:
            return None
        next_slot = self.coordinator.data.get("next_slot")
        return parse_dt(next_slot["start"]) if next_slot else None


class EvChargingNextSlotEndSensor(WholesaleEvScheduleEntity, SensorEntity):
    """ISO datetime of the next scheduled slot end."""

    _attr_translation_key = "next_slot_end"
    _attr_icon = "mdi:clock-end"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "sensor", "next_slot_end")

    @property
    def native_value(self) -> datetime | None:
        if not self.coordinator.data:
            return None
        next_slot = self.coordinator.data.get("next_slot")
        return parse_dt(next_slot["end"]) if next_slot else None


class EvChargingNextSlotAveragePriceSensor(WholesaleEvScheduleEntity, SensorEntity):
    """Average price of the next scheduled slot. None when nothing's scheduled."""

    _attr_translation_key = "next_slot_average_price"
    _attr_icon = "mdi:cash"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "sensor", "next_slot_average_price")

    @property
    def native_value(self) -> float | None:
        return _next_slot_average_price(self.coordinator)


class EvChargingNextSlotEstimatedCostSensor(WholesaleEvScheduleEntity, SensorEntity):
    """Next slot's average price × assumed_charge_kwh — a rough estimate only,
    since this integration never knows actual delivered energy. Same price
    unit as average price / max price (no currency conversion applied)."""

    _attr_translation_key = "next_slot_estimated_cost"
    _attr_icon = "mdi:receipt-text-outline"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "sensor", "next_slot_estimated_cost")

    @property
    def native_value(self) -> float | None:
        avg_price = _next_slot_average_price(self.coordinator)
        if avg_price is None:
            return None
        return round(avg_price * self.coordinator.assumed_charge_kwh, 2)


class EvChargingHoursRemainingSensor(WholesaleEvScheduleEntity, SensorEntity):
    """Hours of uncommenced committed charging."""

    _attr_translation_key = "hours_remaining"
    _attr_icon = "mdi:timer-outline"
    _attr_native_unit_of_measurement = "h"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "sensor", "hours_remaining")

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        return round(self.coordinator.data.get("hours_remaining", 0.0), 2)


class EvChargingTimeRemainingSensor(WholesaleEvScheduleEntity, SensorEntity):
    """Same value as hours_remaining, as a proper duration-class sensor (in
    minutes) — HA renders duration sensors as e.g. "1:30:00" rather than a
    bare decimal, and it composes with duration-based automations/cards."""

    _attr_translation_key = "time_remaining"
    _attr_icon = "mdi:timer-sand"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "sensor", "time_remaining")

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        return round(self.coordinator.data.get("hours_remaining", 0.0) * 60)


class EvChargingBoostEndsAtSensor(WholesaleEvScheduleEntity, SensorEntity):
    """When the active boost ends. None outside of boosting."""

    _attr_translation_key = "boost_ends_at"
    _attr_icon = "mdi:lightning-bolt-outline"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "sensor", "boost_ends_at")

    @property
    def native_value(self) -> datetime | None:
        if not self.coordinator.data:
            return None
        boost_end = self.coordinator.data.get("boost_end")
        return parse_dt(boost_end) if boost_end else None


# =============================================================================
# Diagnostics — hidden by default (entity_registry_visible_default=False),
# grouped under the device's Diagnostic section.
# =============================================================================

class _DiagnosticSensor(WholesaleEvScheduleEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_visible_default = False


class EvChargingBlockCountSensor(_DiagnosticSensor):
    """Number of charging blocks in the current schedule (active + future)."""

    _attr_translation_key = "block_count"
    _attr_icon = "mdi:format-list-numbered"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "sensor", "block_count")

    @property
    def native_value(self) -> int | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("block_count", 0)


class EvChargingUpcomingBlock2StartSensor(_DiagnosticSensor):
    """Start of the second upcoming block, when the schedule has more than one."""

    _attr_translation_key = "upcoming_block_2_start"
    _attr_icon = "mdi:clock-start"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "sensor", "upcoming_block_2_start")

    @property
    def native_value(self) -> datetime | None:
        slot = _upcoming_slot(self.coordinator, 1)
        return parse_dt(slot["start"]) if slot else None


class EvChargingUpcomingBlock2EndSensor(_DiagnosticSensor):
    """End of the second upcoming block, when the schedule has more than one."""

    _attr_translation_key = "upcoming_block_2_end"
    _attr_icon = "mdi:clock-end"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "sensor", "upcoming_block_2_end")

    @property
    def native_value(self) -> datetime | None:
        slot = _upcoming_slot(self.coordinator, 1)
        return parse_dt(slot["end"]) if slot else None


class EvChargingUpcomingBlock3StartSensor(_DiagnosticSensor):
    """Start of the third upcoming block, when the schedule has that many."""

    _attr_translation_key = "upcoming_block_3_start"
    _attr_icon = "mdi:clock-start"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "sensor", "upcoming_block_3_start")

    @property
    def native_value(self) -> datetime | None:
        slot = _upcoming_slot(self.coordinator, 2)
        return parse_dt(slot["start"]) if slot else None


class EvChargingUpcomingBlock3EndSensor(_DiagnosticSensor):
    """End of the third upcoming block, when the schedule has that many."""

    _attr_translation_key = "upcoming_block_3_end"
    _attr_icon = "mdi:clock-end"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "sensor", "upcoming_block_3_end")

    @property
    def native_value(self) -> datetime | None:
        slot = _upcoming_slot(self.coordinator, 2)
        return parse_dt(slot["end"]) if slot else None


class EvChargingCandidatePricePointsSensor(_DiagnosticSensor):
    """How many price data points (actual + predicted, deduplicated) were
    available to the optimizer this cycle — "options considered"."""

    _attr_translation_key = "candidate_price_points"
    _attr_icon = "mdi:database-search"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "sensor", "candidate_price_points")

    @property
    def native_value(self) -> int | None:
        return _price_summary(self.coordinator).get("count")


class EvChargingCheapestAvailablePriceSensor(_DiagnosticSensor):
    """Cheapest price point in the current candidate dataset, regardless of
    whether it ended up scheduled."""

    _attr_translation_key = "cheapest_available_price"
    _attr_icon = "mdi:cash-minus"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "sensor", "cheapest_available_price")

    @property
    def native_value(self) -> float | None:
        return _price_summary(self.coordinator).get("cheapest_price")


class EvChargingMostExpensiveAvailablePriceSensor(_DiagnosticSensor):
    """Most expensive price point in the current candidate dataset."""

    _attr_translation_key = "most_expensive_available_price"
    _attr_icon = "mdi:cash-plus"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "sensor", "most_expensive_available_price")

    @property
    def native_value(self) -> float | None:
        return _price_summary(self.coordinator).get("most_expensive_price")


class EvChargingAveragePriceNext24hSensor(_DiagnosticSensor):
    """Average price across candidate data points in the next 24 hours."""

    _attr_translation_key = "average_price_next_24h"
    _attr_icon = "mdi:chart-line"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "sensor", "average_price_next_24h")

    @property
    def native_value(self) -> float | None:
        return _price_summary(self.coordinator).get("average_price_next_window")


class EvChargingAveragePriceAllDataSensor(_DiagnosticSensor):
    """Average price across the entire currently-available candidate dataset
    (not just the next 24h)."""

    _attr_translation_key = "average_price_all_data"
    _attr_icon = "mdi:chart-line"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "sensor", "average_price_all_data")

    @property
    def native_value(self) -> float | None:
        return _price_summary(self.coordinator).get("average_price")


class EvChargingPriceDataSourcesSensor(_DiagnosticSensor):
    """State is the total candidate price point count; attributes break that
    down by source (current_actual / next_actual / predicted) — "sources used"."""

    _attr_translation_key = "price_data_sources"
    _attr_icon = "mdi:database"

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "sensor", "price_data_sources")

    @property
    def native_value(self) -> int | None:
        return _price_summary(self.coordinator).get("count")

    @property
    def extra_state_attributes(self) -> dict:
        return {"source_counts": _price_summary(self.coordinator).get("source_counts", {})}


class EvChargingActiveProvidersSensor(_DiagnosticSensor):
    """Which named provider (see providers.py) supplies rates and forecast
    data for this instance — set once at config time, shown here for
    visibility without needing to inspect the integration's configuration."""

    _attr_translation_key = "active_providers"
    _attr_icon = "mdi:api"

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "sensor", "active_providers")

    @property
    def native_value(self) -> str:
        return f"{self.coordinator.rates_provider_label} / {self.coordinator.forecast_provider_label}"

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "rates_provider": self.coordinator.rates_provider_label,
            "forecast_provider": self.coordinator.forecast_provider_label,
        }
