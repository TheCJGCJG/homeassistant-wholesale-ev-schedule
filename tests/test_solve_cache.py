"""Issue #60: the coordinator skips find_optimal_slots when nothing it reads has
changed (`WholesaleEvScheduleCoordinator._solve`), instead of re-solving on every
wall-clock minute tick.

Two properties matter and are tested separately: it must *hit* when inputs are
identical (the whole point), and it must *never* hit when anything that could change
the answer changed (a stale cache would silently charge on an outdated schedule).
The second is checked by comparing against an uncached recompute rather than against
hand-written expectations, so it keeps holding as the scheduler evolves.
"""

import random
from datetime import timedelta
from unittest.mock import patch

import homeassistant.util.dt as dt_util
import pytest

from custom_components.wholesale_ev_schedule import coordinator as coordinator_module
from custom_components.wholesale_ev_schedule.const import CONF_DEFAULT_REQUIRED_HOURS, DOMAIN

from .factories import (
    CURRENT_RATES_ENTITY,
    NEXT_RATES_ENTITY,
    OPPORTUNISTIC_OPTIONS,
    async_setup_wholesale_entry,
    priced_points,
    set_octopus_rate_entity,
)

_REAL_SOLVER = coordinator_module.find_optimal_slots
_SOLVER_PATH = "custom_components.wholesale_ev_schedule.coordinator.find_optimal_slots"
_OPTIONS = {**OPPORTUNISTIC_OPTIONS, CONF_DEFAULT_REQUIRED_HOURS: 0.0}

SLOTS = 48 * 4
CHEAP, MID, EXPENSIVE = 0.05, 0.15, 0.50


def _prices(cheap_at=0.05):
    prices = [EXPENSIVE] * SLOTS
    for lo, hi, price in ((4, 12, cheap_at), (30, 38, MID), (60, 68, CHEAP), (100, 110, MID)):
        for i in range(lo, hi):
            prices[i] = price
    return prices


async def _scenario(hass, freezer):
    """Required 2h + opportunistic 2h over a multi-day horizon with several dips,
    so both tiers have a real solve to cache."""
    freezer.move_to("2026-03-02 20:00:00+00:00")
    now = dt_util.now()
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, priced_points(now, _prices()))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])
    entry = await async_setup_wholesale_entry(hass, options=_OPTIONS)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_set_min_block_hours(0.5)
    await coordinator.async_set_max_price(40.0)
    await coordinator.async_set_ready_by(now + timedelta(days=2))
    await coordinator.async_set_required_hours(2.0)
    await coordinator.async_set_opportunistic_max_price(100.0)
    await coordinator.async_set_opportunistic_target_hours(2.0)
    await coordinator.async_set_opportunistic_enabled(True)
    assert coordinator.data["sessions"], "scenario must actually schedule something"
    return coordinator, now


def _spy():
    return patch(_SOLVER_PATH, wraps=_REAL_SOLVER)


async def _uncached_sessions(coordinator):
    coordinator._solve_cache.clear()
    await coordinator.async_refresh()
    return coordinator.data["sessions"]


async def test_unchanged_refreshes_and_same_slot_minute_ticks_do_not_resolve(hass, freezer):
    coordinator, now = await _scenario(hass, freezer)
    before = coordinator.data["sessions"]

    with _spy() as spy:
        for minutes in (0, 0, 1, 2, 5):
            freezer.move_to(now + timedelta(minutes=minutes))
            await coordinator.async_refresh()

    assert spy.call_count == 0
    assert coordinator.data["sessions"] == before


async def test_a_hit_rebuilds_from_the_current_candidates_not_stale_objects(hass, freezer):
    coordinator, _ = await _scenario(hass, freezer)
    candidates = [
        {
            "date_time": coordinator.ready_by - timedelta(minutes=30 * i),
            "raw_price": 10.0 + i,
            "effective_price": 10.0 + i,
            "tier": "actual",
        }
        for i in range(1, 9)
    ]
    candidates.sort(key=lambda s: s["date_time"])
    args = ("probe", candidates, 2, coordinator.ready_by, 100.0)

    first = coordinator._solve(*args)
    rebuilt = [dict(s) for s in candidates]
    second = coordinator._solve("probe", rebuilt, 2, coordinator.ready_by, 100.0)

    assert [s["date_time"] for s in second] == [s["date_time"] for s in first]
    assert all(any(s is r for r in rebuilt) for s in second)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c, now: c.async_set_required_hours(3.0),
        lambda c, now: c.async_set_max_price(12.0),
        lambda c, now: c.async_set_min_block_hours(1.5),
        lambda c, now: c.async_set_optimization_algorithm("optimal"),
        lambda c, now: c.async_set_optimization_algorithm("hybrid"),
        lambda c, now: c.async_set_ready_by(now + timedelta(days=1)),
        lambda c, now: c.async_set_opportunistic_target_hours(3.0),
        lambda c, now: c.async_set_opportunistic_max_price(20.0),
        lambda c, now: c.async_set_opportunistic_ready_by(now + timedelta(days=1)),
    ],
    ids=[
        "required_hours",
        "max_price",
        "min_block_hours",
        "algorithm_optimal",
        "algorithm_hybrid",
        "ready_by",
        "opp_target_hours",
        "opp_max_price",
        "opp_ready_by",
    ],
)
async def test_every_input_that_can_change_the_answer_forces_a_resolve(hass, freezer, mutate):
    coordinator, now = await _scenario(hass, freezer)

    with _spy() as spy:
        await mutate(coordinator, now)

    assert spy.call_count >= 1
    cached = coordinator.data["sessions"]
    assert cached == await _uncached_sessions(coordinator)


