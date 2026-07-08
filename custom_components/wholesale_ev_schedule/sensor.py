"""Output sensors for Wholesale EV Schedule."""
from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
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
        EvChargingHoursRemainingSensor(coordinator),
    ])


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
        return {"error_reason": self.coordinator.data.get("error_reason")}


class EvChargingScheduleSensor(WholesaleEvScheduleEntity, SensorEntity):
    """Schedule state with the full session list as an attribute."""

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
            "active_slot": data.get("active_slot"),
            "next_slot": data.get("next_slot"),
            "total_committed_hours": sum(s.get("duration_hours", 0.0) for s in sessions),
            "hours_remaining": round(data.get("hours_remaining", 0.0), 2),
            "calculated_at": data.get("calculated_at"),
        }


class EvChargingNextSlotStartSensor(WholesaleEvScheduleEntity, SensorEntity):
    """ISO datetime of the next scheduled slot start."""

    _attr_translation_key = "next_slot_start"
    _attr_icon = "mdi:clock-start"
    _attr_device_class = "timestamp"

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
    _attr_device_class = "timestamp"

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "sensor", "next_slot_end")

    @property
    def native_value(self) -> datetime | None:
        if not self.coordinator.data:
            return None
        next_slot = self.coordinator.data.get("next_slot")
        return parse_dt(next_slot["end"]) if next_slot else None


class EvChargingHoursRemainingSensor(WholesaleEvScheduleEntity, SensorEntity):
    """Hours of uncommenced committed charging."""

    _attr_translation_key = "hours_remaining"
    _attr_icon = "mdi:timer-outline"
    _attr_native_unit_of_measurement = "h"

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "sensor", "hours_remaining")

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        return round(self.coordinator.data.get("hours_remaining", 0.0), 2)
