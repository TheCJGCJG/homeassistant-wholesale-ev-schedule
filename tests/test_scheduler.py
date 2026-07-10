"""Unit tests for the pure scheduling logic in scheduler.py.

These functions have no Home Assistant dependency, so they're tested directly
without any hass fixture — same approach as the upstream pyscript's test suite
this module was ported from.
"""

from datetime import datetime, timedelta

import pytest

from custom_components.wholesale_ev_schedule.scheduler import (
    BASE_CREDIBILITY,
    TIER_ACTUAL,
    TIER_PREDICTED_0_24,
    TIER_PREDICTED_24_48,
    TIER_PREDICTED_48_72,
    TIER_PREDICTED_72_PLUS,
    assign_credibilities,
    build_contiguous_runs,
    compute_effective_price,
    compute_hours_remaining,
    deduplicate_and_sort_prices,
    determine_state,
    find_optimal_slots,
    get_source_tier,
    next_ready_by,
    parse_dt,
    parse_iso_str,
    prune_and_classify,
    slots_to_sessions,
    summarize_prices,
)

NOW = datetime(2024, 1, 15, 10, 0)


def make_slots(start, count, price=10.0, source="current_actual", step_minutes=30):
    """Build `count` consecutive 30-min price slots starting at `start`."""
    return [
        {
            "date_time": start + timedelta(minutes=step_minutes * i),
            "raw_price": price,
            "source": source,
        }
        for i in range(count)
    ]


# ---------------------------------------------------------------------------
# get_source_tier / compute_effective_price
# ---------------------------------------------------------------------------


def test_get_source_tier_actual_sources_always_tier_actual():
    assert get_source_tier("current_actual", NOW + timedelta(days=10), NOW) == TIER_ACTUAL
    assert get_source_tier("next_actual", NOW + timedelta(days=10), NOW) == TIER_ACTUAL


@pytest.mark.parametrize(
    "hours_ahead,expected",
    [
        (1, TIER_PREDICTED_0_24),
        (24, TIER_PREDICTED_0_24),
        (25, TIER_PREDICTED_24_48),
        (48, TIER_PREDICTED_24_48),
        (49, TIER_PREDICTED_48_72),
        (72, TIER_PREDICTED_48_72),
        (73, TIER_PREDICTED_72_PLUS),
    ],
)
def test_get_source_tier_predicted_horizon_boundaries(hours_ahead, expected):
    slot_dt = NOW + timedelta(hours=hours_ahead)
    assert get_source_tier("predicted", slot_dt, NOW) == expected


def test_compute_effective_price_face_value_at_full_gamble_tolerance():
    price = compute_effective_price(10.0, TIER_PREDICTED_72_PLUS, gamble_tolerance=100.0)
    assert price == pytest.approx(10.0)


def test_compute_effective_price_inflated_at_zero_gamble_tolerance():
    price = compute_effective_price(10.0, TIER_PREDICTED_72_PLUS, gamble_tolerance=0.0)
    assert price == pytest.approx(10.0 / BASE_CREDIBILITY[TIER_PREDICTED_72_PLUS])


def test_compute_effective_price_actual_tier_always_face_value():
    for tolerance in (0.0, 50.0, 100.0):
        assert compute_effective_price(12.5, TIER_ACTUAL, tolerance) == pytest.approx(12.5)


def test_assign_credibilities_adds_tier_and_effective_price():
    slots = make_slots(NOW, 2, source="predicted")
    result = assign_credibilities(slots, NOW, gamble_tolerance=50.0)
    assert all("tier" in s and "effective_price" in s for s in result)
    assert slots[0].get("tier") is None  # original list untouched


# ---------------------------------------------------------------------------
# build_contiguous_runs
# ---------------------------------------------------------------------------


def test_build_contiguous_runs_single_run():
    slots = make_slots(NOW, 4)
    runs = build_contiguous_runs(slots)
    assert len(runs) == 1
    assert len(runs[0]) == 4


