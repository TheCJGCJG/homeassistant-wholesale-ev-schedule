"""The scheduler across clock changes, with real timezone-aware local datetimes.

Python compares and subtracts two aware datetimes that share a tzinfo by their *wall clock*,
ignoring the UTC offset, and puts the repeated autumn hour on one dictionary key. Slots built
from local times therefore silently lose or mis-measure time across a DST change unless the
scheduler works in absolute (UTC) time. Every test here builds its slots the way Home Assistant
hands them over -- UTC instants converted into the user's zone -- so a regression shows up
exactly as it would in a real install.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.wholesale_ev_schedule.scheduler import (
    assign_credibilities,
    build_contiguous_runs,
    compute_hours_remaining,
    deduplicate_and_sort_prices,
    find_optimal_slots,
    get_source_tier,
    prune_and_classify,
    slots_to_sessions,
    summarize_prices,
)

UTC = UTC
LONDON = ZoneInfo("Europe/London")
NEW_YORK = ZoneInfo("America/New_York")
SYDNEY = ZoneInfo("Australia/Sydney")
LORD_HOWE = ZoneInfo("Australia/Lord_Howe")

# (zone, first UTC instant of a window straddling a clock change, half-hours in it)
# London autumn: BST ends 2026-10-25 01:00 UTC -> the local hour 01:00-02:00 happens twice.
# London spring: GMT -> BST at 2026-03-29 01:00 UTC -> local 01:00-02:00 never happens.
_CHANGES = {
    "london_autumn": (LONDON, datetime(2026, 10, 24, 22, 0, tzinfo=UTC)),
    "london_spring": (LONDON, datetime(2026, 3, 28, 22, 0, tzinfo=UTC)),
    "new_york_autumn": (NEW_YORK, datetime(2026, 11, 1, 3, 0, tzinfo=UTC)),
    "new_york_spring": (NEW_YORK, datetime(2026, 3, 8, 5, 0, tzinfo=UTC)),
    "sydney_dst_ends": (SYDNEY, datetime(2026, 4, 4, 14, 0, tzinfo=UTC)),
    "lord_howe_30_minute_shift": (LORD_HOWE, datetime(2026, 4, 4, 13, 0, tzinfo=UTC)),
}


def _slots(zone, first_utc, prices, source="current_actual"):
    return [
        {
            "date_time": (first_utc + timedelta(minutes=30 * i)).astimezone(zone),
            "raw_price": price,
            "source": source,
        }
        for i, price in enumerate(prices)
    ]


def _utc(dt):
    return dt.astimezone(UTC)


def _now(first_utc, zone):
    return (first_utc - timedelta(hours=2)).astimezone(zone)


ZONES = pytest.mark.parametrize("change", list(_CHANGES), ids=list(_CHANGES))


@ZONES
def test_dedupe_keeps_every_half_hour_across_the_change(change):
    zone, first = _CHANGES[change]
    slots = _slots(zone, first, [10.0] * 16)

    merged = deduplicate_and_sort_prices(slots, _now(first, zone))

    assert len(merged) == 16
    assert [_utc(s["date_time"]) for s in merged] == [first + timedelta(minutes=30 * i) for i in range(16)]


@ZONES
def test_a_continuous_run_stays_one_run_across_the_change(change):
    zone, first = _CHANGES[change]
    slots = _slots(zone, first, [10.0] * 16)

    runs = build_contiguous_runs(slots)

    assert [len(r) for r in runs] == [16]


@ZONES
def test_a_gap_in_the_data_still_splits_a_run_across_the_change(change):
    zone, first = _CHANGES[change]
    slots = _slots(zone, first, [10.0] * 16)
    del slots[8]

    assert [len(r) for r in build_contiguous_runs(slots)] == [8, 7]


@ZONES
def test_session_duration_is_the_real_elapsed_time(change):
    zone, first = _CHANGES[change]
    slots = _slots(zone, first, [10.0] * 8)

    (session,) = slots_to_sessions(slots)

    start, end = datetime.fromisoformat(session["start"]), datetime.fromisoformat(session["end"])
    assert (end - start) == timedelta(hours=4)
    assert session["duration_hours"] == 4.0
    assert _utc(start) == first and _utc(end) == first + timedelta(hours=4)


@ZONES
@pytest.mark.parametrize("algorithm", ["greedy", "optimal", "hybrid"])
def test_every_algorithm_schedules_through_the_change_with_correct_times(change, algorithm):
    zone, first = _CHANGES[change]
    # cheap in the middle of the window, which straddles the change
    prices = [30.0] * 4 + [5.0] * 8 + [30.0] * 4
    now = _now(first, zone)
    slots = assign_credibilities(deduplicate_and_sort_prices(_slots(zone, first, prices), now), now, 100.0)

    chosen = find_optimal_slots(
        slots, 8, now + timedelta(hours=40), min_block_hours=1.0, max_price=20.0, algorithm=algorithm
    )

    assert [_utc(s["date_time"]) for s in chosen] == [first + timedelta(minutes=30 * (4 + i)) for i in range(8)]
    (session,) = slots_to_sessions(chosen)
    assert session["duration_hours"] == 4.0
    assert _utc(datetime.fromisoformat(session["end"])) - _utc(datetime.fromisoformat(session["start"])) == timedelta(
        hours=4
    )


@ZONES
def test_ready_by_is_an_absolute_instant_not_a_wall_clock_time(change):
    """A slot is eligible only if it *ends* by ready_by -- measured in real time."""
    zone, first = _CHANGES[change]
    now = _now(first, zone)
    slots = assign_credibilities(deduplicate_and_sort_prices(_slots(zone, first, [10.0] * 16), now), now, 100.0)
    ready_by = (first + timedelta(hours=4)).astimezone(zone)  # 4 real hours into the window

    chosen = find_optimal_slots(slots, 8, ready_by, min_block_hours=0.5)

    assert [_utc(s["date_time"]) for s in chosen] == [first + timedelta(minutes=30 * i) for i in range(8)]
    assert find_optimal_slots(slots, 9, ready_by, min_block_hours=0.5) == []


@ZONES
def test_source_tier_horizons_use_real_elapsed_hours(change):
    zone, first = _CHANGES[change]
    now = first.astimezone(zone)
    for hours, expected in (
        (24, "predicted_0_24"),
        (25, "predicted_24_48"),
        (48, "predicted_24_48"),
        (49, "predicted_48_72"),
    ):
        slot = (first + timedelta(hours=hours)).astimezone(zone)
        assert get_source_tier("predicted", slot, now) == expected, (change, hours)


@ZONES
def test_next_24h_average_covers_24_real_hours(change):
    zone, first = _CHANGES[change]
    now = first.astimezone(zone)
    slots = _slots(zone, first, [10.0] * 48 + [1000.0] * 4)

    summary = summarize_prices(slots, now, window_hours=24)

    assert summary["average_price_next_window"] == 10.0


@ZONES
def test_hours_remaining_and_active_session_use_real_time(change):
    zone, first = _CHANGES[change]
    (session,) = slots_to_sessions(_slots(zone, first, [10.0] * 8))
    now = (first + timedelta(hours=1)).astimezone(zone)

    active, future = prune_and_classify([session], now)

    assert active is not None and future == []
    assert compute_hours_remaining(future, active, now) == pytest.approx(3.0)


def test_the_repeated_autumn_hour_yields_two_distinct_slots_per_half_hour():
    zone, first = _CHANGES["london_autumn"]
    slots = _slots(zone, first, [float(i) for i in range(10)])  # 22:00Z .. 02:30Z

    merged = deduplicate_and_sort_prices(slots, _now(first, zone))

    assert [s["raw_price"] for s in merged] == [float(i) for i in range(10)]
    wall_clocks = [s["date_time"].astimezone(LONDON).strftime("%H:%M") for s in merged]
    assert wall_clocks.count("01:00") == 2 and wall_clocks.count("01:30") == 2
