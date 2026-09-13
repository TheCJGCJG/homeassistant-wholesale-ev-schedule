"""Opportunistic charging (issue #53) scheduling behavior: the required tier
runs first and completely unchanged, opportunistic only ever fills leftover
capacity beyond it (never stealing a slot required still needs), the live
switch pauses/resumes without losing configuration, and delivered-hours
accounting for the opportunistic tier works the same way the required tier's
already does (see test_required_hours_completion.py / PR #58).
"""

from datetime import timedelta

import homeassistant.util.dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.wholesale_ev_schedule.const import CONF_DEFAULT_REQUIRED_HOURS, DOMAIN

from .factories import (
    CURRENT_RATES_ENTITY,
    NEXT_RATES_ENTITY,
    OPPORTUNISTIC_OPTIONS,
    async_setup_wholesale_entry,
    priced_points,
    set_octopus_rate_entity,
)

# required_hours starts at 0 so nothing schedules (and potentially locks in
# as "active" at `now`) before a test finishes configuring ready_by/
# required_hours -- see test_required_hours_completion.py.
_OPPORTUNISTIC_ZERO_REQUIRED_OPTIONS = {**OPPORTUNISTIC_OPTIONS, CONF_DEFAULT_REQUIRED_HOURS: 0.0}

CHEAP = 0.05
MID = 0.15
EXPENSIVE = 0.50
SLOTS_PER_DAY = 48


def _dips(total_slots: int, ranges: list[tuple[int, int, float]]) -> list[float]:
    """Per-slot price list, EXPENSIVE everywhere except the given
    [start, end, price) ranges."""
    prices = [EXPENSIVE] * total_slots
    for lo, hi, price in ranges:
        for i in range(lo, hi):
            prices[i] = price
    return prices


async def _setup(hass, options=None):
    entry = await async_setup_wholesale_entry(hass, options=options or _OPPORTUNISTIC_ZERO_REQUIRED_OPTIONS)
    return hass.data[DOMAIN][entry.entry_id]


async def test_opportunistic_never_claims_a_slot_required_still_needs(hass, freezer):
    # A single 4h cheap window (slots 4-11) with nothing else nearly as
    # cheap. required_hours (2h) and opportunistic_target_hours (2h)
    # together (4h) exactly consume the whole window -- if opportunistic
    # were allowed to compete for the same slots as required rather than
    # only take what's left over, the two could overlap or the combined
    # total could be wrong.
    freezer.move_to("2026-03-02 20:00:00+00:00")
    now = dt_util.now()
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, priced_points(now, _dips(SLOTS_PER_DAY * 3, [(4, 12, CHEAP)])))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])

    coordinator = await _setup(hass)
    await coordinator.async_set_min_block_hours(0.5)
    await coordinator.async_set_ready_by(now + timedelta(days=2))
    await coordinator.async_set_required_hours(2.0)

    await coordinator.async_set_opportunistic_target_hours(2.0)
    await coordinator.async_set_opportunistic_max_price(10.0)
    await coordinator.async_set_opportunistic_enabled(True)

    sessions = coordinator.data["sessions"]
    required_dts = set()
    opportunistic_dts = set()
    for s in sessions:
        start = dt_util.parse_datetime(s["start"])
        end = dt_util.parse_datetime(s["end"])
        dts = set()
        t = start
        while t < end:
            dts.add(t)
            t += timedelta(minutes=30)
        if s.get("tier") == "opportunistic":
            opportunistic_dts |= dts
        else:
            required_dts |= dts

    assert required_dts, "required tier should have scheduled something"
    assert opportunistic_dts, "opportunistic tier should have scheduled something"
    assert required_dts.isdisjoint(opportunistic_dts), "opportunistic must never claim a required slot"

    total_hours = sum(s["duration_hours"] for s in sessions)
    assert total_hours == pytest.approx(4.0, abs=0.01)


async def test_opportunistic_max_price_independent_of_required_max_price(hass, freezer):
    # required_max_price (default DEFAULT_MAX_PRICE, very loose) would happily
    # accept the MID-priced window, but opportunistic_max_price is set
    # tighter -- opportunistic must only use the genuinely cheap window, not
    # the merely mid-priced one, even though required's own cap wouldn't
    # have minded either.
    freezer.move_to("2026-03-02 20:00:00+00:00")
    now = dt_util.now()
    # slots 4-7: required's cheap window. slots 20-23: MID-priced (too
    # expensive for a tight opportunistic cap, fine for required's loose one).
    set_octopus_rate_entity(
        hass,
        CURRENT_RATES_ENTITY,
        priced_points(now, _dips(SLOTS_PER_DAY * 3, [(4, 8, CHEAP), (20, 24, MID)])),
    )
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])

    coordinator = await _setup(hass)
    await coordinator.async_set_min_block_hours(0.5)
    await coordinator.async_set_ready_by(now + timedelta(days=2))
    await coordinator.async_set_required_hours(2.0)

    await coordinator.async_set_opportunistic_target_hours(2.0)
    await coordinator.async_set_opportunistic_max_price(10.0)  # under MID (15) but over CHEAP (5)
    await coordinator.async_set_opportunistic_enabled(True)

    opportunistic_sessions = [s for s in coordinator.data["sessions"] if s.get("tier") == "opportunistic"]
    # required already took the CHEAP window (2h of it); the only remaining
    # candidate for opportunistic is the MID window, which its own cap
    # excludes -- so opportunistic should find nothing this cycle.
    # opportunistic_hours_remaining reflects what's actually committed (like
    # the required tier's own hours_remaining outside the unschedulable
    # state), so it reads 0.0 here, not the still-unfulfilled 2h target --
    # the un-scheduled target itself is verified directly via
    # opportunistic_target_hours/_delivered_opportunistic_hours below.
    assert opportunistic_sessions == []
    assert coordinator.data["opportunistic_hours_remaining"] == 0.0
    assert coordinator.opportunistic_target_hours == 2.0
    assert coordinator._delivered_opportunistic_hours == 0.0


