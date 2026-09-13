"""Multi-day ready_by coverage: the scenario behind the delivered-hours fix
(test_required_hours_completion.py) but stretched across a week-long horizon
with several non-contiguous sessions, price data changing mid-flight, and
sessions getting reshuffled after some have already completed.

The property under test throughout: total hours actually delivered by
ready_by must equal required_hours (not more, not less), regardless of how
many separate blocks that gets split across or how many times the remaining
(not-yet-occurred) portion gets replanned -- as long as max_price doesn't
prevent it. A dedicated pair of tests at the bottom confirms max_price is
enforced per-block (a block's own average, not the whole schedule, not each
individual slot) and that a genuinely-blocking cap correctly leaves the
requirement unmet rather than silently faking completion.
"""

from datetime import timedelta

import homeassistant.util.dt as dt_util
import pytest
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

# See test_required_hours_completion.py -- required_hours starts at 0 so
# nothing schedules (and potentially locks in as "active" at `now`) before a
# test finishes configuring ready_by/required_hours.
_ZERO_DEFAULT_HOURS_OPTIONS = {**FULL_OPTIONS, CONF_DEFAULT_REQUIRED_HOURS: 0.0}

CHEAP = 0.05
EXPENSIVE = 0.50
SLOTS_PER_DAY = 48  # 30-minute slots


def _dips(total_slots: int, cheap_ranges: list[tuple[int, int]]) -> list[float]:
    """Per-slot price list, EXPENSIVE everywhere except the given
    [start, end) cheap ranges -- lets a test lay out several separate cheap
    dips at chosen points across a multi-day horizon."""
    prices = [EXPENSIVE] * total_slots
    for lo, hi in cheap_ranges:
        for i in range(lo, hi):
            prices[i] = CHEAP
    return prices


async def _advance_and_refresh(hass, freezer, target_dt):
    freezer.move_to(target_dt)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def _run_to_ready_by(hass, freezer, coordinator, ready_by, step=timedelta(hours=2)):
    """Advance the clock in steps up to (but not past) ready_by, refreshing at
    each step -- exercising every intermediate recompute along the way, not
    just a single jump to the end.

    Deliberately stops short of ready_by itself: once now_dt reaches it, the
    coordinator rolls ready_by forward to the next standing daily occurrence
    (see _async_update_data) and resets delivered_hours for that fresh cycle
    -- correct behaviour for "charge N hours every day by 7am", but it would
    make a single one-off requirement in these tests look unschedulable for
    reasons unrelated to what's being tested here.
    """
    now = dt_util.now()
    t = now + step
    while t < ready_by:
        await _advance_and_refresh(hass, freezer, t)
        t += step
    if dt_util.now() < ready_by - timedelta(minutes=1):
        await _advance_and_refresh(hass, freezer, ready_by - timedelta(minutes=1))


@pytest.mark.parametrize("days_out", [1, 3, 7])
async def test_delivers_full_required_hours_by_ready_by_across_varying_horizons(hass, freezer, days_out):
    # Static prices (no reshuffle) at three different horizon lengths -- a
    # short 1-day window with one dip, and longer 3/7-day windows with several
    # scattered dips the scheduler must pick from to total the requirement.
    freezer.move_to("2026-02-02 20:00:00+00:00")
    now = dt_util.now()
    total_slots = SLOTS_PER_DAY * (days_out + 1)

    # One cheap dip per day, 3h (6 slots) each, spread through the horizon.
    cheap_ranges = [(SLOTS_PER_DAY * d + 10, SLOTS_PER_DAY * d + 16) for d in range(days_out)]
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, priced_points(now, _dips(total_slots, cheap_ranges)))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])

    entry = await async_setup_wholesale_entry(hass, options=_ZERO_DEFAULT_HOURS_OPTIONS)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    ready_by = now + timedelta(days=days_out)
    await coordinator.async_set_ready_by(ready_by)
    required_hours = min(3.0 * days_out, 3.0 * days_out)  # one full dip's worth per day, well within budget
    await coordinator.async_set_required_hours(required_hours)

    assert coordinator.data["sessions"], "should be schedulable: plenty of cheap capacity within the horizon"

    await _run_to_ready_by(hass, freezer, coordinator, ready_by)

    assert coordinator.data["state"] == "complete"
    assert coordinator.data["hours_remaining"] == 0.0
    assert coordinator._delivered_hours == pytest.approx(required_hours, abs=0.01)


