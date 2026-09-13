"""Live pause/resume for opportunistic charging (see coordinator.py).

Only created when CONF_ENABLE_OPPORTUNISTIC_CHARGING is set at setup time --
same conditional-creation guard as number.py/datetime.py/sensor.py's
opportunistic entities, so an install that never opts in sees this platform
produce nothing at all.
"""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ENABLE_OPPORTUNISTIC_CHARGING, DOMAIN
from .coordinator import WholesaleEvScheduleCoordinator
from .entity import WholesaleEvScheduleEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    if not entry.options.get(CONF_ENABLE_OPPORTUNISTIC_CHARGING, False):
        return
    coordinator: WholesaleEvScheduleCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EvOpportunisticChargingEnabledSwitch(coordinator)])


class EvOpportunisticChargingEnabledSwitch(WholesaleEvScheduleEntity, SwitchEntity):
    """Live pause/resume for opportunistic charging, independent of the
    setup-time toggle: off just means the scheduler treats
    opportunistic_target_hours as 0 for this cycle, without touching any
    stored opportunistic value -- re-enabling resumes with everything intact."""

    _attr_translation_key = "opportunistic_charging_enabled"
    _attr_icon = "mdi:lightning-bolt-outline"

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator) -> None:
        super().__init__(coordinator, "switch", "opportunistic_charging_enabled")

    @property
    def is_on(self) -> bool:
        return self.coordinator.opportunistic_enabled

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_opportunistic_enabled(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_opportunistic_enabled(False)
