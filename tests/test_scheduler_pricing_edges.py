"""Unusual prices: negative (Agile "plunge pricing"), zero, tiny, huge, ties.

The risk adjustment exists so a doubtful (predicted) price is *less attractive* than an equal
real one. That must hold for every sign of price -- dividing a negative price by a credibility
below 1 would make it more negative, i.e. more attractive, the exact opposite."""

from datetime import datetime, timedelta

import pytest

from custom_components.wholesale_ev_schedule.scheduler import (
    TIER_ACTUAL,
    TIER_PREDICTED_0_24,
    TIER_PREDICTED_24_48,
    TIER_PREDICTED_48_72,
    TIER_PREDICTED_72_PLUS,
    TIER_SHORT_RANGE,
    assign_credibilities,
    compute_effective_price,
    deduplicate_and_sort_prices,
    find_optimal_slots,
    slots_to_sessions,
    summarize_prices,
)

NOW = datetime(2026, 3, 2, 20, 0)
READY_BY = NOW + timedelta(hours=30)
PREDICTED_TIERS = [
    TIER_SHORT_RANGE,
    TIER_PREDICTED_0_24,
    TIER_PREDICTED_24_48,
    TIER_PREDICTED_48_72,
    TIER_PREDICTED_72_PLUS,
]
PRICES = [-50.0, -12.5, -0.01, 0.0, 0.01, 7.0, 40.0, 250.0]


@pytest.mark.parametrize("raw", PRICES)
@pytest.mark.parametrize("tier", PREDICTED_TIERS)
@pytest.mark.parametrize("gamble", [0.0, 25.0, 50.0, 99.0])
def test_a_predicted_price_is_never_more_attractive_than_the_same_real_price(raw, tier, gamble):
    assert compute_effective_price(raw, tier, gamble) >= compute_effective_price(raw, TIER_ACTUAL, gamble)


@pytest.mark.parametrize("raw", PRICES)
def test_a_more_distant_forecast_is_never_more_attractive_than_a_nearer_one(raw):
    tiers = [TIER_ACTUAL, TIER_PREDICTED_0_24, TIER_PREDICTED_24_48, TIER_PREDICTED_48_72, TIER_PREDICTED_72_PLUS]
    effective = [compute_effective_price(raw, t, 20.0) for t in tiers]
    assert effective == sorted(effective)


@pytest.mark.parametrize("raw", PRICES)
def test_trusting_forecasts_more_never_makes_them_less_attractive(raw):
    effective = [compute_effective_price(raw, TIER_PREDICTED_48_72, g) for g in (0.0, 25.0, 50.0, 75.0, 100.0)]
    assert effective == sorted(effective, reverse=True)


@pytest.mark.parametrize("raw", PRICES)
def test_at_full_gamble_tolerance_every_price_is_taken_at_face_value(raw):
    for tier in PREDICTED_TIERS:
        assert compute_effective_price(raw, tier, 100.0) == pytest.approx(raw)


@pytest.mark.parametrize("raw", PRICES)
def test_actual_rates_are_never_adjusted(raw):
    assert compute_effective_price(raw, TIER_ACTUAL, 0.0) == raw


def test_the_adjustment_is_continuous_through_zero():
    just_below = compute_effective_price(-1e-9, TIER_PREDICTED_72_PLUS, 0.0)
    just_above = compute_effective_price(1e-9, TIER_PREDICTED_72_PLUS, 0.0)
    assert abs(just_above - just_below) < 1e-6


def _slot(dt, price, source):
    return {"date_time": dt, "raw_price": price, "source": source}


def _series(price, source, start, count=4):
    return [_slot(start + timedelta(minutes=30 * i), price, source) for i in range(count)]


@pytest.mark.parametrize("algorithm", ["greedy", "optimal", "hybrid"])
def test_equal_negative_prices_prefer_the_real_one_over_the_forecast(algorithm):
    real = _series(-5.0, "current_actual", NOW + timedelta(hours=2))
    guess = _series(-5.0, "predicted", NOW + timedelta(hours=6))
    slots = assign_credibilities(deduplicate_and_sort_prices(real + guess, NOW), NOW, gamble_tolerance=30.0)

    chosen = find_optimal_slots(slots, 4, READY_BY, min_block_hours=1.0, algorithm=algorithm)

    assert {s["source"] for s in chosen} == {"current_actual"}


