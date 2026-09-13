"""Regression coverage: once a scheduled session finishes and required_hours
has been fully delivered, the coordinator must not silently reschedule a new
session and bounce hours_remaining back up.

Previously, required_hours was a static target with no memory of hours
already delivered by sessions that had already completed and aged out of
_stored_sessions -- only the *currently active* session's duration offset it
(see coordinator._compute_sessions). The moment a session's `end` passed,
prune_and_classify stopped reporting it as active, required_hours was still
sitting at its original value, and the very next update cycle scheduled a
brand new session from scratch: hours_remaining went 3.5 -> 0 as the session
ran, then instantly back up to 3.5 once it ended.
"""

from datetime import timedelta

import homeassistant.util.dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.wholesale_ev_schedule.const import CONF_DEFAULT_REQUIRED_HOURS, DOMAIN

from .factories import (
    CURRENT_RATES_ENTITY,
    FULL_OPTIONS,
    NEXT_RATES_ENTITY,
    async_setup_wholesale_entry,
    priced_points,
    set_octopus_rate_entity,
)

# Setup-time default required_hours of 0 -- nothing gets scheduled (and
# potentially locked in as "active" immediately at `now`) before the test has
# finished configuring ready_by/required_hours via the live setters.
_ZERO_DEFAULT_HOURS_OPTIONS = {**FULL_OPTIONS, CONF_DEFAULT_REQUIRED_HOURS: 0.0}


def _priced_points(start, count: int, cheap_from_slot: int, cheap_to_slot: int, step_minutes: int = 30) -> list[dict]:
    """Expensive everywhere except one clearly cheapest contiguous window
    [cheap_from_slot, cheap_to_slot), so the scheduler's choice of window is
    deterministic for the test -- unlike uniform pricing, where tie-breaking
    among equally-cheap windows can pick a longer block than the one the test
    expects."""
    prices = [0.05 if cheap_from_slot <= i < cheap_to_slot else 0.50 for i in range(count)]
    return priced_points(start, prices, step_minutes)


async def test_hours_remaining_does_not_bounce_back_up_after_session_completes(hass, freezer):
    freezer.move_to("2026-01-10 22:00:00+00:00")
    now = dt_util.now()

    # A single clearly-cheapest 3.5h window (7 half-hour slots), starting 2h
    # from now rather than immediately -- so it isn't already "active" (and
    # therefore locked in at whatever required_hours the default setup used)
    # by the time this test finishes configuring ready_by/required_hours.
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, _priced_points(now, 48, cheap_from_slot=4, cheap_to_slot=11))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])

    entry = await async_setup_wholesale_entry(hass, options=_ZERO_DEFAULT_HOURS_OPTIONS)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await coordinator.async_set_ready_by(now + timedelta(hours=10))
    await coordinator.async_set_required_hours(3.5)

    assert coordinator.data["sessions"], "a session should have been scheduled"
    session = coordinator.data["sessions"][0]
    session_start = dt_util.parse_datetime(session["start"])
    session_end = dt_util.parse_datetime(session["end"])
    assert session_start > now  # confirms it isn't already active/locked-in
    assert coordinator.data["hours_remaining"] == 3.5

    # Advance to partway through the session -- hours_remaining should count
    # down, same as before this fix.
    freezer.move_to(session_start + timedelta(hours=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert coordinator.data["state"] == "charging"
    assert coordinator.data["hours_remaining"] < 3.5

    # Advance to just after the session has fully ended.
    freezer.move_to(session_end + timedelta(minutes=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    # The requirement has been fully met: no new session should have been
    # scheduled, hours_remaining must stay at 0, and the state must reflect
    # completion rather than bouncing back to "scheduled"/"unschedulable".
    assert coordinator.data["sessions"] == []
    assert coordinator.data["active_slot"] is None
    assert coordinator.data["hours_remaining"] == 0.0
    assert coordinator.data["state"] == "complete"
    assert coordinator.data["desired"] is False

    # One more refresh later, still well before ready_by -- must stay complete
    # rather than drifting back into scheduling a fresh session.
    freezer.tick(timedelta(minutes=30))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert coordinator.data["sessions"] == []
    assert coordinator.data["hours_remaining"] == 0.0
    assert coordinator.data["state"] == "complete"


async def test_delivered_hours_reset_when_required_hours_changed_again(hass, freezer):
    freezer.move_to("2026-01-10 22:00:00+00:00")
    now = dt_util.now()
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, _priced_points(now, 48, cheap_from_slot=4, cheap_to_slot=11))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])

    entry = await async_setup_wholesale_entry(hass, options=_ZERO_DEFAULT_HOURS_OPTIONS)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await coordinator.async_set_ready_by(now + timedelta(hours=10))
    await coordinator.async_set_required_hours(1.0)
    session = coordinator.data["sessions"][0]
    session_end = dt_util.parse_datetime(session["end"])

    freezer.move_to(session_end + timedelta(minutes=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert coordinator.data["state"] == "complete"

    # Bumping required_hours back up (e.g. the user decides they need more
    # charge after all) must schedule fresh hours, not be swallowed by a
    # stale delivered_hours carried over from the previous target. (Not
    # asserting an exact hours_remaining here: real wall-clock time elapses
    # between the setter call and reading coordinator.data, which can shave a
    # few minutes off whatever gets scheduled -- the point of this test is
    # that it's close to the full 2.0h just requested, not ~0 as it would be
    # if delivered_hours had incorrectly carried over from the 1.0h already
    # delivered under the previous required_hours.)
    await coordinator.async_set_required_hours(2.0)
    assert coordinator.data["hours_remaining"] > 1.5
    assert coordinator.data["sessions"]
