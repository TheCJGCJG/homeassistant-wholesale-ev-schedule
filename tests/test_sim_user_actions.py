"""What a user does mid-day -- changing hours, moving the deadline, capping the price, forcing the
charger, boosting, stopping, resetting -- simulated over real time with the invariants checked at
every step. Each test states what a user would reasonably expect to happen."""

from datetime import datetime, timedelta

import pytest

from .sim import UTC, PriceWorld, Sim

START = "2026-03-02 12:00:00+00:00"
# Overnight trough 00:00-05:30Z; ready_by 07:00Z. Tonight's rates are known from 15:00Z.
WORLD = dict(seed=1)


async def _sim(hass, freezer, **kwargs):
    kwargs.setdefault("start", START)
    kwargs.setdefault("required_hours", 3.0)
    kwargs.setdefault("world", PriceWorld(**WORLD))
    return await Sim.create(hass, freezer, **kwargs)


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 3, day, hour, minute, tzinfo=UTC)


async def _edit(sim: Sim, coro) -> None:
    await sim.act(coro)
    await sim.step(5)


def _cycle_hours(sim: Sim, ready_by: datetime) -> float:
    return sim.charged_minutes.get(ready_by.isoformat(), 0) / 60


# ---------------------------------------------------------------------------
# Changing how much
# ---------------------------------------------------------------------------


async def test_raising_the_requirement_mid_charge_adds_the_extra_hours(hass, freezer):
    sim = await _sim(hass, freezer)
    await sim.run_to(_at(3, 1, 30))
    assert sim.data["state"] == "charging"

    await _edit(sim, sim.coordinator.async_set_required_hours(5.0))
    await sim.run_to(_at(3, 8))

    assert _cycle_hours(sim, _at(3, 7)) == 5.0


async def test_lowering_the_requirement_mid_charge_stops_charging_once_it_is_met(hass, freezer):
    """Asked for 3h, an hour in, the user drops it to 1h: the charger should stop at the new
    target, not run on to the end of the originally planned block."""
    sim = await _sim(hass, freezer)
    await sim.run_to(_at(3, 1, 30))
    assert sim.data["state"] == "charging"
    on_so_far = _cycle_hours(sim, _at(3, 7))

    await _edit(sim, sim.coordinator.async_set_required_hours(1.0))
    await sim.run_to(_at(3, 8))

    total = _cycle_hours(sim, _at(3, 7))
    assert total <= max(on_so_far, 1.0) + 0.5, f"charged {total}h after being told 1h (had {on_so_far}h by then)"


async def test_setting_hours_to_zero_goes_idle_immediately_even_mid_charge(hass, freezer):
    sim = await _sim(hass, freezer)
    await sim.run_to(_at(3, 1, 30))

    await _edit(sim, sim.coordinator.async_set_required_hours(0.0))

    assert sim.data["state"] == "idle" and sim.data["desired"] is False
    await sim.run_to(_at(3, 8))
    assert {t.state for t in sim.timeline if t.at > _at(3, 1, 40)} == {"idle"}


async def test_asking_for_more_hours_than_fit_before_the_deadline_is_unschedulable_not_a_silent_shortfall(
    hass, freezer
):
    sim = await _sim(hass, freezer, start="2026-03-02 22:00:00+00:00", required_hours=1.0)
    await _edit(sim, sim.coordinator.async_set_required_hours(24.0))

    assert sim.data["state"] == "unschedulable"
    assert "less than the 24.0h required" in sim.data["error_reason"]
    assert sim.data["desired"] is False


# ---------------------------------------------------------------------------
# Moving the deadline
# ---------------------------------------------------------------------------


async def test_pulling_the_deadline_earlier_still_meets_it(hass, freezer):
    sim = await _sim(hass, freezer)
    await sim.run_to(_at(3, 1))
    deadline = _at(3, 4)
    await _edit(sim, sim.coordinator.async_set_ready_by(deadline))
    await sim.run_to(_at(3, 5))

    # Everything the user asked for is on the car by the new, earlier deadline.
    assert sim.on_minutes(_at(2, 12), deadline) == 180