def test_build_contiguous_runs_splits_on_gap():
    slots = make_slots(NOW, 2) + make_slots(NOW + timedelta(hours=2), 2)
    runs = build_contiguous_runs(slots)
    assert len(runs) == 2
    assert len(runs[0]) == 2 and len(runs[1]) == 2


def test_build_contiguous_runs_empty_input():
    assert build_contiguous_runs([]) == []


# ---------------------------------------------------------------------------
# find_optimal_slots / slots_to_sessions
# ---------------------------------------------------------------------------


def test_find_optimal_slots_picks_cheapest_window():
    cheap = make_slots(NOW + timedelta(hours=4), 2, price=5.0)
    expensive = make_slots(NOW, 2, price=50.0)
    adjusted = assign_credibilities(cheap + expensive, NOW, gamble_tolerance=100.0)

    result = find_optimal_slots(adjusted, required_slots=2, ready_by_dt=NOW + timedelta(hours=10), min_block_hours=1.0)

    assert len(result) == 2
    assert all(s["raw_price"] == 5.0 for s in result)


def test_find_optimal_slots_respects_ready_by():
    slots = assign_credibilities(make_slots(NOW + timedelta(hours=20), 2, price=1.0), NOW, 100.0)
    result = find_optimal_slots(slots, required_slots=2, ready_by_dt=NOW + timedelta(hours=5), min_block_hours=1.0)
    assert result == []


def test_find_optimal_slots_max_price_excludes_expensive_windows():
    slots = assign_credibilities(make_slots(NOW, 2, price=30.0), NOW, 100.0)
    result = find_optimal_slots(
        slots,
        required_slots=2,
        ready_by_dt=NOW + timedelta(hours=5),
        min_block_hours=1.0,
        max_price=20.0,
    )
    assert result == []


def test_find_optimal_slots_returns_empty_for_zero_required():
    slots = assign_credibilities(make_slots(NOW, 2), NOW, 100.0)
    assert find_optimal_slots(slots, 0, NOW + timedelta(hours=5), 1.0) == []


def _priced_slot(dt, price):
    return {"date_time": dt, "raw_price": price, "source": "current_actual"}


def test_find_optimal_slots_skips_overlap_then_fills_via_relaxed_fallback():
    # A 4-slot run priced [5, 1, 1, 5]: the cheap middle pair (avg=1) is the
    # best 2-slot window and gets picked first, leaving 2 slots still needed.
    # Every remaining window is then rejected — the 3- and 4-slot windows are
    # now too large for what's left (`if w['size'] > remaining: continue`),
    # and the other 2-slot windows overlap the selection (`if w['dts'] &
    # selected_dts: continue`) — so the run is exhausted without filling the
    # full 4-slot requirement, forcing the relaxed fallback to top up the
    # remainder with the two leftover (pricier) slots.
    run = [
        _priced_slot(NOW, 5.0),
        _priced_slot(NOW + timedelta(minutes=30), 1.0),
        _priced_slot(NOW + timedelta(hours=1), 1.0),
        _priced_slot(NOW + timedelta(hours=1, minutes=30), 5.0),
    ]
    slots = assign_credibilities(run, NOW, gamble_tolerance=100.0)

    result = find_optimal_slots(slots, required_slots=4, ready_by_dt=NOW + timedelta(hours=5), min_block_hours=1.0)

    assert {s["date_time"] for s in result} == {s["date_time"] for s in run}


def test_find_optimal_slots_skips_windows_that_would_leave_an_unfillable_gap():
    # A 3-slot run, all tied at the same price, with min_block=1h (2 slots).
    # Either 2-slot sub-window would leave a 1-slot remainder — smaller than
    # the minimum block — so both must be skipped (`if after > 0 and after <
    # min_slots_per_block: continue`) in favour of the single 3-slot window
    # that exactly satisfies the requirement in one block.
    run = make_slots(NOW, 3, price=1.0)
    slots = assign_credibilities(run, NOW, gamble_tolerance=100.0)

    result = find_optimal_slots(slots, required_slots=3, ready_by_dt=NOW + timedelta(hours=5), min_block_hours=1.0)

    assert len(result) == 3
    assert build_contiguous_runs(sorted(result, key=lambda s: s["date_time"])) == [
        sorted(result, key=lambda s: s["date_time"])
    ]


