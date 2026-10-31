"""Which price sources exist, when they publish, and what happens when they go away -- simulated
over real time. The market is `sim.PriceWorld`: cheap overnight, dear 16:00-19:00, Agile rates
published for the next day at ~15:00Z, a forecast that is only roughly right beyond what is
published, and a day-ahead estimate that appears at ~10:00Z and is final by ~15:00Z."""

from datetime import datetime, timedelta

import pytest

from custom_components.wholesale_ev_schedule.scheduler import parse_dt

from .factories import FULL_OPTIONS, short_range_options
from .sim import UTC, PriceWorld, Sim


def _at(day, hour, minute=0):
    return datetime(2026, 3, day, hour, minute, tzinfo=UTC)


async def _sim(hass, freezer, feeds, start="2026-03-02 12:00:00+00:00", options=None, **kwargs):
    kwargs.setdefault("world", PriceWorld(seed=1))
    kwargs.setdefault("required_hours", 3.0)
    return await Sim.create(hass, freezer, start=start, feeds=feeds, options=options, **kwargs)


def _cycle_hours(sim, ready_by):
    return sim.charged_minutes.get(ready_by.isoformat(), 0) / 60


# ---------------------------------------------------------------------------
# What each extra source buys you
# ---------------------------------------------------------------------------


async def test_with_only_actual_rates_the_plan_can_only_use_what_is_published_then_moves_to_the_night(hass, freezer):
    """Before 15:00Z tomorrow's rates do not exist, so the only candidates are today's remaining
    slots; once they are published the unspent requirement moves to the cheap night."""
    sim = await _sim(hass, freezer, feeds=("actual",))
    assert all(parse_dt(s["end"]) <= _at(2, 23) for s in sim.data["sessions"])  # nothing known beyond today
    assert sim.data["state"] in ("scheduled", "charging")

    await sim.run_to(_at(2, 16))

    assert all(parse_dt(s["start"]) >= _at(2, 23) for s in sim.data["sessions"] if parse_dt(s["start"]) > sim.now)
    await sim.run_to(_at(3, 8))
    assert _cycle_hours(sim, _at(3, 7)) == 3.0


async def test_with_a_forecast_nothing_is_charged_before_the_cheap_night(hass, freezer):
    sim = await _sim(hass, freezer, feeds=("actual", "forecast"))
    await sim.run_to(_at(3, 8))

    assert sim.on_minutes(_at(2, 12), _at(2, 23)) == 0
    assert _cycle_hours(sim, _at(3, 7)) == 3.0
    assert sim.cost < 3 * 15  # bought in the overnight trough, not the afternoon


async def test_the_plan_for_tonight_exists_hours_before_the_rates_do(hass, freezer):
    sim = await _sim(hass, freezer, feeds=("actual", "forecast"))
    await sim.run(60)

    assert sim.data["state"] == "scheduled"
    assert all(parse_dt(s["start"]) >= _at(3, 0) for s in sim.data["sessions"])  # already aiming at the night


async def test_sessions_planned_on_a_forecast_are_flagged_estimated_until_the_real_rates_land(hass, freezer):
    sim = await _sim(hass, freezer, feeds=("actual", "forecast"))
    await sim.run_to(_at(2, 14))
    assert {s["estimated"] for s in sim.data["sessions"]} == {True}
    assert all(s["confidence"] < 100 for s in sim.data["sessions"])

    await sim.run_to(_at(2, 16))

    assert {s["estimated"] for s in sim.data["sessions"]} == {False}
    assert all(s["confidence"] == 100 for s in sim.data["sessions"])


async def test_a_day_ahead_estimate_firms_up_the_plan_hours_before_the_rates_publish(hass, freezer):
    options = short_range_options(FULL_OPTIONS)
    sim = await _sim(
        hass, freezer, feeds=("actual", "forecast", "short_range"), options=options, start="2026-03-02 09:00:00+00:00"
    )
    await sim.run_to(_at(2, 9, 30))
    long_range_confidence = {s["confidence"] for s in sim.data["sessions"]}

    await sim.run_to(_at(2, 11))
    short_range_confidence = {s["confidence"] for s in sim.data["sessions"]}

    assert short_range_confidence == {95.0}  # the 95% default for estimates
    assert max(long_range_confidence) < 95.0  # a >24h-out forecast is trusted less than the day-ahead estimate
    assert {s["estimated"] for s in sim.data["sessions"]} == {True}

    await sim.run_to(_at(2, 16))
    assert {s["estimated"] for s in sim.data["sessions"]} == {False}
    await sim.run_to(_at(3, 8))
    assert _cycle_hours(sim, _at(3, 7)) == 3.0


