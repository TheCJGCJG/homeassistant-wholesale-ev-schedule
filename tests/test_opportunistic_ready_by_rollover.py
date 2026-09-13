"""opportunistic_ready_by's auto-derivation and "snaps back on rollover"
semantics (issue #53): it's always `ready_by + default_opportunistic_offset_days`
the moment ready_by itself rolls forward, and a live/manual edit only holds
for the current cycle -- the next required-ready_by rollover overwrites it
back to the derived value regardless of what was set in between.
"""

from datetime import timedelta

from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.wholesale_ev_schedule.const import (
    CONF_DEFAULT_OPPORTUNISTIC_OFFSET_DAYS,
    CONF_DEFAULT_READY_BY_HOUR,
    DOMAIN,
)

from .factories import OPPORTUNISTIC_OPTIONS, async_setup_wholesale_entry


async def test_opportunistic_ready_by_defaults_to_required_ready_by_plus_offset(hass):
    entry = await async_setup_wholesale_entry(hass, options=OPPORTUNISTIC_OPTIONS)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    offset_days = OPPORTUNISTIC_OPTIONS[CONF_DEFAULT_OPPORTUNISTIC_OFFSET_DAYS]
    assert coordinator.opportunistic_ready_by == coordinator.ready_by + timedelta(days=offset_days)


async def test_opportunistic_ready_by_manual_edit_holds_until_next_rollover(hass, freezer):
    freezer.move_to("2026-04-01 12:00:00+00:00")
    entry = await async_setup_wholesale_entry(hass, options=OPPORTUNISTIC_OPTIONS)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    derived = coordinator.opportunistic_ready_by
    manual_value = derived + timedelta(days=3)
    await coordinator.async_set_opportunistic_ready_by(manual_value)
    assert coordinator.opportunistic_ready_by == manual_value

    # A refresh that does NOT cross required ready_by's own rollover must
    # leave the manual edit alone.
    freezer.tick(timedelta(hours=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert coordinator.opportunistic_ready_by == manual_value

    # Advance past required ready_by's own deadline -- this is the one place
    # opportunistic_ready_by gets forcibly re-derived, overwriting the
    # manual edit regardless of how far in the future it was set.
    freezer.move_to(coordinator.ready_by + timedelta(minutes=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    offset_days = OPPORTUNISTIC_OPTIONS[CONF_DEFAULT_OPPORTUNISTIC_OFFSET_DAYS]
    assert coordinator.opportunistic_ready_by == coordinator.ready_by + timedelta(days=offset_days)
    assert coordinator.opportunistic_ready_by != manual_value


async def test_opportunistic_ready_by_rolls_forward_across_several_required_rollovers(hass, freezer):
    freezer.move_to("2026-04-01 12:00:00+00:00")
    entry = await async_setup_wholesale_entry(hass, options=OPPORTUNISTIC_OPTIONS)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    offset_days = OPPORTUNISTIC_OPTIONS[CONF_DEFAULT_OPPORTUNISTIC_OFFSET_DAYS]
    default_hour = OPPORTUNISTIC_OPTIONS[CONF_DEFAULT_READY_BY_HOUR]

    for _ in range(3):
        freezer.move_to(coordinator.ready_by + timedelta(minutes=5))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert coordinator.opportunistic_ready_by == coordinator.ready_by + timedelta(days=offset_days)
        assert coordinator.ready_by.hour == default_hour


async def test_manually_editing_required_ready_by_does_not_perturb_opportunistic_ready_by(hass, freezer):
    # Distinguishes a live edit to required ready_by (a mid-cycle adjustment
    # to the current deadline -- not a rollover) from ready_by actually
    # *rolling forward* once reached (the one and only trigger for
    # re-deriving opportunistic_ready_by, see async_set_ready_by vs the
    # rollover block in _async_update_data). Pushing ready_by further out by
    # hand must not itself snap opportunistic_ready_by back to the derived
    # value -- only reaching/rolling past the (new) deadline does that.
    freezer.move_to("2026-04-01 12:00:00+00:00")
    entry = await async_setup_wholesale_entry(hass, options=OPPORTUNISTIC_OPTIONS)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    derived = coordinator.opportunistic_ready_by
    manual_opportunistic_value = derived + timedelta(days=3)
    await coordinator.async_set_opportunistic_ready_by(manual_opportunistic_value)

    # Manually push required ready_by further out -- a live edit, not a
    # rollover (the new value is still in the future, so
    # _async_update_data's `ready_by <= now_dt` rollover check never fires).
    await coordinator.async_set_ready_by(coordinator.ready_by + timedelta(days=1))

    assert coordinator.opportunistic_ready_by == manual_opportunistic_value