def test_find_optimal_slots_skips_a_window_too_large_for_the_remaining_need():
    # Run A (2 slots @ 1p) is cheapest and gets picked first, leaving 2 slots
    # still needed. Run B (3 slots @ 1p/5p/1p) has a 3-slot window whose
    # average (2.33p) beats its own 2-slot sub-windows (3p each) and so sorts
    # next — but its size (3) now exceeds what's left to fill (2), so it must
    # be skipped (`if w['size'] > remaining: continue`) in favour of the
    # cheapest 2-slot sub-window that actually fits.
    run_a = [_priced_slot(NOW, 1.0), _priced_slot(NOW + timedelta(minutes=30), 1.0)]
    run_b_start = NOW + timedelta(hours=5)
    run_b = [
        _priced_slot(run_b_start, 1.0),
        _priced_slot(run_b_start + timedelta(minutes=30), 5.0),
        _priced_slot(run_b_start + timedelta(hours=1), 1.0),
    ]
    slots = assign_credibilities(run_a + run_b, NOW, gamble_tolerance=100.0)

    result = find_optimal_slots(slots, required_slots=4, ready_by_dt=NOW + timedelta(hours=10), min_block_hours=1.0)

    result_dts = {s["date_time"] for s in result}
    assert result_dts == {run_a[0]["date_time"], run_a[1]["date_time"], run_b[0]["date_time"], run_b[1]["date_time"]}
    # The expensive middle slot of run_b, only reachable via the too-large
    # 3-slot window, must NOT be in the result.
    assert run_b[2]["date_time"] not in result_dts


def test_find_optimal_slots_caps_individual_window_size_with_max_block_hours():
    # A single uniformly-cheap 4-slot (2h) run, required=4 slots, capped at 1h
    # (2 slots) per block. The cap limits how large any one *window* can be
    # during selection, but doesn't force an artificial rest period — since
    # all 4 slots are equally cheap and adjacent, the two capped windows the
    # optimizer picks still end up back-to-back with no gap between them
    # (see the max_block_hours docstring). What the cap does guarantee is
    # that no single window's price average spans more than the cap.
    run = make_slots(NOW, 4, price=1.0)
    slots = assign_credibilities(run, NOW, gamble_tolerance=100.0)

    result = find_optimal_slots(
        slots,
        required_slots=4,
        ready_by_dt=NOW + timedelta(hours=5),
        min_block_hours=0.5,
        max_block_hours=1.0,
    )

    assert {s["date_time"] for s in result} == {s["date_time"] for s in run}


def test_find_optimal_slots_max_block_hours_produces_a_real_gap_around_a_price_spike():
    # Six contiguous slots priced [1, 1, 5, 5, 1, 1] — two genuinely separate
    # cheap dips either side of an expensive middle pair. required=4 slots
    # (2h), max_block_hours=1h (2 slots): the optimizer should pick the two
    # cheap pairs and skip the expensive middle, landing as two separate
    # sessions with a real gap between them.
    prices = [1.0, 1.0, 5.0, 5.0, 1.0, 1.0]
    run = [
        {"date_time": NOW + timedelta(minutes=30 * i), "raw_price": p, "source": "current_actual"}
        for i, p in enumerate(prices)
    ]
    slots = assign_credibilities(run, NOW, gamble_tolerance=100.0)

    result = find_optimal_slots(
        slots,
        required_slots=4,
        ready_by_dt=NOW + timedelta(hours=5),
        min_block_hours=1.0,
        max_block_hours=1.0,
    )

    sessions = build_contiguous_runs(sorted(result, key=lambda s: s["date_time"]))
    assert len(sessions) == 2
    assert all(len(session) == 2 for session in sessions)
    picked_prices = {s["raw_price"] for s in result}
    assert picked_prices == {1.0}  # the expensive middle pair must be excluded


