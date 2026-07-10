"""The Wholesale EV Schedule integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_change

from .const import DOMAIN, PLATFORMS
from .coordinator import WholesaleEvScheduleCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = WholesaleEvScheduleCoordinator(hass, entry)
    await coordinator.async_load_stored_state()
    await coordinator.async_config_entry_first_refresh()

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
