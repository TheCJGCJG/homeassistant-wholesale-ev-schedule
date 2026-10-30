"""Constraint-level tests for find_optimal_slots: min block length and max_price
as *guarantees about the returned schedule*, across all three algorithms.

Found by inspecting a live install (hybrid algorithm, min_block_hours=2,
required 1.5h, max_price=40) that scheduled three isolated 30-minute slots, one
of them (40.02) over the cap. Root cause is in the "top up with the cheapest
leftover slots" step shared by the algorithms:

- `_solve_with_per_run_costs` tries every partial total j (including j=0, i.e.
  *no whole block at all*) and tops up the rest with the cheapest individual
  slots from anywhere, so the DP algorithms freely pick scattered single slots
  whenever that's cheaper than any valid contiguous block, defeating
  min_block_hours entirely.
- Neither that top-up nor greedy's own remainder top-up filters by max_price, so
  an over-cap slot can be selected.

These assert the intended behaviour, so they fail until the top-up step is fixed.
"""

import math
import random
from datetime import datetime, timedelta

import pytest

from custom_components.wholesale_ev_schedule.scheduler import (
    assign_credibilities,
    build_contiguous_runs,
    find_optimal_slots,
)

NOW = datetime(2024, 1, 15, 22, 0)
READY_BY = NOW + timedelta(hours=40)
ALGORITHMS = ["greedy", "optimal", "hybrid"]


def _slots(prices, gaps=()):
    """Consecutive 30-min slots from NOW; a `gaps` index inserts a missing slot
    before it, splitting the data into separate contiguous runs."""
    out = []
    t = NOW
    for i, price in enumerate(prices):
        if i in gaps:
            t += timedelta(minutes=30)
        out.append({"date_time": t, "raw_price": price, "source": "current_actual"})
        t += timedelta(minutes=30)
    return assign_credibilities(out, NOW, gamble_tolerance=100.0)


def _blocks(result):
    return build_contiguous_runs(sorted(result, key=lambda s: s["date_time"]))


def _avg(block):
    return sum(s["raw_price"] for s in block) / len(block)


def _min_slots(required, min_block_hours):
    # Mirrors the scheduler's documented relaxation: a minimum longer than the
    # whole requirement collapses to one contiguous block.
    return min(max(1, math.ceil(min_block_hours * 2)), required)


# ---------------------------------------------------------------------------
# Concrete reproductions
# ---------------------------------------------------------------------------

# One valid contiguous 3-slot window (38 each, avg under the 40 cap) hidden among
# pricier slots, with three isolated 20s that are individually cheaper. The three
# 20s cost 60 vs 114 for the window, so a cost-only top-up scatters them.
_DIPS = [20, 60, 60, 60, 20, 60, 60, 60, 20, 60, 38, 38, 38, 60]


@pytest.mark.parametrize(
    "algorithm",
    [
        "greedy",
        "optimal",
        "hybrid",
    ],
)
def test_required_slots_are_not_scattered_into_single_slot_blocks(algorithm):
    """Live-instance repro: required 3 slots, min_block_hours=2 (relaxed to 3
    slots) must yield one 3-slot block, not three isolated 30-minute slots."""
    result = find_optimal_slots(_slots(_DIPS), 3, READY_BY, min_block_hours=2.0, max_price=40.0, algorithm=algorithm)

    assert len(result) == 3
    assert [len(b) for b in _blocks(result)] == [3]


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_remainder_top_up_slot_never_exceeds_max_price(algorithm):
    """Only [10, 10] is a valid priced window under the cap. The 1-slot
    remainder is then topped up from the cheapest leftover (35), which is over
    the cap of 30 -- no schedule, or a compliant one, is acceptable; a block
    averaging above max_price is not."""
    prices = [10, 10, 90, 90, 35, 90, 90]
    result = find_optimal_slots(_slots(prices), 3, READY_BY, min_block_hours=1.0, max_price=30.0, algorithm=algorithm)

    assert all(_avg(b) <= 30.0 for b in _blocks(result))


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_max_price_with_no_compliant_window_schedules_nothing(algorithm):
    """If every window is over the cap, the answer is 'unschedulable', never a
    schedule made of over-cap slots."""
    result = find_optimal_slots(
        _slots([50.0] * 10), 3, READY_BY, min_block_hours=1.0, max_price=40.0, algorithm=algorithm
    )

    assert result == []


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_max_price_is_inclusive(algorithm):
    """A block averaging exactly max_price is allowed (boundary of `<=`)."""
    result = find_optimal_slots(
        _slots([40.0] * 6), 2, READY_BY, min_block_hours=1.0, max_price=40.0, algorithm=algorithm
    )

    assert len(result) == 2


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_max_price_averages_over_block_not_per_slot(algorithm):
    """[10, 50] averages 30: valid under a cap of 30 even though one slot is
    above it. Pins that max_price is a per-block average (documented), so the
    top-up fix must not start rejecting every individually-over-cap slot."""
    result = find_optimal_slots(
        _slots([10.0, 50.0, 90.0, 90.0]), 2, READY_BY, min_block_hours=1.0, max_price=30.0, algorithm=algorithm
    )

    assert [s["raw_price"] for s in result] == [10.0, 50.0]


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_max_price_none_allows_any_price(algorithm):
    result = find_optimal_slots(
        _slots([500.0] * 4), 2, READY_BY, min_block_hours=1.0, max_price=None, algorithm=algorithm
    )

    assert len(result) == 2


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_min_block_longer_than_requirement_relaxes_to_one_contiguous_block(algorithm):
    """The exact live setting: min_block_hours=2 but only 1.5h (3 slots) needed.
    A single contiguous 3-slot block is the best case and must be found when it
    is also the cheapest option."""
    result = find_optimal_slots(
        _slots([60, 60, 10, 10, 10, 60, 60]), 3, READY_BY, min_block_hours=2.0, max_price=40.0, algorithm=algorithm
    )

    assert [s["raw_price"] for s in result] == [10, 10, 10]


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_runs_shorter_than_min_block_are_never_used_as_whole_blocks(algorithm):
    """Two cheap isolated slots (separated by data gaps) can't satisfy a 1h
    minimum on their own; the longer, pricier run must be chosen instead."""
    prices = [5, 99, 5, 99, 30, 30, 30, 30]
    result = find_optimal_slots(
        _slots(prices, gaps={1, 2, 3, 4}), 4, READY_BY, min_block_hours=1.0, algorithm=algorithm
    )

    assert [s["raw_price"] for s in result] == [30, 30, 30, 30]