def test_find_optimal_slots_max_block_hours_none_is_unlimited():
    run = make_slots(NOW, 4, price=1.0)
    slots = assign_credibilities(run, NOW, gamble_tolerance=100.0)

    result = find_optimal_slots(
        slots,
        required_slots=4,
        ready_by_dt=NOW + timedelta(hours=5),
        min_block_hours=0.5,
        max_block_hours=None,
    )

    sessions = build_contiguous_runs(sorted(result, key=lambda s: s["date_time"]))
    assert len(sessions) == 1  # no cap -> the whole run is offered as one block


def test_find_optimal_slots_max_block_hours_zero_means_unlimited():
    # 0 is the "unlimited" sentinel used by the live number entity (its
    # minimum value), matching the None case above.
    run = make_slots(NOW, 4, price=1.0)
    slots = assign_credibilities(run, NOW, gamble_tolerance=100.0)

    result = find_optimal_slots(
        slots,
        required_slots=4,
        ready_by_dt=NOW + timedelta(hours=5),
        min_block_hours=0.5,
        max_block_hours=0,
    )

    sessions = build_contiguous_runs(sorted(result, key=lambda s: s["date_time"]))
    assert len(sessions) == 1


def test_slots_to_sessions_groups_contiguous_runs():
    run1 = make_slots(NOW, 2, price=10.0)
    run2 = make_slots(NOW + timedelta(hours=3), 2, price=20.0)
    sessions = slots_to_sessions(run1 + run2)

    assert len(sessions) == 2
    assert sessions[0]["duration_hours"] == 1.0
    assert sessions[0]["avg_price"] == 10.0
    assert sessions[1]["avg_price"] == 20.0


def test_slots_to_sessions_confidence_reflects_actual_rates():
    sessions = slots_to_sessions(make_slots(NOW, 2, source="current_actual"))
    assert sessions[0]["confidence"] == 100.0


def test_slots_to_sessions_empty_input():
    assert slots_to_sessions([]) == []


# ---------------------------------------------------------------------------
# parse_dt / parse_iso_str
# ---------------------------------------------------------------------------


def test_parse_iso_str_handles_z_suffix():
    assert parse_iso_str("2024-01-15T10:00:00Z") == parse_iso_str("2024-01-15T10:00:00+00:00")


def test_parse_dt_passthrough_for_datetime_objects():
    assert parse_dt(NOW) is NOW


def test_parse_dt_parses_string():
    assert parse_dt(NOW.isoformat()) == NOW


# ---------------------------------------------------------------------------
# prune_and_classify / compute_hours_remaining
# ---------------------------------------------------------------------------


def _session(start, end, duration_hours=None):
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "duration_hours": duration_hours if duration_hours is not None else (end - start).total_seconds() / 3600,
        "avg_price": 10.0,
        "confidence": 100.0,
    }


def test_prune_and_classify_splits_active_and_future():
    active = _session(NOW - timedelta(minutes=10), NOW + timedelta(minutes=20))
    future = _session(NOW + timedelta(hours=1), NOW + timedelta(hours=2))
    expired = _session(NOW - timedelta(hours=2), NOW - timedelta(hours=1))

    active_result, future_result = prune_and_classify([active, future, expired], NOW)

    assert active_result == active
    assert future_result == [future]


def test_prune_and_classify_no_active_session():
    future = _session(NOW + timedelta(hours=1), NOW + timedelta(hours=2))
    active_result, future_result = prune_and_classify([future], NOW)
    assert active_result is None
    assert future_result == [future]


