"""Price feeds in all the shapes real sources come in, through the real coordinator: other
granularities, timestamp styles, number styles, ordering, duplicates, overlap between the current
and next-day entities, and very large lists."""

import time
from datetime import UTC, datetime, timedelta, timezone

import pytest

from custom_components.wholesale_ev_schedule.const import CONF_DEFAULT_REQUIRED_HOURS, DOMAIN
from custom_components.wholesale_ev_schedule.scheduler import deduplicate_and_sort_prices, parse_dt

from .factories import (
    CURRENT_RATES_ENTITY,
    FULL_OPTIONS,
    NEXT_RATES_ENTITY,
    async_setup_wholesale_entry,
    set_octopus_rate_entity,
)

UTC = UTC
T0 = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
BASE = {**FULL_OPTIONS, CONF_DEFAULT_REQUIRED_HOURS: 0.0}


def _price(minutes_from_t0):
    """Dear 16:00-19:00, cheap otherwise -- a shape any granularity should agree on."""
    return 0.30 if 16 <= (T0 + timedelta(minutes=minutes_from_t0)).hour < 19 else 0.10


def _feed(step_minutes, count, price=_price):
    return [
        {"start": (T0 + timedelta(minutes=step_minutes * i)).isoformat(), "value_inc_vat": price(step_minutes * i)}
        for i in range(count)
    ]


async def _coordinator(hass, freezer, current, nxt=(), options=None):
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to(T0)
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, list(current))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, list(nxt))
    entry = await async_setup_wholesale_entry(hass, options=options or BASE)
    return hass.data[DOMAIN][entry.entry_id]


async def _plan(coordinator, hours=2.0, min_block=1.0, ready_in=20):
    await coordinator.async_set_min_block_hours(min_block)
    await coordinator.async_set_max_price(100.0)
    await coordinator.async_set_ready_by(T0 + timedelta(hours=ready_in))
    await coordinator.async_set_required_hours(hours)


def _blocks(coordinator):
    return [(parse_dt(s["start"]), parse_dt(s["end"])) for s in coordinator.data["sessions"]]


# ---------------------------------------------------------------------------
# Granularity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("step", [60, 30, 15, 10, 5])
@pytest.mark.parametrize("min_block", [0.5, 1.0, 2.0])
async def test_any_regular_granularity_gives_the_same_correct_plan(hass, freezer, step, min_block):
    c = await _coordinator(hass, freezer, _feed(step, 48 * 60 // step))
    await _plan(c, min_block=min_block)

    blocks = _blocks(c)
    assert c.data["state"] in ("scheduled", "charging"), c.data["error_reason"]
    assert sum((e - s).total_seconds() for s, e in blocks) == 2 * 3600
    for a, b in zip(blocks, blocks[1:], strict=False):
        assert a[1] <= b[0], f"overlapping sessions at {step}-minute granularity: {blocks}"
    assert all(s.minute % 30 == 0 for s, _ in blocks)  # on the half-hour grid
    assert max((e - s).total_seconds() for s, e in blocks) >= min(min_block, 2.0) * 3600
    assert blocks[0][0] == T0  # the cheap period starts straight away


async def test_an_hourly_feed_prices_both_halves_of_each_hour(hass, freezer):
    c = await _coordinator(hass, freezer, _feed(60, 48))

    slots = {p["date_time"]: p["raw_price"] for p in deduplicate_and_sort_prices(c._collect_prices(), T0)}

    assert slots[T0 + timedelta(hours=5)] == slots[T0 + timedelta(hours=5, minutes=30)] == 30.0  # 17:00-18:00
    assert slots[T0 + timedelta(hours=2)] == slots[T0 + timedelta(hours=2, minutes=30)] == 10.0


async def test_a_fifteen_minute_feed_is_averaged_not_truncated(hass, freezer):
    pts = [
        {"start": (T0 + timedelta(minutes=15 * i)).isoformat(), "value_inc_vat": [0.10, 0.20, 0.30, 0.50][i % 4]}
        for i in range(64)
    ]
    c = await _coordinator(hass, freezer, pts)

    slots = {p["date_time"]: p["raw_price"] for p in deduplicate_and_sort_prices(c._collect_prices(), T0)}

    assert slots[T0] == 15.0 and slots[T0 + timedelta(minutes=30)] == 40.0


async def test_an_hourly_feed_with_holes_keeps_its_holes(hass, freezer):
    """Expansion is local to each point (a point covers its own hour), so missing hours stay
    missing -- nothing is invented across a gap."""
    holes = {5, 10, 11, 12, 23}
    full = _feed(60, 40)
    c = await _coordinator(hass, freezer, [p for i, p in enumerate(full) if i not in holes])

    times = {p["date_time"] for p in c._collect_prices()}

    assert len(times) == 2 * (40 - len(holes))
    for hour in holes:
        assert T0 + timedelta(hours=hour) not in times
        assert T0 + timedelta(hours=hour, minutes=30) not in times
    for hour in set(range(40)) - holes:
        assert {T0 + timedelta(hours=hour), T0 + timedelta(hours=hour, minutes=30)} <= times


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "render",
    [
        lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ"),
        lambda t: t.isoformat(),
        lambda t: t.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        lambda t: t.strftime("%Y-%m-%dT%H:%M:%S.123456+00:00"),
        lambda t: t.astimezone(timezone(timedelta(hours=1))).isoformat(),
        lambda t: t.astimezone(timezone(timedelta(hours=-5, minutes=-30))).isoformat(),
        lambda t: t.replace(tzinfo=None).isoformat(),  # naive: read as Home Assistant's local time (UTC here)
        lambda t: t,  # an actual datetime object, as some integrations expose
    ],
    ids=["z", "offset0", "z_millis", "micros", "plus1", "minus530", "naive", "datetime_object"],
)
async def test_every_common_timestamp_style_parses_to_the_same_slots(hass, freezer, render):
    pts = [{"start": render(T0 + timedelta(minutes=30 * i)), "value_inc_vat": _price(30 * i)} for i in range(48)]
    c = await _coordinator(hass, freezer, pts)

    slots = c._collect_prices()

    assert sorted(p["date_time"] for p in slots) == [T0 + timedelta(minutes=30 * i) for i in range(48)]


@pytest.mark.parametrize("bad", [None, "", "not a date", "2026-13-45T99:00:00Z", 1772452800, 1.77e9, [], {}, True])
async def test_unusable_timestamps_skip_just_that_point(hass, freezer, bad):
    pts = _feed(30, 10)
    pts[4] = {"start": bad, "value_inc_vat": 0.2}
    c = await _coordinator(hass, freezer, pts)

    assert len(c._collect_prices()) == 9


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (0.25, 25.0),
        (1, 100.0),
        ("0.25", 25.0),
        (" 0.25 ", 25.0),
        ("2.5e-1", 25.0),
        (0, 0.0),
        (-0.05, -5.0),
        ("-0.05", -5.0),
        (0.125, 12.5),
    ],
)
async def test_every_numeric_style_is_read_correctly(hass, freezer, value, expected):
    pts = _feed(30, 6)
    pts[2] = {"start": pts[2]["start"], "value_inc_vat": value}
    c = await _coordinator(hass, freezer, pts)

    prices = {p["date_time"]: p["raw_price"] for p in c._collect_prices()}

    assert prices[T0 + timedelta(minutes=60)] == pytest.approx(expected)


