"""Shared entity base for Wholesale EV Schedule."""
from __future__ import annotations

from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, ENTITY_ID_PREFIX
from .coordinator import WholesaleEvScheduleCoordinator


class WholesaleEvScheduleEntity(CoordinatorEntity[WholesaleEvScheduleCoordinator]):
    """Base entity tying all platform entities to a single device per config entry.

    entity_id is set explicitly (not left to be derived from the friendly name) so
    it is always "<platform>.wholesale_ev_schedule_<suffix>" — this integration is
    designed to run alongside an unrelated EV-charging setup (e.g. a pyscript-based
    one) on the same HA instance, and must never collide with its entity_ids.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator, platform: str, unique_id_suffix: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{unique_id_suffix}"
        self.entity_id = f"{platform}.{ENTITY_ID_PREFIX}_{unique_id_suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name="Wholesale EV Schedule",
            manufacturer="Wholesale EV Schedule",
            model="EV charging scheduler",
        )