def test_compute_hours_remaining_counts_partial_active_plus_future():
    active = _session(NOW - timedelta(minutes=30), NOW + timedelta(minutes=30))
    future = _session(NOW + timedelta(hours=1), NOW + timedelta(hours=2))
    remaining = compute_hours_remaining([future], active, NOW)
    assert remaining == pytest.approx(1.5)


def test_compute_hours_remaining_zero_when_nothing_scheduled():
    assert compute_hours_remaining([], None, NOW) == 0.0


# ---------------------------------------------------------------------------
# determine_state
# ---------------------------------------------------------------------------


def test_determine_state_error_when_data_not_ok():
    assert determine_state(1.0, False, None, [], 0.0, data_ok=False) == "error"


def test_determine_state_idle_when_no_hours_required():
    assert determine_state(0.0, False, None, [], 0.0, data_ok=True) == "idle"


def test_determine_state_boosting_takes_priority_over_active_session():
    assert determine_state(2.0, True, {"start": "x"}, [], 1.0, data_ok=True) == "boosting"


def test_determine_state_charging_when_active_session():
    assert determine_state(2.0, False, {"start": "x"}, [], 1.0, data_ok=True) == "charging"


def test_determine_state_scheduled_when_future_sessions_only():
    assert determine_state(2.0, False, None, [{"start": "x"}], 1.0, data_ok=True) == "scheduled"


def test_determine_state_complete_when_nothing_left():
    assert determine_state(2.0, False, None, [], 0.0, data_ok=True) == "complete"


def test_determine_state_error_when_unschedulable_remainder():
    assert determine_state(2.0, False, None, [], 1.0, data_ok=True) == "error"


# ---------------------------------------------------------------------------
# deduplicate_and_sort_prices
# ---------------------------------------------------------------------------


def test_deduplicate_actual_wins_over_predicted_for_same_slot():
    predicted = {"date_time": NOW, "raw_price": 99.0, "source": "predicted"}
    actual = {"date_time": NOW, "raw_price": 5.0, "source": "current_actual"}

    result = deduplicate_and_sort_prices([predicted, actual], NOW - timedelta(minutes=10))

    assert len(result) == 1
    assert result[0]["source"] == "current_actual"


def test_deduplicate_drops_past_slots():
    past = {"date_time": NOW - timedelta(hours=2), "raw_price": 1.0, "source": "current_actual"}
    future = {"date_time": NOW + timedelta(hours=1), "raw_price": 1.0, "source": "current_actual"}

    result = deduplicate_and_sort_prices([past, future], NOW)

    assert result == [future]


def test_deduplicate_sorts_chronologically():
    later = {"date_time": NOW + timedelta(hours=2), "raw_price": 1.0, "source": "current_actual"}
    earlier = {"date_time": NOW + timedelta(hours=1), "raw_price": 1.0, "source": "current_actual"}

    result = deduplicate_and_sort_prices([later, earlier], NOW)

    assert [r["date_time"] for r in result] == [earlier["date_time"], later["date_time"]]


# ---------------------------------------------------------------------------
# summarize_prices
# ---------------------------------------------------------------------------


def test_summarize_prices_empty():
    assert summarize_prices([], NOW) == {
        "count": 0,
        "cheapest_price": None,
        "most_expensive_price": None,
        "average_price": None,
        "average_price_next_window": None,
        "source_counts": {},
    }


def test_summarize_prices_basic_stats():
    slots = [
        {"date_time": NOW, "raw_price": 10.0, "source": "current_actual"},
        {"date_time": NOW + timedelta(minutes=30), "raw_price": 20.0, "source": "current_actual"},
        {"date_time": NOW + timedelta(hours=1), "raw_price": 30.0, "source": "predicted"},
    ]
    summary = summarize_prices(slots, NOW, window_hours=24.0)

    assert summary["count"] == 3
    assert summary["cheapest_price"] == 10.0
    assert summary["most_expensive_price"] == 30.0
    assert summary["average_price"] == 20.0
    assert summary["source_counts"] == {"current_actual": 2, "predicted": 1}