async def test_pushing_the_deadline_later_gives_the_scheduler_room_but_still_delivers_once(hass, freezer):
    sim = await _sim(hass, freezer)
    await _edit(sim, sim.coordinator.async_set_ready_by(_at(4, 7)))  # a day later than the default
    await sim.run_to(_at(4, 9))

    assert sum(m for m in sim.charged_minutes.values()) / 60 == 3.0


async def test_a_deadline_in_the_past_rolls_forward_to_the_next_morning_instead_of_breaking(hass, freezer):
    sim = await _sim(hass, freezer)
    await _edit(sim, sim.coordinator.async_set_ready_by(sim.now - timedelta(hours=1)))

    assert sim.coordinator.ready_by == _at(3, 7)
    assert sim.data["state"] in ("scheduled", "charging")
    await sim.run_to(_at(3, 8))
    assert _cycle_hours(sim, _at(3, 7)) == 3.0


# ---------------------------------------------------------------------------
# Price cap
# ---------------------------------------------------------------------------


async def test_a_price_cap_below_every_rate_is_unschedulable_and_recovers_when_raised(hass, freezer):
    sim = await _sim(hass, freezer)
    await sim.run_to(_at(2, 18))
    await _edit(sim, sim.coordinator.async_set_max_price(1.0))
    assert sim.data["state"] == "unschedulable" and sim.data["desired"] is False
    assert "max_price" in sim.data["error_reason"]

    await _edit(sim, sim.coordinator.async_set_max_price(100.0))

    assert sim.data["state"] in ("scheduled", "charging")
    await sim.run_to(_at(3, 8))
    assert _cycle_hours(sim, _at(3, 7)) == 3.0


async def test_a_cap_that_only_allows_the_cheapest_hours_picks_them(hass, freezer):
    sim = await _sim(hass, freezer, max_price=12.0)
    await sim.run_to(_at(3, 8))

    cycle = sim.timeline[-1].ready_by
    on_times = [t.at for t in sim.timeline if t.desired and _at(3, 0) <= t.at <= _at(3, 7)]
    assert on_times and all(_at(3, 0) <= t <= _at(3, 5, 35) for t in on_times)  # only inside the cheap trough
    assert _cycle_hours(sim, _at(3, 7)) == 3.0 and cycle is not None


async def test_when_the_cap_leaves_too_little_cheap_time_the_requirement_is_reported_unmet(hass, freezer):
    sim = await _sim(hass, freezer, required_hours=8.0, max_price=12.0)  # trough is 5.5h
    await sim.run_to(_at(2, 20))

    assert sim.data["state"] == "unschedulable"
    assert sim.data["desired"] is False
    assert sim.data["hours_remaining"] > 0


async def test_hours_remaining_in_unschedulable_state_accounts_for_what_was_already_delivered(hass, freezer):
    """3h wanted; charging starts, then the cap is dropped so nothing more qualifies. What is
    still outstanding is what hasn't been delivered, not the full 3h again."""
    sim = await _sim(hass, freezer)
    await sim.run_to(_at(3, 2))
    delivered = sim.coordinator._delivered_hours
    await _edit(sim, sim.coordinator.async_set_max_price(1.0))
    await sim.run_to(_at(3, 4, 30))  # the in-progress block has ended by now

    assert sim.data["state"] == "unschedulable"
    assert sim.data["hours_remaining"] == pytest.approx(3.0 - sim.coordinator._delivered_hours)
    assert sim.coordinator._delivered_hours >= delivered


# ---------------------------------------------------------------------------
# Manual override
# ---------------------------------------------------------------------------


async def test_force_on_charges_regardless_of_the_plan_and_auto_restores_it(hass, freezer):
    sim = await _sim(hass, freezer)
    await sim.run_to(_at(2, 14))
    assert sim.data["desired"] is False

    await _edit(sim, sim.coordinator.async_set_charge_override("force_on"))
    await sim.run(60)
    assert sim.data["desired"] is True and sim.data["state"] != "charging"  # plan unchanged, output forced

    await _edit(sim, sim.coordinator.async_set_charge_override("auto"))
    assert sim.data["desired"] is False


