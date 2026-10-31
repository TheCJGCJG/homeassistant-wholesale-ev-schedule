"""Pure-scheduler coverage for short-range forecasts (issue #54): per-slot source
precedence, the short-range credibility tier and its per-slot override, and the
`estimated` flag on sessions. No hass fixture -- scheduler.py stays HA-free.
"""

import itertools
import math
from datetime import datetime, timedelta

import pytest

from custom_components.wholesale_ev_schedule.scheduler import (
    BASE_CREDIBILITY,
    SOURCE_CURRENT_ACTUAL,
    SOURCE_NEXT_ACTUAL,
    SOURCE_PREDICTED,
    SOURCE_SHORT_RANGE,
    SOURCE_SHORT_RANGE_FINAL,
    TIER_ACTUAL,
    TIER_PREDICTED_0_24,
    TIER_SHORT_RANGE,
    assign_credibilities,
    compute_effective_price,
    deduplicate_and_sort_prices,
    find_optimal_slots,
    get_source_tier,
    slot_base_credibility,
    slots_to_sessions,
    summarize_prices,
)

NOW = datetime(2026, 3, 2, 20, 0)
SLOT = NOW + timedelta(hours=4)


def _slot(source, price=10.0, dt=SLOT, **extra):
    return {"date_time": dt, "raw_price": price, "source": source, **extra}


# ---------------------------------------------------------------------------
# Source precedence (deduplicate_and_sort_prices)
# ---------------------------------------------------------------------------

# Best to worst. current/next actual share rank 0, so they are not a strict order.
_PRECEDENCE = [
    [SOURCE_CURRENT_ACTUAL, SOURCE_NEXT_ACTUAL],
    [SOURCE_SHORT_RANGE_FINAL],
    [SOURCE_SHORT_RANGE],
    [SOURCE_PREDICTED],
]


@pytest.mark.parametrize(
    "better,worse",
    [
        (b, w)
        for hi, lo in itertools.combinations(range(len(_PRECEDENCE)), 2)
        for b in _PRECEDENCE[hi]
        for w in _PRECEDENCE[lo]
    ],
)
@pytest.mark.parametrize("better_first", [True, False])
def test_better_ranked_source_wins_a_slot_regardless_of_input_order(better, worse, better_first):
    better_slot, worse_slot = _slot(better, price=1.0), _slot(worse, price=2.0)
    prices = [better_slot, worse_slot] if better_first else [worse_slot, better_slot]

    result = deduplicate_and_sort_prices(prices, NOW)

    assert [s["source"] for s in result] == [better]
    assert result[0]["raw_price"] == 1.0


@pytest.mark.parametrize(
    "source",
    [SOURCE_CURRENT_ACTUAL, SOURCE_NEXT_ACTUAL, SOURCE_SHORT_RANGE_FINAL, SOURCE_SHORT_RANGE, SOURCE_PREDICTED],
)
def test_equal_ranked_sources_keep_whichever_came_first(source):
    result = deduplicate_and_sort_prices([_slot(source, price=1.0), _slot(source, price=2.0)], NOW)
    assert [s["raw_price"] for s in result] == [1.0]


def test_current_and_next_actual_tie_keeps_the_first():
    """Unchanged from before short-range existed: the two actual sources never
    displace each other."""
    first = deduplicate_and_sort_prices([_slot(SOURCE_NEXT_ACTUAL, 1.0), _slot(SOURCE_CURRENT_ACTUAL, 2.0)], NOW)
    assert first[0]["source"] == SOURCE_NEXT_ACTUAL
    second = deduplicate_and_sort_prices([_slot(SOURCE_CURRENT_ACTUAL, 2.0), _slot(SOURCE_NEXT_ACTUAL, 1.0)], NOW)
    assert second[0]["source"] == SOURCE_CURRENT_ACTUAL