async def test_the_short_range_estimate_never_overrides_published_rates(hass, freezer):
    options = short_range_options(FULL_OPTIONS)
    sim = await _sim(
        hass, freezer, feeds=("actual", "forecast", "short_range"), options=options, start="2026-03-02 16:00:00+00:00"
    )
    prices = sim.coordinator._collect_prices()
    sources_for_night = {p["source"] for p in prices if _at(3, 2) <= p["date_time"] < _at(3, 3)}
    assert {"current_actual", "next_actual"} & sources_for_night

    best = sim.coordinator._collect_prices()
    from custom_components.wholesale_ev_schedule.scheduler import deduplicate_and_sort_prices

    merged = {p["date_time"]: p for p in deduplicate_and_sort_prices(best, sim.now)}
    assert merged[_at(3, 2)]["source"] in ("current_actual", "next_actual")


# ---------------------------------------------------------------------------
# Outages
# ---------------------------------------------------------------------------


async def test_rates_failing_to_publish_at_the_usual_time_falls_back_to_the_forecast_and_catches_up(hass, freezer):
    sim = await _sim(hass, freezer, feeds=("actual", "forecast"), start="2026-03-02 13:00:00+00:00")
    sim.outage("actual")
    await sim.run_to(_at(2, 22))
    assert sim.data["state"] in ("scheduled", "error")

    sim.outage("actual", on=False)
    await sim.run_to(_at(3, 8))

    assert _cycle_hours(sim, _at(3, 7)) == 3.0


async def test_the_forecast_going_away_leaves_a_working_plan_from_published_rates(hass, freezer):
    sim = await _sim(hass, freezer, feeds=("actual", "forecast"), start="2026-03-02 16:00:00+00:00")
    sim.outage("forecast")
    await sim.run_to(_at(3, 8))

    assert _cycle_hours(sim, _at(3, 7)) == 3.0


async def test_an_outage_of_everything_mid_session_keeps_the_charger_running_to_the_end_of_the_block(hass, freezer):
    sim = await _sim(
        hass, freezer, feeds=("actual", "forecast"), start="2026-03-02 16:00:00+00:00", min_block_hours=3.0
    )
    await sim.run_to(_at(3, 1, 0))
    assert sim.data["state"] == "charging"
    planned_end = parse_dt(sim.data["sessions"][0]["end"])

    sim.outage("actual")
    sim.outage("forecast")
    while sim.now + timedelta(minutes=5) < planned_end:
        t = await sim.step(5)
        assert t.state == "error" and t.desired is True, t  # still charging what was already planned
    await sim.step(5)  # the block has now ended
    t = await sim.step(5)
    assert t.state == "error" and t.desired is False  # block over; nothing further to run
    assert sim.now > planned_end


async def test_charging_done_during_an_outage_is_credited_when_the_feeds_return(hass, freezer):
    sim = await _sim(
        hass, freezer, feeds=("actual", "forecast"), start="2026-03-02 16:00:00+00:00", min_block_hours=3.0
    )
    await sim.run_to(_at(3, 1, 0))
    sim.outage("actual")
    sim.outage("forecast")
    await sim.run_to(_at(3, 4))
    sim.outage("actual", on=False)
    sim.outage("forecast", on=False)
    await sim.run_to(_at(3, 8))

    assert _cycle_hours(sim, _at(3, 7)) == 3.0  # not repeated, not lost