@pytest.mark.parametrize(
    "bad", [None, "", "N/A", "unknown", "nan", "inf", "-inf", float("nan"), float("inf"), True, False, [], {}, "1,5"]
)
async def test_unusable_prices_skip_just_that_point(hass, freezer, bad):
    pts = _feed(30, 10)
    pts[4] = {"start": pts[4]["start"], "value_inc_vat": bad}
    c = await _coordinator(hass, freezer, pts)

    assert len(c._collect_prices()) == 9


async def test_extra_unknown_keys_on_points_are_ignored(hass, freezer):
    pts = [{**p, "end": "x", "tariff": "AGILE", "meta": {"a": [1, 2, 3]}, "extra": None} for p in _feed(30, 10)]
    c = await _coordinator(hass, freezer, pts)
    assert len(c._collect_prices()) == 10


# ---------------------------------------------------------------------------
# Order, duplicates, overlap
# ---------------------------------------------------------------------------


async def test_an_unsorted_feed_schedules_the_same_as_a_sorted_one(hass, freezer):
    feed = _feed(30, 48)
    c = await _coordinator(hass, freezer, feed)
    await _plan(c, min_block=0.5)
    expected = _blocks(c)

    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, list(reversed(feed)))
    await c.async_refresh()

    assert _blocks(c) == expected


async def test_a_duplicated_slot_inside_one_feed_is_counted_once_and_the_first_wins(hass, freezer):
    feed = _feed(30, 12)
    dupe = {"start": feed[3]["start"], "value_inc_vat": 9.99}
    c = await _coordinator(hass, freezer, feed[:4] + [dupe] + feed[4:])

    merged = {p["date_time"]: p["raw_price"] for p in deduplicate_and_sort_prices(c._collect_prices(), T0)}

    assert len(merged) == 12 and merged[T0 + timedelta(minutes=90)] == 10.0


async def test_current_and_next_day_overlapping_is_harmless(hass, freezer):
    feed = _feed(30, 48)
    c = await _coordinator(hass, freezer, feed[:30], feed[20:])  # 10 slots in both entities
    await _plan(c, min_block=0.5)

    merged = deduplicate_and_sort_prices(c._collect_prices(), T0)
    assert len(merged) == 48 and c.data["state"] in ("scheduled", "charging")


async def test_slots_in_the_past_are_ignored_but_the_one_in_progress_counts(hass, freezer):
    feed = [
        {"start": (T0 - timedelta(minutes=30 * (4 - i))).isoformat(), "value_inc_vat": 0.5 if i < 4 else 0.1}
        for i in range(20)
    ]
    c = await _coordinator(hass, freezer, feed)

    merged = deduplicate_and_sort_prices(c._collect_prices(), T0)

    assert merged[0]["date_time"] == T0 and len(merged) == 16


# ---------------------------------------------------------------------------
# Size
# ---------------------------------------------------------------------------


async def test_a_huge_feed_is_handled_quickly(hass, freezer):
    c = await _coordinator(hass, freezer, _feed(30, 48 * 60))  # 60 days of half-hours
    started = time.perf_counter()
    await _plan(c, min_block=2.0, ready_in=24 * 7)
    elapsed = time.perf_counter() - started

    assert c.data["state"] in ("scheduled", "charging")
    assert elapsed < 5.0, f"{elapsed:.1f}s to plan against 60 days of prices"


async def test_a_feed_with_thousands_of_malformed_rows_still_yields_the_good_ones(hass, freezer):
    feed = _feed(30, 100)
    junk = [{"start": "junk", "value_inc_vat": i} for i in range(5000)] + [None] * 2000 + ["text"] * 1000
    c = await _coordinator(hass, freezer, junk[:3000] + feed + junk[3000:])

    assert len(c._collect_prices()) == 100