@pytest.mark.parametrize(
    "permutation",
    list(
        itertools.permutations([SOURCE_PREDICTED, SOURCE_SHORT_RANGE, SOURCE_SHORT_RANGE_FINAL, SOURCE_CURRENT_ACTUAL])
    ),
)
def test_four_way_overlap_resolves_to_actual_in_any_order(permutation):
    prices = [_slot(src, price=float(i)) for i, src in enumerate(permutation)]
    assert deduplicate_and_sort_prices(prices, NOW)[0]["source"] == SOURCE_CURRENT_ACTUAL


def test_unknown_source_ranks_as_the_forecast_exactly_as_before():
    """Before short-range existed every non-actual source was equivalent; an unknown
    label must still lose to every known source and tie with the forecast."""
    assert (
        deduplicate_and_sort_prices([_slot("mystery", 1.0), _slot(SOURCE_PREDICTED, 2.0)], NOW)[0]["source"]
        == "mystery"
    )
    assert (
        deduplicate_and_sort_prices([_slot(SOURCE_PREDICTED, 2.0), _slot("mystery", 1.0)], NOW)[0]["source"]
        == SOURCE_PREDICTED
    )
    assert (
        deduplicate_and_sort_prices([_slot("mystery", 1.0), _slot(SOURCE_SHORT_RANGE, 2.0)], NOW)[0]["source"]
        == SOURCE_SHORT_RANGE
    )
    assert (
        deduplicate_and_sort_prices([_slot(SOURCE_SHORT_RANGE, 2.0), _slot("mystery", 1.0)], NOW)[0]["source"]
        == SOURCE_SHORT_RANGE
    )


def test_precedence_is_per_slot_so_coverage_gaps_fall_back_to_the_next_source():
    """The short-range source covers only the first 3 of 6 slots; the rest fall back
    to the long-range forecast, and the first slot is real."""
    slots = [_slot(SOURCE_PREDICTED, 50.0, SLOT + timedelta(minutes=30 * i)) for i in range(6)]
    slots += [_slot(SOURCE_SHORT_RANGE, 30.0, SLOT + timedelta(minutes=30 * i)) for i in range(3)]
    slots += [_slot(SOURCE_CURRENT_ACTUAL, 20.0, SLOT)]

    result = deduplicate_and_sort_prices(slots, NOW)

    assert [(s["source"], s["raw_price"]) for s in result] == [
        (SOURCE_CURRENT_ACTUAL, 20.0),
        (SOURCE_SHORT_RANGE, 30.0),
        (SOURCE_SHORT_RANGE, 30.0),
        (SOURCE_PREDICTED, 50.0),
        (SOURCE_PREDICTED, 50.0),
        (SOURCE_PREDICTED, 50.0),
    ]


def test_expired_slots_are_still_dropped_and_output_stays_sorted():
    past = NOW - timedelta(hours=2)
    later, sooner = SLOT + timedelta(hours=1), SLOT
    result = deduplicate_and_sort_prices(
        [
            _slot(SOURCE_SHORT_RANGE, 1.0, later),
            _slot(SOURCE_PREDICTED, 1.0, past),
            _slot(SOURCE_SHORT_RANGE, 1.0, sooner),
        ],
        NOW,
    )
    assert [s["date_time"] for s in result] == [sooner, later]


def test_a_better_ranked_slot_does_not_disturb_neighbouring_slots():
    a, b = SLOT, SLOT + timedelta(minutes=30)
    result = deduplicate_and_sort_prices(
        [_slot(SOURCE_PREDICTED, 9.0, a), _slot(SOURCE_PREDICTED, 9.0, b), _slot(SOURCE_SHORT_RANGE, 1.0, b)], NOW
    )
    assert [(s["date_time"], s["source"]) for s in result] == [(a, SOURCE_PREDICTED), (b, SOURCE_SHORT_RANGE)]


# ---------------------------------------------------------------------------
# Tiers
# ---------------------------------------------------------------------------


def test_final_short_range_is_actual_grade():
    assert get_source_tier(SOURCE_SHORT_RANGE_FINAL, SLOT, NOW) == TIER_ACTUAL


