"""Seeded chaos: a few simulated days of random settings changes, button presses, feed outages,
market shifts and restarts, with the invariants checked after every step. Each seed is a
different, fully reproducible story -- a failure message names the seed and the action log, so
it can be replayed and turned into a focused regression test."""

import random
from datetime import timedelta

import pytest

from custom_components.wholesale_ev_schedule.const import CONF_DEFAULT_OPPORTUNISTIC_OFFSET_DAYS

from .factories import FULL_OPTIONS, OPPORTUNISTIC_OPTIONS, short_range_options
from .sim import PriceWorld, Sim

TIMEZONES = ["UTC", "Europe/London", "America/New_York", "Australia/Adelaide", "Asia/Kolkata"]


def _build(rng: random.Random):
    use_short_range = rng.random() < 0.5
    use_opportunistic = rng.random() < 0.5
    options = {**(OPPORTUNISTIC_OPTIONS if use_opportunistic else FULL_OPTIONS)}
    options[CONF_DEFAULT_OPPORTUNISTIC_OFFSET_DAYS] = rng.choice([1, 2, 7])
    if use_short_range:
        options = short_range_options(options)
    feeds = {"actual", "forecast"} | ({"short_range"} if use_short_range else set())
    if rng.random() < 0.2:
        feeds.discard(rng.choice(["forecast", "short_range"]))
    return options, feeds, use_opportunistic


async def _random_action(sim: Sim, rng: random.Random, opportunistic: bool, log: list) -> None:
    c = sim.coordinator
    now = sim.now
    choices = [
        ("required_hours", lambda: c.async_set_required_hours(rng.choice([0.0, 0.5, 1.0, 2.0, 3.5, 6.0, 9.0]))),
        ("ready_by", lambda: c.async_set_ready_by(now + timedelta(minutes=rng.randint(20, 40 * 60)))),
        ("max_price", lambda: c.async_set_max_price(rng.choice([0.0, 3.0, 8.0, 12.0, 25.0, 60.0, 200.0]))),
        ("min_block", lambda: c.async_set_min_block_hours(rng.choice([0.0, 0.5, 1.0, 2.0, 4.0, 8.0]))),
        ("gamble", lambda: c.async_set_gamble_tolerance(rng.choice([0.0, 20.0, 50.0, 100.0]))),
        ("algorithm", lambda: c.async_set_optimization_algorithm(rng.choice(["greedy", "optimal", "hybrid"]))),
        ("kwh", lambda: c.async_set_assumed_charge_kwh(rng.choice([0.0, 7.0, 40.0]))),
        ("override", lambda: c.async_set_charge_override(rng.choice(["auto", "auto", "force_on", "force_off"]))),
        ("boost", lambda: c.async_start_boost(rng.choice([0.5, 1.0, 2.0, 4.0]))),
        ("cancel_boost", lambda: c.async_cancel_boost()),
        ("stop", lambda: c.async_stop()),
        ("reset", lambda: c.async_reset()),
        ("feed_outage", lambda: _toggle_outage(sim, rng)),
        ("market_shift", lambda: _shift_market(sim)),
        ("restart", lambda: sim.restart()),
    ]
    if opportunistic:
        choices += [
            ("opp_enabled", lambda: c.async_set_opportunistic_enabled(rng.random() < 0.6)),
            ("opp_target", lambda: c.async_set_opportunistic_target_hours(rng.choice([0.0, 1.0, 3.0, 6.0]))),
            ("opp_max_price", lambda: c.async_set_opportunistic_max_price(rng.choice([0.0, 10.0, 30.0, 100.0]))),
            (
                "opp_ready_by",
                lambda: c.async_set_opportunistic_ready_by(now + timedelta(minutes=rng.randint(20, 80 * 60))),
            ),
        ]
    name, make = rng.choice(choices)
    log.append(f"{now.isoformat()} {name}")
    if name in ("restart",):
        sim.setting_edits = True
        await make()
        sim.timeline.append(sim._observe())
        return
    await sim.act(make())


async def _toggle_outage(sim: Sim, rng: random.Random) -> None:
    feed = rng.choice(sorted(sim.feeds))
    sim.outage(feed, on=feed not in sim.outages)
    await sim.coordinator.async_refresh()


async def _shift_market(sim: Sim) -> None:
    sim.world.seed += 1
    sim.publish(force=True)
    await sim.coordinator.async_refresh()


@pytest.mark.parametrize("seed", range(40))
async def test_random_days_never_break_an_invariant(hass, freezer, seed):
    rng = random.Random(seed)
    options, feeds, opportunistic = _build(rng)
    tz = rng.choice(TIMEZONES)
    start = rng.choice(["2026-03-02 12:00:00+00:00", "2026-03-27 20:00:00+00:00", "2026-10-23 20:00:00+00:00"])
    log: list[str] = []

    sim = await Sim.create(
        hass,
        freezer,
        start=start,
        tz=tz,
        options=options,
        feeds=feeds,
        world=PriceWorld(seed=seed),
        required_hours=rng.choice([1.0, 2.0, 3.0, 5.0]),
        min_block_hours=rng.choice([0.5, 1.0, 2.0]),
        step_minutes=15,
    )
    try:
        for _ in range(rng.randint(120, 180)):  # ~30-45 simulated hours
            if rng.random() < 0.08:
                await _random_action(sim, rng, opportunistic, log)
            await sim.step(rng.choice([5, 15, 15, 30]))
    except AssertionError as err:
        raise AssertionError(
            f"seed={seed} tz={tz} start={start}\nactions:\n  " + "\n  ".join(log[-12:]) + f"\n{err}"
        ) from err


@pytest.mark.parametrize("seed", range(10))
async def test_after_any_chaos_the_system_recovers_and_delivers_the_next_cycle(hass, freezer, seed):
    """Whatever was thrown at it, once feeds are back and the user presses Reset and asks for
    hours again, the next full cycle delivers exactly that."""
    rng = random.Random(1000 + seed)
    options, feeds, opportunistic = _build(rng)
    sim = await Sim.create(
        hass,
        freezer,
        start="2026-03-02 12:00:00+00:00",
        options=options,
        feeds=feeds,
        world=PriceWorld(seed=seed),
        required_hours=3.0,
        step_minutes=15,
    )
    log: list[str] = []
    for _ in range(100):
        if rng.random() < 0.1:
            await _random_action(sim, rng, opportunistic, log)
        await sim.step(15)

    for feed in list(sim.outages):
        sim.outage(feed, on=False)
    await sim.act(sim.coordinator.async_reset())
    await sim.act(sim.coordinator.async_set_min_block_hours(0.5))
    await sim.act(sim.coordinator.async_set_max_price(100.0))
    await sim.act(sim.coordinator.async_set_required_hours(2.0))
    if opportunistic:
        await sim.act(sim.coordinator.async_set_opportunistic_enabled(False))
    first_deadline = sim.coordinator.ready_by
    await sim.run((first_deadline - sim.now).total_seconds() // 60 + 30, step=15)

    assert sim.on_minutes(first_deadline - timedelta(days=1), first_deadline) >= 120, log[-8:]
