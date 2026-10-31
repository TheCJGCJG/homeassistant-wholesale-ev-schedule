"""A restart must be invisible: a brand-new coordinator rebuilt purely from storage has to report
exactly what the old one did, at any moment, in any state -- and the day must carry on as if
nothing had happened."""

from datetime import timedelta

import pytest

from custom_components.wholesale_ev_schedule.const import CONF_ENABLE_OPPORTUNISTIC_CHARGING
from custom_components.wholesale_ev_schedule.scheduler import parse_dt

from .factories import FULL_OPTIONS, OPPORTUNISTIC_OPTIONS, short_range_options
from .sim import PriceWorld, Sim


async def _restart_and_compare(sim: Sim) -> None:
    before = sim.snapshot()
    await sim.restart()
    after = sim.snapshot()
    assert after == before, {k: (before[k], after[k]) for k in before if before[k] != after[k]}


@pytest.mark.parametrize("tz", ["UTC", "Europe/London", "America/New_York"])
async def test_restart_at_every_hour_of_a_three_day_run_changes_nothing(hass, freezer, tz):
    """A restart every 90 minutes lands in every phase: idle, scheduled, charging, complete, the
    publication boundaries, and exactly on a ready_by rollover."""
    sim = await Sim.create(
        hass,
        freezer,
        start="2026-03-02 12:00:00+00:00",
        tz=tz,
        world=PriceWorld(seed=5, trough=(0.0, 4.5)),
        required_hours=3.0,
    )
    for _ in range(48):
        await sim.run(90, step=15)
        await _restart_and_compare(sim)

    assert {"scheduled", "charging", "complete"} <= {t.state for t in sim.timeline}
    full = [m for k, m in sorted(sim.charged_minutes.items())][:-1]
    assert full and all(m == 180 for m in full), sim.charged_minutes


async def test_restarting_every_single_step_through_a_session_never_drops_or_repeats_charging(hass, freezer):
    world = PriceWorld(seed=2, trough=(1.0, 4.0))
    sim = await Sim.create(hass, freezer, start="2026-03-02 22:00:00+00:00", world=world, required_hours=3.0)
    first_deadline = sim.coordinator.ready_by  # captured once: ready_by rolls forward at each deadline
    while sim.now < first_deadline + timedelta(minutes=30):
        await sim.step(5)
        await _restart_and_compare(sim)

    cycle = sorted(sim.charged_minutes)[0]
    assert sim.charged_minutes[cycle] == 180


async def test_restart_during_an_active_boost_keeps_the_boost_and_its_end_time(hass, freezer):
    sim = await Sim.create(hass, freezer, start="2026-03-02 08:00:00+00:00", required_hours=2.0)
    sim.setting_edits = True
    await sim.coordinator.async_start_boost(2.0)
    await sim.step(30)
    assert sim.data["state"] == "boosting"
    boost_end = sim.data["boost_end"]

    await _restart_and_compare(sim)

    assert sim.data["state"] == "boosting" and sim.data["boost_end"] == boost_end and sim.data["desired"] is True
    await sim.run(120, step=15)
    assert sim.data["state"] != "boosting"
    assert parse_dt(boost_end) <= sim.now


@pytest.mark.parametrize("override", ["force_on", "force_off"])
async def test_restart_keeps_the_manual_override(hass, freezer, override):
    sim = await Sim.create(hass, freezer, start="2026-03-02 08:00:00+00:00", required_hours=2.0)
    sim.setting_edits = True
    await sim.coordinator.async_set_charge_override(override)
    await sim.step(5)

    await _restart_and_compare(sim)

    assert sim.data["desired"] is (override == "force_on")
    await sim.run(60, step=15)
    assert sim.data["desired"] is (override == "force_on")


async def test_restart_while_stopped_stays_idle_until_hours_are_set_again(hass, freezer):
    sim = await Sim.create(hass, freezer, start="2026-03-02 08:00:00+00:00", required_hours=2.0)
    sim.setting_edits = True
    await sim.coordinator.async_stop()
    await sim.step(5)

    await _restart_and_compare(sim)
    await sim.run(60 * 30, step=30)

    assert {t.state for t in sim.timeline[-60:]} == {"idle"}
    assert sim.charged_minutes.get(sim.timeline[-1].ready_by.isoformat(), 0) == 0


async def test_restart_with_opportunistic_charging_preserves_both_tiers(hass, freezer):
    options = {**OPPORTUNISTIC_OPTIONS}
    sim = await Sim.create(
        hass, freezer, start="2026-03-02 12:00:00+00:00", options=options, world=PriceWorld(seed=4), required_hours=2.0
    )
    c = sim.coordinator
    sim.setting_edits = True
    await c.async_set_opportunistic_max_price(100.0)
    await c.async_set_opportunistic_target_hours(2.0)
    await c.async_set_opportunistic_enabled(True)
    tiers_seen = set()
    for _ in range(40):
        await sim.run(120, step=20)
        tiers_seen |= {s["tier"] for s in sim.data["sessions"]}
        await _restart_and_compare(sim)

    assert tiers_seen == {"required", "opportunistic"}


async def test_restart_with_a_short_range_feed_preserves_the_estimated_flags(hass, freezer):
    options = short_range_options(FULL_OPTIONS)
    sim = await Sim.create(
        hass,
        freezer,
        start="2026-03-02 09:00:00+00:00",
        options=options,
        feeds=("actual", "forecast", "short_range"),
        world=PriceWorld(seed=8),
        required_hours=3.0,
    )
    flags = set()
    for _ in range(24):
        await sim.run(60, step=20)
        flags |= {s["estimated"] for s in sim.data["sessions"]}
        await _restart_and_compare(sim)

    assert flags == {True, False}


async def test_opportunistic_entities_stay_absent_after_restart_when_disabled(hass, freezer):
    sim = await Sim.create(hass, freezer, options={**FULL_OPTIONS, CONF_ENABLE_OPPORTUNISTIC_CHARGING: False})
    await sim.restart()
    assert hass.states.get("switch.wholesale_ev_schedule_opportunistic_charging_enabled") is None