@pytest.mark.parametrize("hours_ahead", [0, 1, 23.9, 24, 30, 48, 72, 200, 5000])
def test_short_range_estimate_tier_ignores_the_horizon(hours_ahead):
    assert get_source_tier(SOURCE_SHORT_RANGE, NOW + timedelta(hours=hours_ahead), NOW) == TIER_SHORT_RANGE


def test_forecast_tiers_are_unchanged_by_short_range_existing():
    assert get_source_tier(SOURCE_PREDICTED, NOW + timedelta(hours=10), NOW) == TIER_PREDICTED_0_24
    assert get_source_tier(SOURCE_CURRENT_ACTUAL, NOW + timedelta(hours=100), NOW) == TIER_ACTUAL


def test_short_range_base_credibility_fallback_matches_the_configured_default():
    """scheduler.py can't import const.py (it must stay HA-free), so the fallback is
    duplicated -- this keeps the two from drifting apart."""
    from custom_components.wholesale_ev_schedule.const import DEFAULT_SHORT_RANGE_CONFIDENCE

    assert BASE_CREDIBILITY[TIER_SHORT_RANGE] == DEFAULT_SHORT_RANGE_CONFIDENCE / 100.0


# ---------------------------------------------------------------------------
# Credibility override
# ---------------------------------------------------------------------------


def test_effective_price_without_an_override_is_unchanged():
    assert compute_effective_price(40.0, TIER_PREDICTED_0_24, 50.0) == pytest.approx(40.0 / 0.95)
    assert compute_effective_price(40.0, TIER_ACTUAL, 0.0) == 40.0


@pytest.mark.parametrize(
    "override,gamble,expected_cred",
    [
        (0.95, 0.0, 0.95),
        (0.95, 100.0, 1.0),
        (0.95, 50.0, 0.975),
        (0.5, 0.0, 0.5),
        (0.5, 50.0, 0.75),
        (1.0, 0.0, 1.0),
    ],
)
def test_override_replaces_the_tier_base_and_gamble_tolerance_still_applies(override, gamble, expected_cred):
    assert compute_effective_price(40.0, TIER_SHORT_RANGE, gamble, override) == pytest.approx(40.0 / expected_cred)


def test_override_beats_the_tier_default_not_just_adds_to_it():
    assert compute_effective_price(40.0, TIER_SHORT_RANGE, 0.0, 0.5) > compute_effective_price(
        40.0, TIER_SHORT_RANGE, 0.0
    )


@pytest.mark.parametrize("override", [0.0, -1.0, -1e9])
def test_zero_or_negative_override_is_floored_so_the_price_stays_finite(override):
    """Without the floor, base credibility 0 at gamble tolerance 0 would divide by zero."""
    price = compute_effective_price(40.0, TIER_SHORT_RANGE, 0.0, override)
    assert math.isfinite(price) and price > 40.0


@pytest.mark.parametrize("override", [1.0000001, 2.0, 1e9])
def test_override_above_one_is_capped_so_a_forecast_never_outranks_a_real_rate(override):
    assert compute_effective_price(40.0, TIER_SHORT_RANGE, 0.0, override) == 40.0


