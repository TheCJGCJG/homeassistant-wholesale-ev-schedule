"""Shared entity base for Wholesale EV Schedule."""

from __future__ import annotations

from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import WholesaleEvScheduleCoordinator


class WholesaleEvScheduleEntity(CoordinatorEntity[WholesaleEvScheduleCoordinator]):
    """Base entity tying all platform entities to a single device per config entry.

    entity_id is set explicitly (not left to be derived from the friendly name) so
    it is always "<platform>.<entity_prefix>_<suffix>", where entity_prefix is the
    slugified instance name (see coordinator.entity_prefix / config_flow.py). This
    lets multiple instances of the integration (e.g. one per car) run side by side
    without colliding on entity_ids, and lets a single instance run alongside an
    unrelated EV-charging setup (e.g. a pyscript-based one) on the same HA instance.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: WholesaleEvScheduleCoordinator, platform: str, unique_id_suffix: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{unique_id_suffix}"
        self.entity_id = f"{platform}.{coordinator.entity_prefix}_{unique_id_suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=coordinator.instance_name,
            manufacturer="Wholesale EV Schedule",
            model="EV charging scheduler",
        )
