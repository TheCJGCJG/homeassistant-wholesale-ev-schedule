"""Live-adjustable datetime input for Wholesale EV Schedule."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.datetime import DateTimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ENABLE_OPPORTUNISTIC_CHARGING, DOMAIN
from .coordinator import WholesaleEvScheduleCoordinator
from .entity import WholesaleEvScheduleEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: WholesaleEvScheduleCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[DateTimeEntity] = [EvChargingReadyByDateTime(coordinator)]
    if entry.options.get(CONF_ENABLE_OPPORTUNISTIC_CHARGING, False):
        entities.append(EvOpportunisticReadyByDateTime(coordinator))
    async_add_entities(entities)


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


class EvOpportunisticReadyByDateTime(WholesaleEvScheduleEntity, DateTimeEntity):
    """When opportunistic top-up charging should be complete by. Live-editable,
    but only as a one-cycle override -- it's re-derived as
    `ready_by + default_opportunistic_offset_days` every time ready_by itself
    rolls forward (see coordinator.py's _async_update_data), so a manual edit
    here holds only until the next such rollover."""

    _attr_translation_key = "opportunistic_ready_by"
    _attr_icon = "mdi:clock-plus-outline"

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "datetime", "opportunistic_ready_by")

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.opportunistic_ready_by

    async def async_set_value(self, value: datetime) -> None:
        await self.coordinator.async_set_opportunistic_ready_by(value)
