"""Does the car actually pay the least it could have? With a perfect forecast and nothing changing,
continually re-planning minute by minute must still land on the cheapest schedule the rules allow
-- measured against the brute-force oracle, not against a hand-computed number."""

from datetime import datetime, timedelta

import pytest

from custom_components.wholesale_ev_schedule.scheduler import assign_credibilities, deduplicate_and_sort_prices

from .oracle import cheapest_valid
from .sim import UTC, PriceWorld, Sim, floor_slot


def _at(day, hour, minute=0):
    return datetime(2026, 3, day, hour, minute, tzinfo=UTC)


def _oracle_cost(world, first_slot, ready_by, required_slots, min_block_hours, cap=None):
    """Cheapest valid selection over the true prices in [first_slot, ready_by)."""
    slots, t = [], first_slot
    while t + timedelta(minutes=30) <= ready_by:
        slots.append({"date_time": t, "raw_price": world.true_price(t), "source": "current_actual"})
        t += timedelta(minutes=30)
    now = first_slot - timedelta(hours=1)
    prepared = assign_credibilities(deduplicate_and_sort_prices(slots, now), now, 100.0)
    best, _ = cheapest_valid(prepared, ready_by, required_slots, min_block_hours, cap)
    return best


def _paid(sim, world, start, end):
    """What the charger actually cost over [start, end], from the recorded ticks (p per kW)."""
    total = 0.0
    for prev, cur in zip(sim.timeline, sim.timeline[1:], strict=False):
        if prev.desired and prev.at >= start and cur.at <= end:
            total += world.true_price(floor_slot(prev.at)) * (cur.at - prev.at).total_seconds() / 3600
    return total


@pytest.mark.parametrize("algorithm", ["optimal", "hybrid"])
@pytest.mark.parametrize("min_block", [0.5, 1.0, 2.0])
@pytest.mark.parametrize("seed", [1, 2, 3])
async def test_replanning_every_few_minutes_still_finds_the_true_optimum(hass, freezer, algorithm, min_block, seed):
    world = PriceWorld(seed=seed, forecast_error_scale=0.0, noise=4.0)
    sim = await Sim.create(
        hass, freezer, start="2026-03-02 16:00:00+00:00", world=world, required_hours=2.0, min_block_hours=min_block,
        step_minutes=5,
    )  # fmt: skip
    sim.setting_edits = True
    await sim.coordinator.async_set_optimization_algorithm(algorithm)
    first_slot = floor_slot(sim.now)
    deadline = _at(3, 7)
    await sim.run_to(deadline + timedelta(minutes=30))

    best = _oracle_cost(world, first_slot, deadline, 4, min_block)
    paid = _paid(sim, world, first_slot, deadline)

    assert best is not None
    assert paid == pytest.approx(best * 0.5, abs=1e-6), f"paid {paid:.3f}, the cheapest possible was {best * 0.5:.3f}"


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
async def test_the_default_algorithm_is_never_cheaper_than_possible_and_stays_close(hass, freezer, seed):
    world = PriceWorld(seed=seed, forecast_error_scale=0.0, noise=4.0)
    sim = await Sim.create(
        hass,
        freezer,
        start="2026-03-02 16:00:00+00:00",
        world=world,
        required_hours=2.0,
        min_block_hours=1.0,
        step_minutes=5,
    )
    first_slot = floor_slot(sim.now)
    deadline = _at(3, 7)
    await sim.run_to(deadline + timedelta(minutes=30))

    best = _oracle_cost(world, first_slot, deadline, 4, 1.0) * 0.5
    paid = _paid(sim, world, first_slot, deadline)

    assert paid >= best - 1e-6  # nothing can beat the optimum
    assert paid <= best * 1.35, f"greedy paid {paid:.2f} against an optimum of {best:.2f}"


async def test_a_price_cap_is_respected_in_what_is_actually_paid(hass, freezer):
    world = PriceWorld(seed=2, forecast_error_scale=0.0, noise=4.0)
    sim = await Sim.create(
        hass,
        freezer,
        start="2026-03-02 16:00:00+00:00",
        world=world,
        required_hours=2.0,
        min_block_hours=0.5,
        max_price=12.0,
        step_minutes=5,
    )
    await sim.run_to(_at(3, 8))

    charged = [prev for prev, cur in zip(sim.timeline, sim.timeline[1:], strict=False) if prev.desired]
    average = sum(world.true_price(floor_slot(t.at)) for t in charged) / len(charged)
    assert len(charged) * 5 == 120 and average <= 12.0


def _executed_blocks(sim, start, end):
    """Maximal runs of consecutive charger-on time within [start, end], as lengths in minutes."""
    blocks, run = [], 0
    for prev, cur in zip(sim.timeline, sim.timeline[1:], strict=False):
        if prev.at < start or cur.at > end:
            continue
        if prev.desired:
            run += int((cur.at - prev.at).total_seconds() // 60)
        elif run:
            blocks.append(run)
            run = 0
    if run:
        blocks.append(run)
    return blocks


@pytest.mark.parametrize("error_scale", [0.0, 1.0])
@pytest.mark.parametrize("min_block", [1.0, 1.5, 2.0, 3.0])
@pytest.mark.parametrize("seed", range(1, 9))
async def test_at_most_one_block_shorter_than_the_minimum_actually_runs_per_request(
    hass, freezer, seed, min_block, error_scale
):
    """The rule holds for what the charger actually does, not just for any single plan: every re-plan
    is only the remainder of the request, so it has to remember a short block that has already run."""
    world = PriceWorld(seed=seed, forecast_error_scale=error_scale, noise=4.0)
    sim = await Sim.create(
        hass, freezer, start="2026-03-02 16:00:00+00:00", world=world, required_hours=4.0, min_block_hours=min_block,
        step_minutes=5,
    )  # fmt: skip
    first_slot = floor_slot(sim.now)
    deadline = _at(3, 7)
    await sim.run_to(deadline + timedelta(minutes=30))

    blocks = _executed_blocks(sim, first_slot, deadline)
    minimum = min(int(min_block * 60), 4 * 60)
    short = [b for b in blocks if b < minimum]
    assert sum(blocks) == 240
    assert len(short) <= 1, f"blocks run (minutes): {blocks}, minimum {minimum}"


async def test_the_short_block_count_resets_with_each_request_and_survives_a_restart(hass, freezer):
    world = PriceWorld(seed=2, forecast_error_scale=0.0, noise=4.0)
    sim = await Sim.create(
        hass,
        freezer,
        start="2026-03-02 16:00:00+00:00",
        world=world,
        required_hours=2.0,
        min_block_hours=1.0,
        step_minutes=5,
    )
    await sim.act(
        sim.coordinator.async_set_optimization_algorithm("optimal")
    )  # this market's best plan has a short block
    await sim.run_to(_at(3, 0, 40))
    assert sim.coordinator._short_blocks_used == 1  # the 30-minute block that ran at 00:00
    await sim.restart()
    assert sim.coordinator._short_blocks_used == 1

    await sim.run_to(_at(3, 7, 30))  # the deadline passed: a new request
    assert sim.coordinator._short_blocks_used == 0

    sim.setting_edits = True
    sim.coordinator._short_blocks_used = 1
    await sim.act(sim.coordinator.async_set_required_hours(2.0))
    assert sim.coordinator._short_blocks_used == 0  # setting the hours is a fresh request