async def test_multi_session_week_survives_reshuffle_after_first_session_completes(hass, freezer):
    # A week-long horizon, required_hours split across multiple separate cheap
    # dips. After the FIRST dip's session has already run and completed, new
    # price data arrives (as if a forecast/actuals update landed) that changes
    # which slots are cheapest for the remaining days -- the not-yet-occurred
    # portion of the plan must reshuffle to the new cheap slots, but the
    # total delivered by ready_by must still land exactly on required_hours,
    # with the already-completed session's contribution preserved untouched.
    freezer.move_to("2026-02-02 20:00:00+00:00")
    now = dt_util.now()
    ready_by = now + timedelta(days=7)
    total_slots = SLOTS_PER_DAY * 8

    # Day 0: a clean 2h dip starting soon, so it becomes the first session.
    # Days 1-6: a 2h dip each, giving 12h of future capacity well beyond the
    # 6h actually required, so the optimizer has real freedom to choose
    # differently once prices change.
    initial_cheap_ranges = [(10, 14)] + [(SLOTS_PER_DAY * d + 20, SLOTS_PER_DAY * d + 24) for d in range(1, 7)]
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, priced_points(now, _dips(total_slots, initial_cheap_ranges)))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])

    entry = await async_setup_wholesale_entry(hass, options=_ZERO_DEFAULT_HOURS_OPTIONS)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await coordinator.async_set_ready_by(ready_by)
    # Each dip is 2h -- below the 4h DEFAULT_MIN_BLOCK_HOURS, which would
    # otherwise filter every one of them out as too short to count as a
    # valid block. Not what's under test here (that's covered by
    # test_min_block_hours_relaxes_when_longer_than_required and
    # test_multi_block_scheduling_splits_across_separate_cheap_dips), so
    # relax it.
    await coordinator.async_set_min_block_hours(0.5)
    await coordinator.async_set_required_hours(6.0)

    assert coordinator.data["sessions"]
    first_session = min(coordinator.data["sessions"], key=lambda s: s["start"])
    first_start = dt_util.parse_datetime(first_session["start"])
    first_end = dt_util.parse_datetime(first_session["end"])
    assert first_start > now

    # Run past the first session's completion.
    await _advance_and_refresh(hass, freezer, first_start + timedelta(minutes=30))
    assert coordinator.data["state"] == "charging"
    await _advance_and_refresh(hass, freezer, first_end + timedelta(minutes=5))
    assert coordinator.data["state"] in ("scheduled", "complete")
    delivered_after_first = coordinator._delivered_hours
    assert delivered_after_first == pytest.approx(2.0, abs=0.01)

    # New price data arrives: the day-1 dip that was cheapest before is now
    # expensive, and a brand new, deeper dip opens up on day 4 instead. Only
    # slots still in the future can move -- the completed day-0 session is
    # untouched by construction (deduplicate_and_sort_prices drops elapsed
    # slots before the optimizer ever sees them).
    reshuffled_cheap_ranges = [(SLOTS_PER_DAY * 4 + 30, SLOTS_PER_DAY * 4 + 34)] + [
        (SLOTS_PER_DAY * d + 20, SLOTS_PER_DAY * d + 24) for d in range(2, 7) if d != 4
    ]
    now2 = dt_util.now()
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, priced_points(now, _dips(total_slots, reshuffled_cheap_ranges)))
    await coordinator.async_refresh()

    # The completed session's contribution must survive the reshuffle exactly.
    assert coordinator._delivered_hours == pytest.approx(delivered_after_first, abs=0.01)
    assert coordinator.data["sessions"], "remaining 4h should still be schedulable from the new dips"

    await _run_to_ready_by(hass, freezer, coordinator, ready_by)

    assert coordinator.data["state"] == "complete"
    assert coordinator.data["hours_remaining"] == 0.0
    # Total across the completed-and-preserved first session plus whatever
    # the reshuffled remainder landed on must equal the full requirement --
    # not double-counted, not short.
    assert coordinator._delivered_hours == pytest.approx(6.0, abs=0.01)
    assert now2 > now  # sanity: the reshuffle really did happen after time passed


