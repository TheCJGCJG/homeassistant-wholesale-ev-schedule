"""max_price / min_block_hours enforcement, and confirming that options requiring
a coordinator rebuild (update_interval_minutes) actually take effect after an
options-flow save (via the config-entry reload triggered in __init__.py).
"""
from datetime import timedelta

import homeassistant.util.dt as dt_util

from custom_components.wholesale_ev_schedule.const import (
    CONF_MAX_PRICE,
    CONF_MIN_BLOCK_HOURS,
    CONF_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
)

from .factories import (
    CURRENT_RATES_ENTITY,
    FULL_OPTIONS,
    NEXT_RATES_ENTITY,
    async_setup_wholesale_entry,
    octopus_rate_points,
    set_octopus_rate_entity,
)


async def _schedule_after(hass, coordinator, ready_in_hours, required_hours):
    await coordinator.async_set_ready_by(dt_util.now() + timedelta(hours=ready_in_hours))
    await coordinator.async_set_required_hours(required_hours)
    await hass.async_block_till_done()


async def test_max_price_below_all_available_rates_is_unschedulable(hass):
    options = {**FULL_OPTIONS, CONF_MAX_PRICE: 1.0}
    entry = await async_setup_wholesale_entry(hass, options)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    now = dt_util.now()
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, octopus_rate_points(now, 6, 0.30))  # 30p/kWh
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])

    await _schedule_after(hass, coordinator, ready_in_hours=3, required_hours=1.0)

    assert coordinator.data["state"] == "unschedulable"
    assert "max_price" in coordinator.data["error_reason"]


async def test_min_block_hours_relaxes_when_longer_than_required(hass):
    # min_block_hours (2h) longer than required_hours (1h) must relax to a
    # single contiguous block rather than making the request unschedulable.
    options = {**FULL_OPTIONS, CONF_MIN_BLOCK_HOURS: 2.0}
    entry = await async_setup_wholesale_entry(hass, options)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    now = dt_util.now()
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, octopus_rate_points(now, 6, 0.05))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])

    await _schedule_after(hass, coordinator, ready_in_hours=3, required_hours=1.0)

    assert coordinator.data["state"] in ("scheduled", "charging")
    assert coordinator.data["sessions"]


async def test_update_interval_option_takes_effect_after_reload(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.update_interval == timedelta(minutes=5)

    hass.config_entries.async_update_entry(entry, options={**FULL_OPTIONS, CONF_UPDATE_INTERVAL_MINUTES: 15})
    await hass.async_block_till_done()

    reloaded_coordinator = hass.data[DOMAIN][entry.entry_id]
    assert reloaded_coordinator.update_interval == timedelta(minutes=15)