@pytest.mark.parametrize("override", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_override_falls_back_to_the_tier_default(override):
    assert compute_effective_price(40.0, TIER_SHORT_RANGE, 0.0, override) == pytest.approx(
        40.0 / BASE_CREDIBILITY[TIER_SHORT_RANGE]
    )


def test_assign_credibilities_applies_a_slots_own_override():
    plain = _slot(SOURCE_SHORT_RANGE, 40.0)
    tuned = _slot(SOURCE_SHORT_RANGE, 40.0, base_credibility=0.5)

    a, b = assign_credibilities([plain, tuned], NOW, gamble_tolerance=0.0)

    assert a["tier"] == b["tier"] == TIER_SHORT_RANGE
    assert a["effective_price"] == pytest.approx(40.0 / 0.95)
    assert b["effective_price"] == pytest.approx(40.0 / 0.5)


def test_assign_credibilities_does_not_mutate_its_input():
    original = _slot(SOURCE_SHORT_RANGE, 40.0, base_credibility=0.5)
    snapshot = dict(original)
    assign_credibilities([original], NOW, 50.0)
    assert original == snapshot


def test_actual_and_forecast_slots_ignore_the_absence_of_an_override():
    slots = assign_credibilities([_slot(SOURCE_CURRENT_ACTUAL, 40.0), _slot(SOURCE_PREDICTED, 40.0)], NOW, 0.0)
    assert slots[0]["effective_price"] == 40.0
    assert slots[1]["effective_price"] == pytest.approx(40.0 / 0.9)


@pytest.mark.parametrize(
    "slot,expected",
    [
        ({}, 1.0),  # legacy slot: no tier -> treated as actual
        ({"tier": TIER_ACTUAL}, 1.0),
        ({"tier": TIER_PREDICTED_0_24}, 0.9),
        ({"tier": TIER_SHORT_RANGE}, 0.95),
        ({"tier": TIER_SHORT_RANGE, "base_credibility": 0.8}, 0.8),
        ({"tier": TIER_SHORT_RANGE, "base_credibility": None}, 0.95),
        ({"tier": TIER_SHORT_RANGE, "base_credibility": 7}, 1.0),
        ({"tier": TIER_SHORT_RANGE, "base_credibility": float("nan")}, 0.95),
    ],
)
def test_slot_base_credibility(slot, expected):
    assert slot_base_credibility(slot) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Sessions: confidence + estimated
# ---------------------------------------------------------------------------


def _tiered(dt, source, price=10.0, **extra):
    return assign_credibilities([_slot(source, price, dt, **extra)], NOW, 100.0)[0]


def _run(sources, **extra):
    return [_tiered(SLOT + timedelta(minutes=30 * i), src, **extra) for i, src in enumerate(sources)]


def test_a_session_of_only_actual_rates_is_not_estimated():
    (session,) = slots_to_sessions(_run([SOURCE_CURRENT_ACTUAL, SOURCE_NEXT_ACTUAL]))
    assert session["estimated"] is False
    assert session["confidence"] == 100.0


def test_a_session_of_only_final_short_range_prices_is_not_estimated():
    (session,) = slots_to_sessions(_run([SOURCE_SHORT_RANGE_FINAL] * 3))
    assert session["estimated"] is False
    assert session["confidence"] == 100.0


def test_a_session_of_short_range_estimates_is_estimated_at_the_slots_credibility():
    (session,) = slots_to_sessions(_run([SOURCE_SHORT_RANGE] * 2, base_credibility=0.8))
    assert session["estimated"] is True
    assert session["confidence"] == 80.0


def test_a_session_of_forecast_slots_is_estimated():
    (session,) = slots_to_sessions(_run([SOURCE_PREDICTED]))
    assert session["estimated"] is True


@pytest.mark.parametrize("position", [0, 1, 2])
def test_one_estimated_slot_anywhere_makes_the_whole_block_estimated(position):
    sources = [SOURCE_CURRENT_ACTUAL] * 3
    sources[position] = SOURCE_SHORT_RANGE
    (session,) = slots_to_sessions(_run(sources))
    assert session["estimated"] is True
    assert session["confidence"] == pytest.approx((200 + 95) / 3, abs=0.1)


def test_estimated_is_computed_per_session_not_for_the_whole_schedule():
    actual = _tiered(SLOT, SOURCE_CURRENT_ACTUAL)
    estimate = _tiered(SLOT + timedelta(hours=2), SOURCE_SHORT_RANGE)

    first, second = slots_to_sessions([actual, estimate])

    assert (first["estimated"], second["estimated"]) == (False, True)


def test_sessions_from_slots_without_a_tier_default_to_actual_as_before():
    (session,) = slots_to_sessions([_slot(SOURCE_CURRENT_ACTUAL)])
    assert session["estimated"] is False
    assert session["confidence"] == 100.0


def test_existing_session_fields_are_unchanged():
    (session,) = slots_to_sessions(_run([SOURCE_CURRENT_ACTUAL] * 2))
    assert set(session) == {"start", "end", "duration_hours", "avg_price", "confidence", "estimated"}
    assert session["duration_hours"] == 1.0


# ---------------------------------------------------------------------------
# Diagnostics + end-to-end ranking
# ---------------------------------------------------------------------------


def test_summarize_prices_counts_each_short_range_source_separately():
    slots = [
        _slot(SOURCE_SHORT_RANGE, dt=SLOT),
        _slot(SOURCE_SHORT_RANGE, dt=SLOT + timedelta(minutes=30)),
        _slot(SOURCE_SHORT_RANGE_FINAL, dt=SLOT + timedelta(hours=1)),
        _slot(SOURCE_PREDICTED, dt=SLOT + timedelta(hours=2)),
    ]
    assert summarize_prices(slots, NOW)["source_counts"] == {
        SOURCE_SHORT_RANGE: 2,
        SOURCE_SHORT_RANGE_FINAL: 1,
        SOURCE_PREDICTED: 1,
    }


def _price_series(source, prices, start=SLOT, **extra):
    return [_slot(source, p, start + timedelta(minutes=30 * i), **extra) for i, p in enumerate(prices)]


def test_a_cheaper_short_range_estimate_beats_a_dearer_actual_block_at_high_confidence():
    prices = _price_series(SOURCE_SHORT_RANGE, [30.0, 30.0], base_credibility=0.95)
    prices += _price_series(SOURCE_CURRENT_ACTUAL, [40.0, 40.0], start=SLOT + timedelta(hours=3))
    slots = assign_credibilities(deduplicate_and_sort_prices(prices, NOW), NOW, gamble_tolerance=50.0)

    chosen = find_optimal_slots(slots, 2, NOW + timedelta(hours=10), min_block_hours=1.0)

    assert {s["source"] for s in chosen} == {SOURCE_SHORT_RANGE}


def test_low_confidence_makes_the_same_estimate_lose_to_the_dearer_actual_block():
    """Same prices as above; only the configured confidence differs."""
    prices = _price_series(SOURCE_SHORT_RANGE, [30.0, 30.0], base_credibility=0.5)
    prices += _price_series(SOURCE_CURRENT_ACTUAL, [40.0, 40.0], start=SLOT + timedelta(hours=3))
    slots = assign_credibilities(deduplicate_and_sort_prices(prices, NOW), NOW, gamble_tolerance=0.0)

    chosen = find_optimal_slots(slots, 2, NOW + timedelta(hours=10), min_block_hours=1.0)

    assert {s["source"] for s in chosen} == {SOURCE_CURRENT_ACTUAL}


def test_max_price_is_checked_against_the_raw_price_not_the_confidence_adjusted_one():
    """Confidence reorders candidates; it must never make a 45p block look like a 40p
    one to the cap."""
    prices = _price_series(SOURCE_SHORT_RANGE, [45.0, 45.0], base_credibility=1.0)
    slots = assign_credibilities(prices, NOW, gamble_tolerance=100.0)

    assert find_optimal_slots(slots, 2, NOW + timedelta(hours=10), min_block_hours=1.0, max_price=40.0) == []
    assert len(find_optimal_slots(slots, 2, NOW + timedelta(hours=10), min_block_hours=1.0, max_price=45.0)) == 2


@pytest.mark.parametrize("algorithm", ["greedy", "optimal", "hybrid"])
def test_every_algorithm_handles_mixed_source_slots(algorithm):
    prices = _price_series(SOURCE_SHORT_RANGE, [10.0] * 4, base_credibility=0.9)
    prices += _price_series(SOURCE_PREDICTED, [12.0] * 8, start=SLOT + timedelta(hours=2))
    prices += _price_series(SOURCE_SHORT_RANGE_FINAL, [11.0] * 2, start=SLOT + timedelta(hours=6))
    slots = assign_credibilities(deduplicate_and_sort_prices(prices, NOW), NOW, 50.0)

    chosen = find_optimal_slots(slots, 4, NOW + timedelta(hours=20), min_block_hours=1.0, algorithm=algorithm)

    assert len(chosen) == 4
    assert all(s["raw_price"] <= 12.0 for s in chosen)