async def test_force_off_blocks_charging_through_a_planned_session_then_auto_resumes_the_plan(hass, freezer):
    sim = await _sim(hass, freezer)
    await sim.run_to(_at(3, 0, 30))
    assert sim.data["desired"] is True
    await _edit(sim, sim.coordinator.async_set_charge_override("force_off"))
    await sim.run(60)
    assert sim.data["desired"] is False and sim.data["state"] == "charging"  # the plan still thinks it is charging

    await _edit(sim, sim.coordinator.async_set_charge_override("auto"))

    assert sim.data["desired"] is True


# ---------------------------------------------------------------------------
# Boost
# ---------------------------------------------------------------------------


async def test_a_boost_runs_for_exactly_the_hours_asked_then_the_plan_resumes(hass, freezer):
    sim = await _sim(hass, freezer)
    await sim.run_to(_at(2, 14))
    assert sim.data["desired"] is False
    await sim.act(sim.coordinator.async_start_boost(1.5))
    boost_start = sim.now
    assert sim.data["state"] == "boosting"
    while sim.data["state"] == "boosting":
        assert sim.data["desired"] is True
        await sim.step(5)

    assert sim.now - boost_start == timedelta(minutes=90)
    assert sim.data["state"] == "scheduled"  # back to the normal plan for tonight
    await sim.run_to(_at(3, 8))
    assert _cycle_hours(sim, _at(3, 7)) == 3.0 + 1.5  # the planned 3h, plus the 1.5h boost on top


async def test_cancelling_a_boost_returns_to_the_plan_immediately(hass, freezer):
    sim = await _sim(hass, freezer)
    await sim.run_to(_at(2, 14))
    await sim.act(sim.coordinator.async_start_boost(4.0))
    await sim.step(5)
    assert sim.data["state"] == "boosting"

    await _edit(sim, sim.coordinator.async_cancel_boost())

    assert sim.data["state"] == "scheduled" and sim.data["desired"] is False


async def test_a_boost_across_the_autumn_clock_change_lasts_the_real_hours_asked(hass, freezer):
    sim = await _sim(hass, freezer, start="2026-10-24 22:00:00+00:00", tz="Europe/London", required_hours=1.0)
    # 00:30Z on the 25th is 01:30 BST; the clocks fall back at 01:00Z. A 2h boost is 2 real hours.
    await sim.run_to(datetime(2026, 10, 25, 0, 30, tzinfo=UTC))
    await sim.act(sim.coordinator.async_start_boost(2.0))
    started = sim.now
    await sim.step(5)

    while sim.data["state"] == "boosting":
        await sim.step(5)

    assert sim.now - started == timedelta(hours=2, minutes=5) or sim.now - started == timedelta(hours=2)


async def test_force_off_beats_a_boost(hass, freezer):
    sim = await _sim(hass, freezer)
    await sim.act(sim.coordinator.async_start_boost(1.0))
    await sim.act(sim.coordinator.async_set_charge_override("force_off"))
    await sim.step(5)

    assert sim.data["state"] == "boosting" and sim.data["desired"] is False


# ---------------------------------------------------------------------------
# Stop and Reset
# ---------------------------------------------------------------------------


async def test_stop_mid_session_ends_charging_clears_the_plan_and_keeps_the_deadline(hass, freezer):
    sim = await _sim(hass, freezer)
    await sim.run_to(_at(3, 1, 30))
    deadline = sim.coordinator.ready_by

    await _edit(sim, sim.coordinator.async_stop())

    assert sim.data["state"] == "idle" and sim.data["desired"] is False and sim.data["sessions"] == []
    assert sim.coordinator.ready_by == deadline and sim.coordinator.required_hours == 0.0
    await sim.run_to(_at(3, 8))
    assert {t.state for t in sim.timeline if t.at > _at(3, 1, 45)} == {"idle"}