@pytest.mark.parametrize("algorithm", ["greedy", "optimal", "hybrid"])
def test_a_clearly_deeper_negative_forecast_still_wins_over_a_barely_negative_real_rate(algorithm):
    real = _series(-0.5, "current_actual", NOW + timedelta(hours=2))
    guess = _series(-60.0, "predicted", NOW + timedelta(hours=6))
    slots = assign_credibilities(deduplicate_and_sort_prices(real + guess, NOW), NOW, gamble_tolerance=50.0)

    chosen = find_optimal_slots(slots, 4, READY_BY, min_block_hours=1.0, algorithm=algorithm)

    assert {s["source"] for s in chosen} == {"predicted"}


@pytest.mark.parametrize("algorithm", ["greedy", "optimal", "hybrid"])
def test_negative_prices_are_picked_over_positive_ones_and_over_zero(algorithm):
    prices = [10.0, 10.0, -3.0, -3.0, -3.0, -3.0, 0.0, 0.0, 10.0, 10.0]
    slots = assign_credibilities(
        deduplicate_and_sort_prices(
            [_slot(NOW + timedelta(hours=1, minutes=30 * i), p, "current_actual") for i, p in enumerate(prices)], NOW
        ),
        NOW,
        100.0,
    )

    chosen = find_optimal_slots(slots, 4, READY_BY, min_block_hours=1.0, algorithm=algorithm)

    assert [s["raw_price"] for s in chosen] == [-3.0] * 4


@pytest.mark.parametrize("algorithm", ["greedy", "optimal", "hybrid"])
def test_a_block_averaging_exactly_zero_or_below_satisfies_a_zero_cap(algorithm):
    slots = assign_credibilities(
        deduplicate_and_sort_prices(
            [
                _slot(NOW + timedelta(hours=1, minutes=30 * i), p, "current_actual")
                for i, p in enumerate([-4.0, 4.0, 5.0, 5.0])
            ],
            NOW,
        ),
        NOW,
        100.0,
    )

    chosen = find_optimal_slots(slots, 2, READY_BY, min_block_hours=1.0, max_price=0.0, algorithm=algorithm)

    assert [s["raw_price"] for s in chosen] == [-4.0, 4.0]


@pytest.mark.parametrize("algorithm", ["greedy", "optimal", "hybrid"])
def test_a_cap_below_zero_admits_only_negative_blocks(algorithm):
    slots = assign_credibilities(
        deduplicate_and_sort_prices(
            [
                _slot(NOW + timedelta(hours=1, minutes=30 * i), p, "current_actual")
                for i, p in enumerate([3.0, -2.0, -1.0, 3.0])
            ],
            NOW,
        ),
        NOW,
        100.0,
    )

    assert [
        s["raw_price"] for s in find_optimal_slots(slots, 2, READY_BY, 1.0, max_price=-0.5, algorithm=algorithm)
    ] == [-2.0, -1.0]
    assert find_optimal_slots(slots, 2, READY_BY, 1.0, max_price=-5.0, algorithm=algorithm) == []


def test_session_average_and_summary_handle_negative_prices():
    slots = [
        _slot(NOW + timedelta(hours=1, minutes=30 * i), p, "current_actual") for i, p in enumerate([-4.0, -2.0, 6.0])
    ]
    (session,) = slots_to_sessions(assign_credibilities(slots, NOW, 100.0))
    summary = summarize_prices(slots, NOW)

    assert session["avg_price"] == 0.0
    assert (
        summary["cheapest_price"] == -4.0 and summary["most_expensive_price"] == 6.0 and summary["average_price"] == 0.0
    )


@pytest.mark.parametrize("algorithm", ["greedy", "optimal", "hybrid"])
@pytest.mark.parametrize("price", [0.0, 1e-9, 1e9, -1e9])
def test_a_flat_market_of_any_magnitude_schedules_the_requirement(algorithm, price):
    slots = assign_credibilities(
        deduplicate_and_sort_prices(_series(price, "current_actual", NOW + timedelta(hours=1), count=12), NOW),
        NOW,
        50.0,
    )

    chosen = find_optimal_slots(slots, 6, READY_BY, min_block_hours=1.0, algorithm=algorithm)

    assert len(chosen) == 6