# ---------------------------------------------------------------------------
# Randomised invariants (seeded, so failures are reproducible)
# ---------------------------------------------------------------------------


def _scenarios(count=400, seed=20261030):
    rng = random.Random(seed)
    for _ in range(count):
        n = rng.randint(6, 40)
        prices = [round(rng.uniform(15, 60), 2) for _ in range(n)]
        gaps = {rng.randint(1, n - 1)} if rng.random() < 0.3 else set()
        yield {
            "slots": _slots(prices, gaps),
            "required": rng.randint(1, 8),
            "min_block_hours": rng.choice([0.5, 1.0, 1.5, 2.0, 3.0]),
            "max_price": rng.choice([None, 35.0, 40.0, 45.0]),
        }


def _run(algorithm, sc):
    return find_optimal_slots(
        sc["slots"],
        sc["required"],
        READY_BY,
        sc["min_block_hours"],
        max_price=sc["max_price"],
        algorithm=algorithm,
    )


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_invariant_result_is_exactly_the_requirement_with_no_duplicates(algorithm):
    """Already holds: a non-empty answer always has exactly `required` distinct
    slots, all of which came from the candidates and finish before ready_by."""
    for sc in _scenarios():
        result = _run(algorithm, sc)
        if not result:
            continue
        times = [s["date_time"] for s in result]
        assert len(times) == sc["required"] == len(set(times))
        assert set(times) <= {s["date_time"] for s in sc["slots"]}
        assert all(t + timedelta(minutes=30) <= READY_BY for t in times)


@pytest.mark.parametrize("algorithm", ["optimal", "hybrid"])
def test_invariant_dp_algorithms_never_cost_more_than_greedy(algorithm):
    """Already holds and is the whole point of 'optimal'/'hybrid' (issue #55)."""
    for sc in _scenarios(count=200):
        greedy = _run("greedy", sc)
        other = _run(algorithm, sc)
        if greedy and other:
            assert sum(s["effective_price"] for s in other) <= sum(s["effective_price"] for s in greedy) + 1e-9


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_invariant_at_most_one_undersized_block(algorithm):
    """The documented relaxation is that the *last* block may be shorter than
    min_block_hours -- one block, not arbitrarily many."""
    for sc in _scenarios():
        result = _run(algorithm, sc)
        if not result:
            continue
        floor = _min_slots(sc["required"], sc["min_block_hours"])
        undersized = [b for b in _blocks(result) if len(b) < floor]
        assert len(undersized) <= 1, (sc["required"], sc["min_block_hours"], [len(b) for b in _blocks(result)])


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_invariant_no_block_averages_above_max_price(algorithm):
    """What users see as each session's avg_price must respect max_price."""
    for sc in _scenarios():
        cap = sc["max_price"]
        result = _run(algorithm, sc)
        if cap is None or not result:
            continue
        for block in _blocks(result):
            assert _avg(block) <= cap, (cap, [s["raw_price"] for s in block])


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_max_price_checks_a_long_blocks_whole_average_not_just_its_tail(algorithm):
    """[40, 1, 1] is one valid-looking 3-slot block only if you look at its last
    slot; its real average (14) is over the cap of 10. With no max_block_hours the
    DP used to collapse every block length >= min into one state, so its cap check
    averaged just the last `min` slots and let this through. The only other slot
    (99) is far over the cap, so the right answer is 'unschedulable'."""
    result = find_optimal_slots(
        _slots([40.0, 1.0, 1.0, 99.0], gaps={3}),
        3,
        READY_BY,
        min_block_hours=0.5,
        max_price=10.0,
        algorithm=algorithm,
    )

    assert all(_avg(b) <= 10.0 for b in _blocks(result))