async def test_after_a_stop_setting_hours_again_plans_the_remaining_window(hass, freezer):
    sim = await _sim(hass, freezer)
    await sim.run_to(_at(3, 1, 30))
    await _edit(sim, sim.coordinator.async_stop())
    await sim.run_to(_at(3, 2, 30))

    await _edit(sim, sim.coordinator.async_set_required_hours(2.0))

    assert sim.data["state"] in ("scheduled", "charging")
    await sim.run_to(_at(3, 8))
    assert _cycle_hours(sim, _at(3, 7)) <= 2.0 + 1.5  # what ran before the stop plus the new request


async def test_reset_restores_every_default_and_rolls_the_deadline_to_the_next_morning(hass, freezer):
    sim = await _sim(hass, freezer)
    c = sim.coordinator
    sim.setting_edits = True
    await c.async_set_gamble_tolerance(5.0)
    await c.async_set_max_price(33.0)
    await c.async_set_min_block_hours(2.0)
    await c.async_set_charge_override("force_on")
    await c.async_set_optimization_algorithm("optimal")
    await c.async_set_assumed_charge_kwh(11.0)
    await c.async_set_ready_by(_at(6, 9))

    await _edit(sim, c.async_reset())

    assert (c.gamble_tolerance, c.max_price, c.min_block_hours) == (50.0, 20.0, 4.0)
    assert c.charge_override == "auto" and c.optimization_algorithm == "greedy" and c.assumed_charge_kwh == 7.0
    assert c.ready_by == _at(3, 7)
    assert c.required_hours == 0.0  # the harness's configured default for this entry


# ---------------------------------------------------------------------------
# Algorithm and tuning changes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("algorithm", ["greedy", "optimal", "hybrid"])
async def test_each_algorithm_delivers_the_requirement_through_a_multi_day_run(hass, freezer, algorithm):
    sim = await _sim(hass, freezer, min_block_hours=1.0)
    sim.setting_edits = True
    await sim.coordinator.async_set_optimization_algorithm(algorithm)
    await sim.run(60 * 24 * 3, step=10)

    full = [m for _, m in sorted(sim.charged_minutes.items())][:-1]
    assert full and all(m == 180 for m in full), sim.charged_minutes


async def test_switching_algorithm_mid_plan_keeps_a_valid_plan(hass, freezer):
    sim = await _sim(hass, freezer)
    await sim.run_to(_at(2, 18))
    for algorithm in ("optimal", "hybrid", "greedy"):
        await _edit(sim, sim.coordinator.async_set_optimization_algorithm(algorithm))
        assert sim.data["state"] in ("scheduled", "charging")
    await sim.run_to(_at(3, 8))
    assert _cycle_hours(sim, _at(3, 7)) == 3.0


@pytest.mark.parametrize("gamble", [0.0, 25.0, 100.0])
async def test_any_gamble_tolerance_still_delivers(hass, freezer, gamble):
    sim = await _sim(hass, freezer)
    sim.setting_edits = True
    await sim.coordinator.async_set_gamble_tolerance(gamble)
    await sim.run_to(_at(3, 8))

    assert _cycle_hours(sim, _at(3, 7)) == 3.0


@pytest.mark.parametrize("min_block", [0.0, 0.5, 1.0, 2.0, 3.0, 4.0, 6.0])
async def test_any_min_block_length_still_delivers_or_reports_why_not(hass, freezer, min_block):
    sim = await _sim(hass, freezer, min_block_hours=min_block)
    await sim.run_to(_at(3, 8))

    assert _cycle_hours(sim, _at(3, 7)) == 3.0


async def test_changing_the_assumed_kwh_updates_the_cost_estimate_without_touching_the_plan(hass, freezer):
    sim = await _sim(hass, freezer)
    await sim.run_to(_at(2, 18))
    plan = sim.data["sessions"]
    cost = lambda: float(hass.states.get("sensor.wholesale_ev_schedule_next_slot_estimated_cost").state)  # noqa: E731
    before = cost()

    await _edit(sim, sim.coordinator.async_set_assumed_charge_kwh(14.0))

    assert sim.data["sessions"] == plan
    assert cost() == pytest.approx(before * 2, rel=1e-6)