@pytest.mark.parametrize("bad", ["unavailable", "unknown", "missing"])
async def test_starting_with_no_price_data_reports_an_error_and_recovers_the_moment_it_arrives(hass, freezer, bad):
    """HA boots before the price integrations have loaded."""
    sim = await Sim.create(
        hass, freezer, start="2026-03-02 16:00:00+00:00", feeds=(), world=PriceWorld(seed=1), required_hours=3.0
    )
    if bad != "missing":
        for entity in (
            "event.octopus_energy_electricity_current_day_rates",
            "event.octopus_energy_electricity_next_day_rates",
            "sensor.agile_predict",
        ):
            hass.states.async_set(entity, bad)
    await sim.step(5)
    assert sim.data["state"] == "error" and "No price data" in sim.data["error_reason"] and sim.data["desired"] is False

    sim.feeds = {"actual", "forecast"}
    sim.publish(force=True)
    await sim.step(5)

    assert sim.data["state"] == "scheduled"
    await sim.run_to(_at(3, 8))
    assert _cycle_hours(sim, _at(3, 7)) == 3.0


async def test_a_feed_that_empties_out_does_not_wipe_the_plan_while_others_remain(hass, freezer):
    sim = await _sim(hass, freezer, feeds=("actual", "forecast"), start="2026-03-02 16:00:00+00:00")
    hass.states.async_set("sensor.agile_predict", "populated", {"prices": []})
    await sim.step(5)

    assert sim.data["state"] == "scheduled"


# ---------------------------------------------------------------------------
# A long, dull week
# ---------------------------------------------------------------------------


async def test_a_week_of_ordinary_operation_delivers_every_night_at_a_plausible_cost(hass, freezer):
    sim = await _sim(hass, freezer, feeds=("actual", "forecast"), min_block_hours=1.0)
    await sim.run(60 * 24 * 7, step=30)

    cycles = [m for _, m in sorted(sim.charged_minutes.items())][:-1]
    assert len(cycles) >= 6 and all(m == 180 for m in cycles), sim.charged_minutes
    assert sim.cost / 7 < 3 * 15  # always bought in the trough


# ---------------------------------------------------------------------------
# Edits made while prices are unavailable still apply to the plan being replayed
# ---------------------------------------------------------------------------


async def _stored_plan_during_outage(hass, freezer, **kwargs):
    sim = await _sim(hass, freezer, feeds=("actual", "forecast"), start="2026-03-02 16:00:00+00:00", **kwargs)
    await sim.run_to(_at(3, 0, 40))
    sim.outage("actual")
    sim.outage("forecast")
    await sim.step(5)
    assert sim.data["state"] == "error"
    return sim


def _planned_hours(sim):
    now = sim.now
    return sum(
        max(0.0, (parse_dt(s["end"]) - max(parse_dt(s["start"]), now)).total_seconds()) / 3600
        for s in sim.data["sessions"]
    )


async def test_lowering_the_hours_during_an_outage_trims_the_plan_that_is_being_replayed(hass, freezer):
    sim = await _stored_plan_during_outage(hass, freezer, min_block_hours=2.0, required_hours=3.0)
    assert _planned_hours(sim) > 1.0

    await sim.act(sim.coordinator.async_set_required_hours(1.0))

    assert sim.data["state"] == "error"
    assert _planned_hours(sim) <= 1.0
    await sim.run_to(_at(3, 6))
    assert sim.on_minutes(_at(3, 0), _at(3, 6)) <= 60 + 5


async def test_pulling_the_deadline_in_during_an_outage_trims_the_replayed_plan(hass, freezer):
    sim = await _stored_plan_during_outage(hass, freezer, min_block_hours=0.5, required_hours=3.0)
    deadline = _at(3, 2)

    await sim.act(sim.coordinator.async_set_ready_by(deadline))

    for s in sim.data["sessions"]:
        assert parse_dt(s["end"]) <= deadline


async def test_the_trimmed_plan_survives_a_restart_during_the_outage(hass, freezer):
    sim = await _stored_plan_during_outage(hass, freezer, min_block_hours=2.0, required_hours=3.0)
    await sim.act(sim.coordinator.async_set_required_hours(1.0))
    before = sim.snapshot()

    await sim.restart()

    assert sim.snapshot()["sessions"] == before["sessions"]
    assert _planned_hours(sim) <= 1.0


async def test_stopping_during_an_outage_clears_the_replayed_plan(hass, freezer):
    sim = await _stored_plan_during_outage(hass, freezer, min_block_hours=2.0, required_hours=3.0)

    await sim.act(sim.coordinator.async_stop())

    assert sim.data["state"] == "idle" and sim.data["sessions"] == [] and sim.data["desired"] is False
