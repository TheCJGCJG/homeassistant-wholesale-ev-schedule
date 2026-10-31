"""normalize_to_half_hours: sources that are not half-hourly are put on the 30-minute grid the
scheduler works in.

Hourly day-ahead markets and 15-minute feeds are common outside Octopus Agile. Left alone, a
15-minute feed produces overlapping sessions and an hourly one prices only half of each hour, so a
regular coarse or fine feed is converted: hourly points cover two slots, finer points are averaged
into the half-hour they fall in. Anything irregular or already half-hourly is returned untouched --
guessing at the grid of a feed with gaps would invent prices."""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.wholesale_ev_schedule.price_parsing import normalize_to_half_hours

UTC = UTC
T0 = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)


def _entries(step_minutes, prices, start=T0):
    return [(start + timedelta(minutes=step_minutes * i), float(p), {"i": i}) for i, p in enumerate(prices)]


def _times(entries):
    return [t for t, _, _ in entries]


def _prices(entries):
    return [p for _, p, _ in entries]


def test_a_half_hourly_feed_is_returned_exactly_as_given():
    entries = _entries(30, [1, 2, 3, 4, 5])
    assert normalize_to_half_hours(entries) == entries


@pytest.mark.parametrize("count", [0, 1, 2, 3])
def test_feeds_too_short_to_judge_are_left_alone(count):
    entries = _entries(60, range(count))
    assert normalize_to_half_hours(entries) == entries


def test_an_hourly_feed_covers_two_half_hour_slots_per_point():
    out = normalize_to_half_hours(_entries(60, [10, 20, 30, 40]))

    assert _times(out) == [T0 + timedelta(minutes=30 * i) for i in range(8)]
    assert _prices(out) == [10, 10, 20, 20, 30, 30, 40, 40]


def test_each_expanded_slot_keeps_the_originating_point():
    out = normalize_to_half_hours(_entries(60, [10, 20, 30, 40]))
    assert [meta["i"] for _, _, meta in out] == [0, 0, 1, 1, 2, 2, 3, 3]


def test_a_fifteen_minute_feed_is_averaged_into_half_hours():
    out = normalize_to_half_hours(_entries(15, [10, 20, 30, 50, 5, 7, 9, 11]))

    assert _times(out) == [T0 + timedelta(minutes=30 * i) for i in range(4)]
    assert _prices(out) == [15, 40, 6, 10]


def test_a_five_minute_feed_is_averaged_into_half_hours():
    out = normalize_to_half_hours(_entries(5, [6] * 6 + [12] * 6 + [3] * 6 + [9] * 6))
    assert _prices(out) == [6, 12, 3, 9]


def test_a_fifteen_minute_feed_starting_mid_slot_still_buckets_by_the_half_hour():
    out = normalize_to_half_hours(_entries(15, [10, 20, 30, 40, 50], start=T0 + timedelta(minutes=15)))

    assert _times(out) == [T0, T0 + timedelta(minutes=30), T0 + timedelta(minutes=60)]
    assert _prices(out) == [10, 25, 45]  # a lone quarter stands in for its half hour


def test_a_partial_trailing_bucket_uses_what_there_is():
    out = normalize_to_half_hours(_entries(15, [10, 20, 30, 40, 50]))
    assert _prices(out) == [15, 35, 50]  # the last half hour has only its first quarter


def test_an_irregular_feed_is_not_guessed_at():
    gappy = _entries(60, [1, 2, 3, 4, 5, 6])
    del gappy[3]  # one missing hour in an otherwise hourly feed -> 4 of 5 gaps regular
    del gappy[1]
    assert normalize_to_half_hours(gappy) == gappy


@pytest.mark.parametrize("step", [45, 90, 120, 20, 7])
def test_unsupported_grids_are_left_alone(step):
    entries = _entries(step, range(8))
    assert normalize_to_half_hours(entries) == entries


def test_a_mostly_regular_hourly_feed_with_one_odd_gap_is_still_expanded():
    entries = _entries(60, range(10))
    entries[5:] = [(t + timedelta(minutes=60), p, m) for t, p, m in entries[5:]]  # one 120-minute gap in 9

    out = normalize_to_half_hours(entries)

    assert len(out) == 20


def test_a_few_stray_points_off_the_grid_do_not_stop_a_regular_feed_being_recognised():
    entries = _entries(60, range(12))
    assert len(normalize_to_half_hours(entries)) == 24


def test_unsorted_and_duplicated_input_is_handled():
    base = _entries(60, [10, 20, 30, 40])
    shuffled = [base[2], base[0], (base[1][0], 99.0, {"i": "dupe"}), base[1], base[3], base[0]]

    out = normalize_to_half_hours(shuffled)

    assert _times(out) == [T0 + timedelta(minutes=30 * i) for i in range(8)]
    assert _prices(out) == [10, 10, 99, 99, 30, 30, 40, 40]  # the first occurrence of a start wins


def test_negative_and_zero_prices_survive_expansion_and_averaging():
    assert _prices(normalize_to_half_hours(_entries(60, [-5, 0, 5, -1]))) == [-5, -5, 0, 0, 5, 5, -1, -1]
    assert _prices(normalize_to_half_hours(_entries(15, [-10, 10, 0, 4]))) == [0, 2]


def test_a_real_half_hour_point_inside_an_hourly_feed_wins_over_the_synthesised_one():
    entries = _entries(60, range(10, 20))
    entries.append((T0 + timedelta(minutes=90), 77.0, {"i": "explicit"}))  # a genuine 01:30 point

    out = {t: p for t, p, _ in normalize_to_half_hours(entries)}

    assert len(out) == 20  # no slot appears twice
    assert out[T0 + timedelta(minutes=90)] == 77.0
    assert out[T0 + timedelta(minutes=60)] == 11.0


def test_works_across_a_clock_change_because_it_uses_absolute_time():
    from zoneinfo import ZoneInfo

    london = ZoneInfo("Europe/London")
    start = datetime(2026, 10, 25, 0, 0, tzinfo=UTC)  # the clocks go back at 01:00Z
    entries = [((start + timedelta(minutes=60 * i)).astimezone(london), float(i), None) for i in range(6)]

    out = normalize_to_half_hours(entries)

    assert [t.astimezone(UTC) for t, _, _ in out] == [start + timedelta(minutes=30 * i) for i in range(12)]