def test_summarize_prices_next_window_excludes_slots_outside_the_window():
    inside = {"date_time": NOW + timedelta(hours=1), "raw_price": 10.0, "source": "current_actual"}
    outside = {"date_time": NOW + timedelta(hours=48), "raw_price": 1000.0, "source": "predicted"}

    summary = summarize_prices([inside, outside], NOW, window_hours=24.0)

    assert summary["average_price"] == 505.0  # both slots, unrestricted
    assert summary["average_price_next_window"] == 10.0  # only the in-window slot


def test_summarize_prices_next_window_none_when_nothing_in_window():
    outside = {"date_time": NOW + timedelta(hours=48), "raw_price": 10.0, "source": "predicted"}
    summary = summarize_prices([outside], NOW, window_hours=24.0)
    assert summary["average_price_next_window"] is None


# ---------------------------------------------------------------------------
# next_ready_by
# ---------------------------------------------------------------------------


def test_next_ready_by_rolls_to_tomorrow_when_past_the_hour_today():
    now = datetime(2024, 1, 15, 15, 0)  # 3pm -- 7am has already passed today
    assert next_ready_by(now, hour=7) == datetime(2024, 1, 16, 7, 0)


def test_next_ready_by_stays_today_when_before_the_hour():
    now = datetime(2024, 1, 15, 2, 0)  # 2am -- 7am is still later today
    assert next_ready_by(now, hour=7) == datetime(2024, 1, 15, 7, 0)


def test_next_ready_by_rolls_forward_at_the_exact_hour():
    now = datetime(2024, 1, 15, 7, 0)  # exactly 7am -- must not return "now"
    assert next_ready_by(now, hour=7) == datetime(2024, 1, 16, 7, 0)


def test_next_ready_by_respects_custom_hour():
    now = datetime(2024, 1, 15, 10, 0)
    assert next_ready_by(now, hour=22) == datetime(2024, 1, 15, 22, 0)


# min_day_offset -- setup-time "Next day / Next day + 1/2/3" default ready-by
# options (issue #2). Default (0, omitted above) must stay byte-identical to
# the pre-existing behaviour, which the tests above already cover unmodified.


def test_next_ready_by_day_offset_zero_matches_default_behaviour():
    now = datetime(2024, 1, 15, 2, 0)
    assert next_ready_by(now, hour=7, min_day_offset=0) == next_ready_by(now, hour=7)


def test_next_ready_by_day_offset_one_forces_tomorrow_even_if_hour_not_yet_passed():
    now = datetime(2024, 1, 15, 2, 0)  # 2am -- 7am hasn't happened yet today
    assert next_ready_by(now, hour=7, min_day_offset=1) == datetime(2024, 1, 16, 7, 0)


def test_next_ready_by_day_offset_one_matches_default_when_hour_already_passed():
    now = datetime(2024, 1, 15, 15, 0)  # 3pm -- 7am already passed today
    # Offset=1 ("at least tomorrow") and offset=0 ("as soon as possible")
    # land on the same day here, since "as soon as possible" is already
    # tomorrow once the hour has passed today.
    assert next_ready_by(now, hour=7, min_day_offset=1) == datetime(2024, 1, 16, 7, 0)


def test_next_ready_by_day_offset_two():
    now = datetime(2024, 1, 15, 2, 0)
    assert next_ready_by(now, hour=7, min_day_offset=2) == datetime(2024, 1, 17, 7, 0)


def test_next_ready_by_day_offset_three():
    now = datetime(2024, 1, 15, 2, 0)
    assert next_ready_by(now, hour=7, min_day_offset=3) == datetime(2024, 1, 18, 7, 0)


def test_next_ready_by_day_offset_rolls_forward_at_the_exact_hour():
    now = datetime(2024, 1, 15, 7, 0)  # exactly 7am -- must not return "now"
    assert next_ready_by(now, hour=7, min_day_offset=1) == datetime(2024, 1, 16, 7, 0)