async def test_max_price_applies_per_block_average_not_globally(hass, freezer):
    # Two required blocks: one whose average stays under max_price despite
    # containing a pricier slot, and a separate, genuinely-too-expensive
    # block elsewhere. Confirms max_price is checked per-block (its own
    # average), not against a single expensive slot in isolation and not as
    # some whole-schedule aggregate -- consistent with scheduler.py's
    # documented per-window check (_find_optimal_slots_greedy /
    # _min_cost_for_run), exercised here through the coordinator across a
    # multi-day horizon.
    #
    # Note on units: octopus_rate_points/priced_points prices get multiplied
    # by CONF_RATE_UNIT_MULTIPLIER (100, see const.py DEFAULT_RATE_UNIT_MULTIPLIER)
    # before becoming raw_price, but async_set_max_price sets the coordinator's
    # max_price directly with no such conversion -- so max_price must be given
    # in that already-multiplied scale to compare like-for-like (same
    # convention as test_max_price_below_all_available_rates_is_unschedulable
    # in test_tolerances_and_reload.py).
    freezer.move_to("2026-02-02 20:00:00+00:00")
    now = dt_util.now()
    total_slots = SLOTS_PER_DAY * 3

    prices = [EXPENSIVE] * total_slots
    # Block A (day 0, slots 10-13): [0.05, 0.05, 0.20, 0.05] -> raw avg 8.75
    # (post-multiplier), under a cap of 10 despite one pricier slot mixed in.
    prices[10:14] = [0.05, 0.05, 0.20, 0.05]
    # Block B (day 1, slots 58-61): flat 0.30 -> raw avg 30, well over the
    # cap -- must never be used regardless of how much required_hours needs it.
    prices[58:62] = [0.30, 0.30, 0.30, 0.30]
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, priced_points(now, prices))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])

    entry = await async_setup_wholesale_entry(hass, options=_ZERO_DEFAULT_HOURS_OPTIONS)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await coordinator.async_set_ready_by(now + timedelta(days=3))
    await coordinator.async_set_max_price(10.0)
    await coordinator.async_set_min_block_hours(0.5)
    await coordinator.async_set_required_hours(2.0)  # only block A (2h) can satisfy this under the cap

    assert coordinator.data["sessions"]
    for session in coordinator.data["sessions"]:
        assert session["avg_price"] <= 10.0
    total_scheduled_hours = sum(s["duration_hours"] for s in coordinator.data["sessions"])
    assert total_scheduled_hours == pytest.approx(2.0, abs=0.01)


async def test_max_price_that_blocks_enough_capacity_leaves_requirement_unmet(hass, freezer):
    # The counter-case to every "delivers full hours" test above: if
    # max_price genuinely excludes enough cheap capacity, the total delivered
    # by ready_by must fall short (state stays unschedulable / hours_remaining
    # stays positive) -- this must never silently report completion it didn't
    # actually achieve. Same post-multiplier units note as the test above.
    freezer.move_to("2026-02-02 20:00:00+00:00")
    now = dt_util.now()
    total_slots = SLOTS_PER_DAY * 3

    # Only 2h of genuinely cheap (under-cap) capacity exists in the whole
    # 3-day horizon; everything else is priced over the cap.
    prices = [EXPENSIVE] * total_slots
    prices[10:14] = [0.05] * 4
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, priced_points(now, prices))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])

    entry = await async_setup_wholesale_entry(hass, options=_ZERO_DEFAULT_HOURS_OPTIONS)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    ready_by = now + timedelta(days=3)
    await coordinator.async_set_ready_by(ready_by)
    await coordinator.async_set_max_price(10.0)
    await coordinator.async_set_required_hours(4.0)  # more than the 2h actually available under the cap

    await _run_to_ready_by(hass, freezer, coordinator, ready_by)

    # Never silently "complete" on less than what was actually asked for.
    assert coordinator.data["state"] != "complete"
    assert coordinator.data["hours_remaining"] > 0.0
    assert coordinator._delivered_hours < 4.0
