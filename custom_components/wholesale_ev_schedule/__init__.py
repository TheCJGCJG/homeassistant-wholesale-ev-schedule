"""The Wholesale EV Schedule integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_change

from .const import CONF_ENABLE_OPPORTUNISTIC_CHARGING, DOMAIN, PLATFORMS
from .coordinator import WholesaleEvScheduleCoordinator

# Every opportunistic entity's unique_id is "{entry_id}_opportunistic_..."
# (see entity.py: unique_id = f"{entry_id}_{unique_id_suffix}", and
# switch.py/number.py/datetime.py/sensor.py's opportunistic entity classes,
# whose unique_id_suffix always starts with "opportunistic_"). Matching on
# this prefix rather than hardcoding each entity's exact suffix here means
# _async_remove_opportunistic_entities_if_disabled never needs updating when
# an opportunistic entity is added/renamed elsewhere -- it stays in sync with
# the classes that actually create them.
_OPPORTUNISTIC_UNIQUE_ID_INFIX = "_opportunistic_"


def _async_remove_opportunistic_entities_if_disabled(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Explicitly remove any previously-created opportunistic entities when
    the setup-time toggle is off -- e.g. reconfigured from on to off.

    Without this, turning the toggle off would leave those entities
    registered but "unavailable" (async_add_entities only ever adds; a
    platform simply not re-adding an entity on the next setup doesn't remove
    it from the registry), which is otherwise fine, matching how e.g.
    forecast_provider:none already leaves unrelated entities alone elsewhere
    in this integration -- but for opportunistic charging specifically,
    leaving stale-but-registered entities around after the user has
    explicitly turned the feature off was judged worth actively cleaning up.
    """
    if entry.options.get(CONF_ENABLE_OPPORTUNISTIC_CHARGING, False):
        return
    registry = er.async_get(hass)
    for entity_entry in registry.entities.get_entries_for_config_entry_id(entry.entry_id):
        if _OPPORTUNISTIC_UNIQUE_ID_INFIX in entity_entry.unique_id:
            registry.async_remove(entity_entry.entity_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = WholesaleEvScheduleCoordinator(hass, entry)
    await coordinator.async_load_stored_state()
    await coordinator.async_config_entry_first_refresh()

    _async_remove_opportunistic_entities_if_disabled(hass, entry)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # DataUpdateCoordinator's own update_interval timer fires at a fixed delta
    # from whenever the coordinator was constructed (integration setup/reload
    # time) — not aligned to wall-clock minute boundaries. That means
    # charging_desired (and every other coordinator-driven sensor) can lag a
    # real slot/schedule boundary by up to update_interval_minutes and land at
    # an arbitrary second offset (e.g. 10:03:04) instead of on the hour/minute.
    # This purely-additive minute-aligned tick guarantees a refresh at :00
    # seconds every minute, on top of (not instead of) the configured
    # update_interval_minutes polling.
    async def _async_minute_tick(_now) -> None:
        await coordinator.async_request_refresh()

    entry.async_on_unload(async_track_time_change(hass, _async_minute_tick, second=0))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change — some options (update interval,
    entity wiring) are only read at coordinator construction time, so a plain
    refresh isn't enough to pick them up."""
    await hass.config_entries.async_reload(entry.entry_id)