async def test_opportunistic_target_zero_is_a_no_op(hass, freezer):
    freezer.move_to("2026-03-02 20:00:00+00:00")
    now = dt_util.now()
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, priced_points(now, _dips(SLOTS_PER_DAY * 3, [(4, 12, CHEAP)])))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])

    coordinator = await _setup(hass)
    await coordinator.async_set_min_block_hours(0.5)
    await coordinator.async_set_ready_by(now + timedelta(days=2))
    await coordinator.async_set_required_hours(2.0)
    await coordinator.async_set_opportunistic_enabled(True)
    # opportunistic_target_hours left at its default (0.0).

    sessions = coordinator.data["sessions"]
    assert all(s.get("tier", "required") != "opportunistic" for s in sessions)
    assert coordinator.data["opportunistic_hours_remaining"] == 0.0


async def test_opportunistic_switch_off_suppresses_scheduling_without_clearing_target(hass, freezer):
    freezer.move_to("2026-03-02 20:00:00+00:00")
    now = dt_util.now()
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, priced_points(now, _dips(SLOTS_PER_DAY * 3, [(4, 12, CHEAP)])))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])

    coordinator = await _setup(hass)
    await coordinator.async_set_min_block_hours(0.5)
    await coordinator.async_set_ready_by(now + timedelta(days=2))
    await coordinator.async_set_required_hours(2.0)
    await coordinator.async_set_opportunistic_target_hours(2.0)
    await coordinator.async_set_opportunistic_max_price(10.0)
    await coordinator.async_set_opportunistic_enabled(True)

    assert any(s.get("tier") == "opportunistic" for s in coordinator.data["sessions"])

    await coordinator.async_set_opportunistic_enabled(False)
    assert all(s.get("tier", "required") != "opportunistic" for s in coordinator.data["sessions"])
    # Configuration itself survives the pause.
    assert coordinator.opportunistic_target_hours == 2.0
    assert coordinator.opportunistic_max_price == 10.0

    # Re-enabling resumes with everything intact.
    await coordinator.async_set_opportunistic_enabled(True)
    assert any(s.get("tier") == "opportunistic" for s in coordinator.data["sessions"])


async def test_opportunistic_delivered_hours_accrue_on_completion_and_reset_on_new_target(hass, freezer):
    # Mirrors test_required_hours_completion.py's
    # test_hours_remaining_does_not_bounce_back_up_after_session_completes,
    # but for the opportunistic counter.
    freezer.move_to("2026-03-02 20:00:00+00:00")
    now = dt_util.now()
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, priced_points(now, _dips(SLOTS_PER_DAY * 3, [(4, 12, CHEAP)])))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])

    coordinator = await _setup(hass)
    await coordinator.async_set_min_block_hours(0.5)
    await coordinator.async_set_ready_by(now + timedelta(days=2))
    await coordinator.async_set_required_hours(2.0)
    await coordinator.async_set_opportunistic_target_hours(2.0)
    await coordinator.async_set_opportunistic_max_price(10.0)
    await coordinator.async_set_opportunistic_enabled(True)

    opportunistic_sessions = [s for s in coordinator.data["sessions"] if s.get("tier") == "opportunistic"]
    assert opportunistic_sessions
    session = opportunistic_sessions[0]
    session_end = dt_util.parse_datetime(session["end"])

    freezer.move_to(session_end + timedelta(minutes=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert coordinator._delivered_opportunistic_hours == pytest.approx(2.0, abs=0.01)
    # Fully delivered -- no new opportunistic session should appear this cycle.
    assert all(s.get("tier", "required") != "opportunistic" for s in coordinator.data["sessions"])
    assert coordinator.data["opportunistic_hours_remaining"] == 0.0

    # A new target resets the counter, same as required_hours does.
    await coordinator.async_set_opportunistic_target_hours(1.0)
    assert coordinator._delivered_opportunistic_hours == 0.0
