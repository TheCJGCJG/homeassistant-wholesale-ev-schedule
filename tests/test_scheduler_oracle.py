"""The scheduling algorithms against an exhaustive brute-force reference (tests/oracle.py).

For small markets every possible selection can be enumerated and checked against the rules, so
the cheapest valid one is known for certain. That turns "does the exact algorithm return the
optimum?" from an argument into a measurement. Markets are random but seeded: a few runs split
by gaps, mixed price sources and credibilities, ties, negative prices, every block-length and
price-cap setting, and deadlines that cut the market short.
"""

import random

import pytest

from custom_components.wholesale_ev_schedule.scheduler import find_optimal_slots

from .oracle import cheapest_valid, cost, is_valid, max_slots, min_slots, random_instance, usable

SEEDS = range(40)
PER_SEED = 25


def _instances(seed):
    rng = random.Random(seed * 7919 + 13)
    for _ in range(PER_SEED):
        yield random_instance(rng)


def _case(instance):
    slots, ready_by, required, min_block_hours, cap = instance
    m = min_slots(required, min_block_hours)
    usable_dts = {s["date_time"] for s in usable(slots, ready_by, m)}
    return slots, ready_by, required, min_block_hours, cap, m, usable_dts


@pytest.mark.parametrize("seed", SEEDS)
def test_optimal_returns_exactly_the_cheapest_valid_selection_or_nothing_when_none_exists(seed):
    for instance in _instances(seed):
        slots, ready_by, required, min_block_hours, cap, m, usable_dts = _case(instance)
        best, _ = cheapest_valid(slots, ready_by, required, min_block_hours, cap)

        result = find_optimal_slots(slots, required, ready_by, min_block_hours, max_price=cap, algorithm="optimal")

        if best is None:
            assert result == [], (instance[2:], [s["raw_price"] for s in slots])
        else:
            assert result, f"a valid schedule costing {best} exists but optimal found none"
            assert is_valid(result, usable_dts, required, m, cap)
            assert cost(result) == pytest.approx(best, abs=1e-9), (cost(result), best)


@pytest.mark.parametrize("algorithm", ["greedy", "optimal", "hybrid"])
@pytest.mark.parametrize("seed", SEEDS)
def test_no_algorithm_ever_returns_a_schedule_that_breaks_the_rules(seed, algorithm):
    for instance in _instances(seed):
        slots, ready_by, required, min_block_hours, cap, m, usable_dts = _case(instance)

        result = find_optimal_slots(slots, required, ready_by, min_block_hours, max_price=cap, algorithm=algorithm)

        if result:
            assert is_valid(result, usable_dts, required, m, cap), (algorithm, instance[2:])


@pytest.mark.parametrize("seed", SEEDS)
def test_hybrid_is_never_cheaper_than_the_optimum_and_greedy_is_never_cheaper_than_hybrid_is_not_required(seed):
    """optimal <= hybrid and optimal <= greedy whenever both find a schedule: nothing can beat an
    exact search. (Greedy vs hybrid has no guaranteed order.)"""
    for instance in _instances(seed):
        slots, ready_by, required, min_block_hours, cap, *_ = _case(instance)
        results = {
            alg: find_optimal_slots(slots, required, ready_by, min_block_hours, max_price=cap, algorithm=alg)
            for alg in ("greedy", "optimal", "hybrid")
        }
        if results["optimal"]:
            for alg in ("greedy", "hybrid"):
                if results[alg]:
                    assert cost(results[alg]) >= cost(results["optimal"]) - 1e-9, alg


@pytest.mark.parametrize("seed", SEEDS)
def test_optimal_is_at_least_as_good_as_greedy_at_finding_a_schedule_at_all(seed):
    for instance in _instances(seed):
        slots, ready_by, required, min_block_hours, cap, *_ = _case(instance)
        greedy = find_optimal_slots(slots, required, ready_by, min_block_hours, max_price=cap, algorithm="greedy")
        optimal = find_optimal_slots(slots, required, ready_by, min_block_hours, max_price=cap, algorithm="optimal")

        if greedy:
            assert optimal, "greedy found a schedule that the exact search missed"


@pytest.mark.parametrize("algorithm", ["greedy", "optimal", "hybrid"])
@pytest.mark.parametrize("seed", range(10))
def test_algorithms_are_deterministic_and_do_not_mutate_their_input(seed, algorithm):
    for instance in _instances(seed):
        slots, ready_by, required, min_block_hours, cap, *_ = _case(instance)
        snapshot = [dict(s) for s in slots]

        first = find_optimal_slots(slots, required, ready_by, min_block_hours, max_price=cap, algorithm=algorithm)
        second = find_optimal_slots(slots, required, ready_by, min_block_hours, max_price=cap, algorithm=algorithm)

        assert [s["date_time"] for s in first] == [s["date_time"] for s in second]
        assert slots == snapshot


# ---------------------------------------------------------------------------
# max_block_hours (not exposed in the UI, but a supported scheduler parameter)
# ---------------------------------------------------------------------------


def _with_block_cap(seed):
    rng = random.Random(seed * 104729 + 7)
    for instance in _instances(seed):
        yield instance, rng.choice([0.5, 1.0, 1.5, 2.0, 3.0])


@pytest.mark.parametrize("algorithm", ["optimal", "hybrid"])
@pytest.mark.parametrize("seed", range(20))
def test_the_dp_algorithms_are_exact_under_a_block_length_cap(seed, algorithm):
    """Includes the case that hid a defect: a cap on each block's length must not limit how many
    blocks a run can hold (hybrid used to report nothing schedulable for a multi-block answer)."""
    for instance, max_block_hours in _with_block_cap(seed):
        slots, ready_by, required, min_block_hours, cap, m, usable_dts = _case(instance)
        best, _ = cheapest_valid(slots, ready_by, required, min_block_hours, cap, max_block_hours)

        result = find_optimal_slots(
            slots,
            required,
            ready_by,
            min_block_hours,
            max_price=cap,
            max_block_hours=max_block_hours,
            algorithm=algorithm,
        )

        if best is None:
            assert result == []
        else:
            assert result, f"{algorithm} found nothing though a valid schedule costing {best} exists"
            assert is_valid(result, usable_dts, required, m, cap, max_slots(required, min_block_hours, max_block_hours))
            assert cost(result) == pytest.approx(best, abs=1e-9)
