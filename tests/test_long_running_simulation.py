"""Long-running simulation and true-restart coverage for issue #49.

Every other "survives restart" test in this suite calls
coordinator.async_load_stored_state() again on the *same* coordinator
instance -- that exercises the read/write round-trip logic, but not a full
config-entry teardown and a brand-new coordinator object rehydrating from
scratch, which is what an actual Home Assistant restart looks like.

Similarly, nothing else exercises the integration continuously across many
real-world hours/days -- ready_by rolling over at day boundaries, sessions
completing and being replaced, and no state corruption accumulating across
many consecutive coordinator refresh cycles, including across a DST
transition.
"""

from datetime import timedelta

import homeassistant.util.dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.wholesale_ev_schedule.const import DEFAULT_READY_BY_HOUR, DOMAIN

from .factories import (
    CURRENT_RATES_ENTITY,
    NEXT_RATES_ENTITY,
    async_setup_wholesale_entry,
    octopus_rate_points,
    set_octopus_rate_entity,
)


async def test_continuous_operation_across_several_days_including_a_dst_transition(hass, freezer):
    # UK clocks spring forward 01:00->02:00 on 2026-03-29 -- start a couple of
    # days before it and run a couple of days past it. freezer interprets a
    # naive move_to/tick target as UTC; dt_util.now() then renders it in
    # whichever timezone hass.config is set to.
    await hass.config.async_set_time_zone("Europe/London")
    freezer.move_to("2026-03-27 08:00:00")
    start = dt_util.now()

    # A full week of uniform, cheap, contiguous data from the start -- plenty
    # of buffer so the ~4.5 simulated days below never run short of future
    # price data (the point of this test is time passing, not data refresh,
    # which is already covered by test_active_session_survives_a_price_refresh).
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, octopus_rate_points(start, 336, price_gbp_per_kwh=0.05))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])

    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.last_update_success is True

    boosted = False
    for step in range(54):  # 54 * 2h = 108h ~= 4.5 simulated days
        freezer.tick(timedelta(hours=2))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

        # The main point: many consecutive refreshes across day boundaries
        # and a DST transition must never crash or leave the coordinator in a
        # failed state.
        assert coordinator.last_update_success is True
        assert coordinator.data["state"] != "error"
        assert coordinator.ready_by is not None
        assert coordinator.ready_by > dt_util.now() - timedelta(days=1)

        # Exercise boost + cancel once, midway through, to confirm live
        # controls still work correctly after a long run rather than only
        # freshly after setup.
        if step == 27 and not boosted:
            await coordinator.async_start_boost(1.0)
            assert coordinator.data["state"] == "boosting"
            await coordinator.async_cancel_boost()
            assert coordinator.data["state"] != "boosting"
            boosted = True

    assert boosted
    # ready_by must have rolled forward at least once across ~4.5 days and
    # still land on the configured default hour.
    assert dt_util.now() > start + timedelta(days=4)
    assert coordinator.ready_by.hour == DEFAULT_READY_BY_HOUR


async def test_restart_from_scratch_rehydrates_full_state(hass):
    # Regression for issue #49. A genuine restart -- unload, then set up
    # again -- produces a brand-new coordinator instance that has to
    # rehydrate entirely from the Store, unlike every other "survives
    # restart" test in this suite, which just calls
    # async_load_stored_state() again on the same object.
    now = dt_util.now()
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, octopus_rate_points(now, 12, price_gbp_per_kwh=0.05))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])

    entry = await async_setup_wholesale_entry(hass)
    coordinator_before = hass.data[DOMAIN][entry.entry_id]

    await coordinator_before.async_set_ready_by(now + timedelta(hours=5))
    await coordinator_before.async_set_required_hours(2.0)
    await coordinator_before.async_set_gamble_tolerance(75.0)
    await coordinator_before.async_set_charge_override("force_on")
    sessions_before = coordinator_before.data["sessions"]
    assert sessions_before  # something actually got scheduled, worth restoring

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator_after = hass.data[DOMAIN][entry.entry_id]
    assert coordinator_after is not coordinator_before  # a genuinely new instance

    assert coordinator_after.ready_by == coordinator_before.ready_by
    assert coordinator_after.required_hours == 2.0
    assert coordinator_after.gamble_tolerance == 75.0
    assert coordinator_after.charge_override == "force_on"
    assert coordinator_after.data["sessions"] == sessions_before