async def test_opportunistic_only_change_resolves_only_the_opportunistic_tier(hass, freezer):
    coordinator, _ = await _scenario(hass, freezer)

    with _spy() as spy:
        await coordinator.async_set_opportunistic_max_price(30.0)

    assert spy.call_count == 1
    assert spy.call_args.kwargs["max_price"] == 30.0


async def test_new_price_data_forces_a_resolve(hass, freezer):
    coordinator, now = await _scenario(hass, freezer)
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, priced_points(now, _prices(cheap_at=0.01)[::-1]))

    with _spy() as spy:
        await coordinator.async_refresh()

    assert spy.call_count >= 1
    assert coordinator.data["sessions"] == await _uncached_sessions(coordinator)


async def test_crossing_a_slot_boundary_forces_a_resolve(hass, freezer):
    coordinator, now = await _scenario(hass, freezer)
    freezer.move_to(now + timedelta(minutes=31))

    with _spy() as spy:
        await coordinator.async_refresh()

    assert spy.call_count >= 1
    assert coordinator.data["sessions"] == await _uncached_sessions(coordinator)


@pytest.mark.parametrize(
    "change",
    [
        {"raw_price": 11.0},
        {"effective_price": 99.0},
        {"tier": "predicted_24_48"},
    ],
    ids=["raw_price", "effective_price", "tier"],
)
async def test_any_single_slot_field_the_solver_or_sessions_read_invalidates(hass, freezer, change):
    """effective_price is what makes credibility tiers shifting with time invalidate
    the cache; tier feeds slots_to_sessions' confidence."""
    coordinator, _ = await _scenario(hass, freezer)
    base = coordinator.ready_by - timedelta(hours=6)
    candidates = [
        {"date_time": base + timedelta(minutes=30 * i), "raw_price": 10.0, "effective_price": 10.0, "tier": "actual"}
        for i in range(8)
    ]

    with _spy() as spy:
        coordinator._solve("probe", candidates, 2, coordinator.ready_by, 100.0)
        coordinator._solve("probe", [dict(s) for s in candidates], 2, coordinator.ready_by, 100.0)
        assert spy.call_count == 1  # identical content, different objects: a hit
        changed = [dict(s) for s in candidates]
        changed[3].update(change)
        coordinator._solve("probe", changed, 2, coordinator.ready_by, 100.0)

    assert spy.call_count == 2


async def test_randomised_edit_and_clock_sequences_always_match_an_uncached_recompute(hass, freezer):
    coordinator, now = await _scenario(hass, freezer)
    rng = random.Random(60)
    edits = [
        lambda: coordinator.async_set_required_hours(rng.choice([1.0, 2.0, 3.0, 4.5])),
        lambda: coordinator.async_set_max_price(rng.choice([12.0, 25.0, 40.0, 80.0])),
        lambda: coordinator.async_set_min_block_hours(rng.choice([0.5, 1.0, 2.0])),
        lambda: coordinator.async_set_optimization_algorithm(rng.choice(["greedy", "optimal", "hybrid"])),
        lambda: coordinator.async_set_opportunistic_target_hours(rng.choice([0.0, 1.0, 2.5])),
        lambda: coordinator.async_set_opportunistic_max_price(rng.choice([20.0, 60.0, 100.0])),
        lambda: coordinator.async_refresh(),
    ]

    t = now
    for _ in range(25):
        if rng.random() < 0.4:
            t += timedelta(minutes=rng.choice([1, 7, 29, 31, 95]))
            freezer.move_to(t)
            await coordinator.async_refresh()
        else:
            await rng.choice(edits)()
        cached = coordinator.data["sessions"]
        assert cached == await _uncached_sessions(coordinator)
