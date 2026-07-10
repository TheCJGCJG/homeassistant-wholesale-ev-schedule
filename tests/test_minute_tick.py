"""Verifies the wall-clock-aligned minute tick registered in
`async_setup_entry` (`__init__.py`, via `async_track_time_change(..., second=0)`)
actually triggers a coordinator refresh.

Without it, `DataUpdateCoordinator`'s own timer only fires at a fixed delta
from whenever the coordinator was constructed -- not from a wall-clock minute
boundary -- so `charging_desired` (and every other coordinator-driven sensor)
could lag a real slot boundary by up to `update_interval_minutes` (5 minutes
by default) and change at an arbitrary second offset (e.g. 10:03:04) instead
of on the minute. This test proves a refresh happens at the very next :00
second boundary, well before a full `update_interval_minutes` has elapsed."""

from datetime import timedelta

import homeassistant.util.dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.wholesale_ev_schedule.const import DOMAIN

from .factories import async_setup_wholesale_entry


async def test_minute_aligned_tick_refreshes_coordinator_between_polls(hass, freezer):
    # Start a few seconds past a minute boundary -- comfortably under the
    # 5-minute default update_interval_minutes, so a refresh at the very next
    # :00 second boundary can only be the minute-aligned tick, not the
    # coordinator's own polling.
    freezer.move_to("2024-01-15 10:00:03+00:00")

    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    calculated_at_before = coordinator.data["calculated_at"]

    # Advance to the next wall-clock minute boundary (10:01:00) -- far short
    # of a full update_interval_minutes later.
    freezer.tick(timedelta(seconds=57))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    calculated_at_after = coordinator.data["calculated_at"]
    assert calculated_at_after != calculated_at_before

    refreshed_at = dt_util.parse_datetime(calculated_at_after)
    assert refreshed_at is not None
    # Landed exactly on a whole minute, not an arbitrary second offset.
    assert refreshed_at.second == 0
    assert refreshed_at >= dt_util.parse_datetime("2024-01-15T10:01:00+00:00")


async def test_minute_tick_unsub_on_unload(hass, freezer):
    """The tick listener must be torn down on unload/reload like the existing
    update listener -- registered via entry.async_on_unload, no custom
    async_unload_entry handling needed."""
    freezer.move_to("2024-01-15 10:00:03+00:00")

    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    calculated_at_before = coordinator.data["calculated_at"]

    # After unload, firing the minute boundary must not touch the old
    # coordinator instance -- its listener should have been unsubscribed.
    freezer.tick(timedelta(seconds=57))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert coordinator.data["calculated_at"] == calculated_at_before