# ---------------------------------------------------------------------------
# An in-progress block must follow what the user changes after it started
# ---------------------------------------------------------------------------


async def _one_long_block(hass, freezer):
    """A 2h minimum block gives a single 3-hour session, 00:00-03:00Z; stopped 40 minutes in."""
    sim = await _sim(hass, freezer, min_block_hours=2.0)
    await sim.run_to(_at(3, 0, 40))
    assert [(s["start"][11:16], s["end"][11:16]) for s in sim.data["sessions"]] == [("00:00", "03:00")]
    assert sim.data["state"] == "charging"
    return sim


async def test_lowering_the_requirement_shortens_the_block_that_is_already_running(hass, freezer):
    sim = await _one_long_block(hass, freezer)

    await _edit(sim, sim.coordinator.async_set_required_hours(1.0))
    await sim.run_to(_at(3, 4))

    assert sim.on_minutes(_at(3, 0), _at(3, 4)) == 60  # the hour asked for, not the planned three


async def test_lowering_the_requirement_below_what_has_already_run_stops_at_once(hass, freezer):
    sim = await _one_long_block(hass, freezer)

    await _edit(sim, sim.coordinator.async_set_required_hours(0.5))

    assert sim.data["desired"] is False and sim.data["state"] == "complete"
    await sim.run_to(_at(3, 4))
    assert sim.on_minutes(_at(3, 0), _at(3, 4)) <= 45  # ran 40 minutes before the change


async def test_a_stopped_block_counts_as_delivered_so_it_is_not_scheduled_again(hass, freezer):
    sim = await _one_long_block(hass, freezer)

    await _edit(sim, sim.coordinator.async_set_required_hours(0.5))
    await sim.run_to(_at(3, 6, 55))

    assert sim.coordinator._delivered_hours == 0.5
    assert sim.on_minutes(_at(3, 0, 50), _at(3, 7)) == 0


async def test_raising_the_requirement_again_after_a_cut_resumes_charging(hass, freezer):
    sim = await _one_long_block(hass, freezer)
    await _edit(sim, sim.coordinator.async_set_required_hours(0.5))
    await sim.run_to(_at(3, 1, 30))
    assert sim.data["desired"] is False

    await _edit(sim, sim.coordinator.async_set_required_hours(3.0))
    await sim.run_to(_at(3, 8))

    # Setting the hours is a fresh request by design (issue #57: so asking for N hours again after
    # completing N charges again), so the new 3h is planned in full on top of the 40 minutes that ran.
    assert sim.on_minutes(_at(3, 0), _at(3, 7)) == 40 + 180


async def test_setting_the_hours_again_is_a_fresh_request_even_after_some_have_been_delivered(hass, freezer):
    """Pinned on purpose: delivered hours do not carry over when the target is set, so asking for
    3h again after 2h have run charges a further 3h. (Without this, re-requesting the same hours
    after a completed charge would do nothing.) If this ever changes it should be a deliberate
    decision, not a side effect."""
    sim = await _sim(hass, freezer, min_block_hours=0.5)
    await sim.run_to(_at(3, 3, 30))
    ran = sim.on_minutes(_at(3, 0), _at(3, 3, 30))
    assert ran >= 120

    await _edit(sim, sim.coordinator.async_set_required_hours(3.0))
    await sim.run_to(_at(3, 8))

    assert sim.on_minutes(_at(3, 0), _at(3, 8)) == ran + 180


async def test_pulling_the_deadline_earlier_cuts_the_block_that_is_running_at_the_new_deadline(hass, freezer):
    sim = await _one_long_block(hass, freezer)

    await _edit(sim, sim.coordinator.async_set_ready_by(_at(3, 2)))
    await sim.run_to(_at(3, 2, 30))

    assert sim.on_minutes(_at(3, 0), _at(3, 2)) == 120
    assert sim.on_minutes(_at(3, 2), _at(3, 2, 30)) == 0
