"""Coordinator-level behaviour of the short-range forecast (issue #54), against a real
config-entry setup: parsing, precedence between sources, what ends up scheduled, and
-- just as important -- that an entry which never opted in is byte-for-byte unaffected.

Scenario shorthand: slot `i` starts at START + 30*i minutes. Actual (Octopus) rates are
in GBP/kWh like the real event entity (x100 -> p/kWh); forecast and short-range
prices are already p/kWh.
"""

import math
from datetime import UTC, datetime, timedelta

import homeassistant.util.dt as dt_util
import pytest

from custom_components.wholesale_ev_schedule.const import (
    CONF_DEFAULT_REQUIRED_HOURS,
    CONF_SHORT_RANGE_ATTRIBUTE,
    CONF_SHORT_RANGE_CONFIDENCE,
    CONF_SHORT_RANGE_DATETIME_KEY,
    CONF_SHORT_RANGE_ENTITY,
    CONF_SHORT_RANGE_ESTIMATE_FLAG_KEY,
    CONF_SHORT_RANGE_PRICE_KEY,
    CONF_SHORT_RANGE_PROVIDER,
    CONF_SHORT_RANGE_UNIT_MULTIPLIER,
    DOMAIN,
)
from custom_components.wholesale_ev_schedule.providers import (
    SHORT_RANGE_PROVIDER_AGILE_PREDICT,
    SHORT_RANGE_PROVIDER_CUSTOM,
    SHORT_RANGE_PROVIDER_NONE,
)
from custom_components.wholesale_ev_schedule.scheduler import (
    SOURCE_CURRENT_ACTUAL,
    SOURCE_PREDICTED,
    SOURCE_SHORT_RANGE,
    SOURCE_SHORT_RANGE_FINAL,
    deduplicate_and_sort_prices,
    parse_dt,
)

from .factories import (
    CURRENT_RATES_ENTITY,
    EXPECTED_ENTITY_IDS,
    FORECAST_ENTITY,
    FULL_OPTIONS,
    NEXT_RATES_ENTITY,
    OPPORTUNISTIC_OPTIONS,
    SHORT_RANGE_ENTITY,
    agile_predict_points,
    agile_rates_points,
    async_setup_wholesale_entry,
    priced_points,
    set_octopus_rate_entity,
    set_short_range_entity,
    short_range_options,
)

START = datetime(2026, 3, 2, 20, 0, tzinfo=UTC)
# Nothing may auto-schedule (and lock in as "active") before a test configures it.
_ZERO_HOURS = {CONF_DEFAULT_REQUIRED_HOURS: 0.0}
_BASE = {**FULL_OPTIONS, **_ZERO_HOURS}


def _at(i: int) -> datetime:
    return START + timedelta(minutes=30 * i)


async def _coordinator(hass, freezer, *, options=None, actual=None, forecast=None, short_range=None, prediction=True):
    """Set up an entry. actual=(first_slot, [GBP/kWh]); forecast=(first_slot, [p/kWh]);
    short_range=(first_slot, [p/kWh]) with `prediction` as agile_rates_points takes it."""
    freezer.move_to(START)
    actual_points = priced_points(_at(actual[0]), actual[1]) if actual else []
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, actual_points)
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])
    if forecast:
        hass.states.async_set(
            FORECAST_ENTITY, "populated", {"prices": agile_predict_points(_at(forecast[0]), forecast[1])}
        )
    if short_range:
        set_short_range_entity(hass, agile_rates_points(_at(short_range[0]), short_range[1], prediction))
    entry = await async_setup_wholesale_entry(
        hass, options=options if options is not None else short_range_options(_BASE)
    )
    return hass.data[DOMAIN][entry.entry_id]


async def _schedule(coordinator, required_hours, *, ready_in_hours=14, min_block=0.5, max_price=100.0, gamble=None):
    await coordinator.async_set_min_block_hours(min_block)
    await coordinator.async_set_max_price(max_price)
    if gamble is not None:
        await coordinator.async_set_gamble_tolerance(gamble)
    await coordinator.async_set_ready_by(START + timedelta(hours=ready_in_hours))
    await coordinator.async_set_required_hours(required_hours)


def _sources(coordinator):
    return {s["source"] for s in coordinator._collect_prices()}


def _best_by_slot(coordinator):
    return {s["date_time"]: s for s in deduplicate_and_sort_prices(coordinator._collect_prices(), dt_util.now())}


def _starts(coordinator):
    return [parse_dt(s["start"]) for s in coordinator.data["sessions"]]


# ---------------------------------------------------------------------------
# Not opted in: nothing may change
# ---------------------------------------------------------------------------


async def test_entry_without_any_short_range_keys_ignores_a_populated_short_range_entity(hass, freezer):
    """The golden rule: every entry created before this feature has none of these
    keys. Even with an Agile Rates entity sitting in HA, it must never be read."""
    coordinator = await _coordinator(
        hass,
        freezer,
        options={**FULL_OPTIONS, **_ZERO_HOURS},
        actual=(0, [0.3] * 4),
        forecast=(4, [50.0] * 4),
        short_range=(0, [1.0] * 8),
    )

    assert _sources(coordinator) == {SOURCE_CURRENT_ACTUAL, SOURCE_PREDICTED}
    assert coordinator.short_range_provider_label is None
    assert all(s["raw_price"] != 1.0 for s in coordinator._collect_prices())


async def test_provider_none_with_a_stale_entity_left_behind_is_off(hass, freezer):
    """Switching back to None in the options flow leaves the old keys in place."""
    options = short_range_options(_BASE, **{CONF_SHORT_RANGE_PROVIDER: SHORT_RANGE_PROVIDER_NONE})
    coordinator = await _coordinator(
        hass, freezer, options=options, forecast=(0, [50.0] * 4), short_range=(0, [1.0] * 4)
    )

    assert _sources(coordinator) == {SOURCE_PREDICTED}
    assert coordinator.short_range_provider_label is None


@pytest.mark.parametrize("entity", [None, ""])
async def test_provider_set_but_no_entity_is_off(hass, freezer, entity):
    options = short_range_options(_BASE, **{CONF_SHORT_RANGE_ENTITY: entity})
    coordinator = await _coordinator(
        hass, freezer, options=options, forecast=(0, [50.0] * 4), short_range=(0, [1.0] * 4)
    )

    assert _sources(coordinator) == {SOURCE_PREDICTED}
    assert coordinator.short_range_provider_label is None


async def test_configured_entity_that_does_not_exist_yet_is_simply_empty(hass, freezer):
    """HA starts integrations before every REST sensor has its first poll."""
    coordinator = await _coordinator(hass, freezer, forecast=(0, [50.0] * 4))

    assert hass.states.get(SHORT_RANGE_ENTITY) is None
    assert _sources(coordinator) == {SOURCE_PREDICTED}


async def test_enabling_short_range_adds_no_entities(hass, freezer):
    await _coordinator(hass, freezer, short_range=(0, [20.0] * 4))

    registered = {
        s.entity_id for s in hass.states.async_all() if s.entity_id.split(".")[1].startswith("wholesale_ev_schedule_")
    }
    assert registered == EXPECTED_ENTITY_IDS


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


async def test_agile_rates_api_estimates_and_finals_are_tagged_and_priced(hass, freezer):
    coordinator = await _coordinator(
        hass, freezer, short_range=(0, [20.0, 21.0, 22.0, 23.0]), prediction=[True, False, True, False]
    )

    slots = {s["date_time"]: s for s in coordinator._collect_prices()}

    assert [(slots[_at(i)]["source"], slots[_at(i)]["raw_price"]) for i in range(4)] == [
        (SOURCE_SHORT_RANGE, 20.0),
        (SOURCE_SHORT_RANGE_FINAL, 21.0),
        (SOURCE_SHORT_RANGE, 22.0),
        (SOURCE_SHORT_RANGE_FINAL, 23.0),
    ]
    assert slots[_at(0)]["base_credibility"] == 0.95
    assert "base_credibility" not in slots[_at(1)]


async def test_utc_z_timestamps_become_timezone_aware_local_slots(hass, freezer):
    coordinator = await _coordinator(hass, freezer, short_range=(0, [20.0]))

    (slot,) = coordinator._collect_prices()

    assert slot["date_time"].tzinfo is not None
    assert slot["date_time"] == START


async def test_missing_flag_on_a_point_is_treated_as_an_estimate(hass, freezer):
    coordinator = await _coordinator(hass, freezer, short_range=None)
    points = agile_rates_points(START, [20.0, 21.0], prediction=False)
    del points[1]["agileRate"]["result"]["prediction"]
    set_short_range_entity(hass, points)

    sources = {s["date_time"]: s["source"] for s in coordinator._collect_prices()}

    assert sources == {_at(0): SOURCE_SHORT_RANGE_FINAL, _at(1): SOURCE_SHORT_RANGE}


@pytest.mark.parametrize(
    "flag,expected",
    [
        (False, SOURCE_SHORT_RANGE_FINAL),
        (True, SOURCE_SHORT_RANGE),
        ("false", SOURCE_SHORT_RANGE_FINAL),
        ("False", SOURCE_SHORT_RANGE_FINAL),
        ("true", SOURCE_SHORT_RANGE),
        (0, SOURCE_SHORT_RANGE_FINAL),
        (1, SOURCE_SHORT_RANGE),
        (None, SOURCE_SHORT_RANGE),
        ("garbage", SOURCE_SHORT_RANGE),
        ([], SOURCE_SHORT_RANGE),
    ],
)
async def test_estimate_flag_values_are_interpreted_conservatively(hass, freezer, flag, expected):
    coordinator = await _coordinator(hass, freezer)
    points = agile_rates_points(START, [20.0])
    points[0]["agileRate"]["result"]["prediction"] = flag
    set_short_range_entity(hass, points)

    assert [s["source"] for s in coordinator._collect_prices()] == [expected]


async def test_malformed_points_are_skipped_without_losing_the_good_ones(hass, freezer):
    coordinator = await _coordinator(hass, freezer)
    good_a = agile_rates_points(_at(0), [20.0])[0]
    good_b = agile_rates_points(_at(1), [21.0])[0]
    good_b["agileRate"]["result"]["rate"] = "21"  # numeric strings are accepted
    bad_points = [
        "a string row",
        None,
        5,
        [],
        {},
        {"deliveryStart": _at(2).isoformat()},  # no price anywhere
        {"agileRate": {"result": {"rate": 5.0}}},  # no time
        {"deliveryStart": "not a date", "agileRate": {"result": {"rate": 5.0}}},
        {"deliveryStart": _at(3).isoformat(), "agileRate": {"result": {"rate": "N/A"}}},
        {"deliveryStart": _at(4).isoformat(), "agileRate": {"result": {"rate": "nan"}}},
        {"deliveryStart": _at(5).isoformat(), "agileRate": {"result": {"rate": float("inf")}}},
        {"deliveryStart": _at(6).isoformat(), "agileRate": {"result": {"rate": None}}},
        {"deliveryStart": _at(7).isoformat(), "agileRate": "not a mapping"},
        {"deliveryStart": _at(8).isoformat(), "agileRate": {"result": ["list"]}},
    ]
    set_short_range_entity(hass, bad_points[:7] + [good_a] + bad_points[7:] + [good_b])

    prices = {s["date_time"]: s["raw_price"] for s in coordinator._collect_prices()}

    assert prices == {_at(0): 20.0, _at(1): 21.0}


@pytest.mark.parametrize(
    "attributes", [{}, {"rates": None}, {"rates": []}, {"rates": "text"}, {"rates": {"not": "a list"}}, {"other": 1}]
)
async def test_missing_or_oddly_shaped_attribute_yields_no_slots_and_no_crash(hass, freezer, attributes):
    coordinator = await _coordinator(hass, freezer, forecast=(0, [50.0] * 2))
    hass.states.async_set(SHORT_RANGE_ENTITY, "ok", attributes)

    assert _sources(coordinator) == {SOURCE_PREDICTED}
    await coordinator.async_refresh()
    assert coordinator.last_update_success


@pytest.mark.parametrize("state", ["unavailable", "unknown", ""])
async def test_unusable_entity_states_are_ignored(hass, freezer, state):
    coordinator = await _coordinator(hass, freezer, forecast=(0, [50.0] * 2))
    hass.states.async_set(SHORT_RANGE_ENTITY, state, {"rates": agile_rates_points(START, [1.0, 1.0])})

    assert _sources(coordinator) == {SOURCE_PREDICTED}


async def test_custom_provider_with_flat_keys_and_no_flag_treats_everything_as_an_estimate(hass, freezer):
    options = short_range_options(
        _ZERO_HOURS,
        **{
            CONF_SHORT_RANGE_PROVIDER: SHORT_RANGE_PROVIDER_CUSTOM,
            CONF_SHORT_RANGE_ATTRIBUTE: "forecast",
            CONF_SHORT_RANGE_DATETIME_KEY: "from",
            CONF_SHORT_RANGE_PRICE_KEY: "gbp",
            CONF_SHORT_RANGE_UNIT_MULTIPLIER: 100.0,
            CONF_SHORT_RANGE_ESTIMATE_FLAG_KEY: None,
        },
    )
    coordinator = await _coordinator(hass, freezer, options=options)
    hass.states.async_set(
        SHORT_RANGE_ENTITY, "ok", {"forecast": [{"from": START.isoformat(), "gbp": 0.2, "final": True}]}
    )

    (slot,) = coordinator._collect_prices()

    assert (slot["source"], slot["raw_price"]) == (SOURCE_SHORT_RANGE, 20.0)


@pytest.mark.parametrize("flag_key", ["", "   ", None, 123, ["x"]])
async def test_blank_or_nonsense_flag_key_in_stored_options_means_no_flag(hass, freezer, flag_key):
    options = short_range_options(_BASE, **{CONF_SHORT_RANGE_ESTIMATE_FLAG_KEY: flag_key})
    coordinator = await _coordinator(hass, freezer, options=options, short_range=(0, [20.0, 21.0]), prediction=False)

    assert {s["source"] for s in coordinator._collect_prices()} == {SOURCE_SHORT_RANGE}


async def test_custom_flat_key_containing_a_dot_still_resolves_literally(hass, freezer):
    options = short_range_options(
        _ZERO_HOURS,
        **{
            CONF_SHORT_RANGE_PROVIDER: SHORT_RANGE_PROVIDER_CUSTOM,
            CONF_SHORT_RANGE_ATTRIBUTE: "prices",
            CONF_SHORT_RANGE_DATETIME_KEY: "date.time",
            CONF_SHORT_RANGE_PRICE_KEY: "price.pence",
            CONF_SHORT_RANGE_ESTIMATE_FLAG_KEY: None,
        },
    )
    coordinator = await _coordinator(hass, freezer, options=options)
    hass.states.async_set(
        SHORT_RANGE_ENTITY,
        "ok",
        {"prices": [{"date.time": START.isoformat(), "price.pence": 17.5, "price": {"pence": 99.0}}]},
    )

    assert [s["raw_price"] for s in coordinator._collect_prices()] == [17.5]


async def test_agile_predict_shaped_short_range_source_works_with_no_flag(hass, freezer):
    from custom_components.wholesale_ev_schedule.providers import SHORT_RANGE_PROVIDERS

    profile = SHORT_RANGE_PROVIDERS[SHORT_RANGE_PROVIDER_AGILE_PREDICT]
    options = short_range_options(
        _ZERO_HOURS,
        **{
            CONF_SHORT_RANGE_PROVIDER: SHORT_RANGE_PROVIDER_AGILE_PREDICT,
            CONF_SHORT_RANGE_ATTRIBUTE: profile["attribute"],
            CONF_SHORT_RANGE_DATETIME_KEY: profile["datetime_key"],
            CONF_SHORT_RANGE_PRICE_KEY: profile["price_key"],
            CONF_SHORT_RANGE_UNIT_MULTIPLIER: profile["unit_multiplier"],
            CONF_SHORT_RANGE_ESTIMATE_FLAG_KEY: None,
        },
    )
    coordinator = await _coordinator(hass, freezer, options=options)
    hass.states.async_set(SHORT_RANGE_ENTITY, "ok", {"prices": agile_predict_points(START, [30.0, 31.0])})

    assert {s["source"] for s in coordinator._collect_prices()} == {SOURCE_SHORT_RANGE}


@pytest.mark.parametrize(
    "stored,expected",
    [
        (95.0, 0.95),
        (80, 0.8),
        ("70", 0.7),
        (100, 1.0),
        (0, 0.0),
        (150, 1.5),  # out-of-range values pass through; the scheduler clamps them
        (-10, -0.1),
        ("abc", 0.95),
        (None, 0.95),
        ([], 0.95),
        (float("nan"), 0.95),
        (float("inf"), 0.95),
    ],
)
async def test_confidence_option_is_converted_to_a_base_credibility(hass, freezer, stored, expected):
    options = short_range_options(_BASE, **{CONF_SHORT_RANGE_CONFIDENCE: stored})
    coordinator = await _coordinator(hass, freezer, options=options, short_range=(0, [20.0]))

    assert coordinator._short_range_base_credibility() == pytest.approx(expected)
    (slot,) = coordinator._collect_prices()
    assert slot["base_credibility"] == pytest.approx(expected)


async def test_missing_confidence_option_uses_the_default(hass, freezer):
    options = short_range_options(_BASE)
    del options[CONF_SHORT_RANGE_CONFIDENCE]
    coordinator = await _coordinator(hass, freezer, options=options, short_range=(0, [20.0]))

    assert coordinator._short_range_base_credibility() == pytest.approx(0.95)


# ---------------------------------------------------------------------------
# DST: the source reports UTC half-hours; local days can be 23 or 25 hours long
# ---------------------------------------------------------------------------

_AUTUMN = datetime(2026, 10, 24, 23, 0, tzinfo=UTC)  # BST ends: a 25h local day, 50 slots
_SPRING = datetime(2026, 3, 29, 0, 0, tzinfo=UTC)  # GMT -> BST: a 23h local day, 46 slots


async def _london_coordinator(hass, freezer, first_utc, half_hours):
    await hass.config.async_set_time_zone("Europe/London")
    freezer.move_to(first_utc - timedelta(hours=1))
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, [])
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])
    set_short_range_entity(hass, agile_rates_points(first_utc, [float(i) for i in range(half_hours)]))
    entry = await async_setup_wholesale_entry(hass, options=short_range_options(_BASE))
    return hass.data[DOMAIN][entry.entry_id]


def _utc_gaps(slots):
    """Gaps between consecutive slots measured in UTC. (Subtracting two aware datetimes
    that share a tzinfo compares wall-clock time, which lies across a DST change.)"""
    utc = [s["date_time"].astimezone(UTC) for s in slots]
    return {b - a for a, b in zip(utc, utc[1:], strict=False)}


@pytest.mark.parametrize("first_utc,half_hours", [(_AUTUMN, 50), (_SPRING, 46)], ids=["autumn_50", "spring_46"])
async def test_dst_days_parse_every_half_hour_as_a_distinct_instant(hass, freezer, first_utc, half_hours):
    coordinator = await _london_coordinator(hass, freezer, first_utc, half_hours)

    slots = coordinator._collect_prices()

    assert len(slots) == half_hours
    assert len({s["date_time"].astimezone(UTC) for s in slots}) == half_hours
    assert _utc_gaps(slots) == {timedelta(minutes=30)}


async def test_the_23_hour_spring_day_merges_without_losing_a_slot(hass, freezer):
    coordinator = await _london_coordinator(hass, freezer, _SPRING, 46)

    merged = deduplicate_and_sort_prices(coordinator._collect_prices(), dt_util.now())

    assert [s["raw_price"] for s in merged] == [float(i) for i in range(46)]
    assert _utc_gaps(merged) == {timedelta(minutes=30)}


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Pre-existing, provider-independent: deduplicate_and_sort_prices keys a dict on local-tz-aware "
        "datetimes, which compare by wall clock when they share a tzinfo, so the repeated 01:00-02:00 hour "
        "on the 25-hour autumn day collapses (50 slots -> 48). Remove this marker when that is fixed."
    ),
)
async def test_the_25_hour_autumn_day_merges_without_losing_a_slot(hass, freezer):
    coordinator = await _london_coordinator(hass, freezer, _AUTUMN, 50)

    merged = deduplicate_and_sort_prices(coordinator._collect_prices(), dt_util.now())

    assert len(merged) == 50


# ---------------------------------------------------------------------------
# Precedence against the real feeds
# ---------------------------------------------------------------------------


async def test_actual_rates_override_short_range_prices_for_the_same_slot(hass, freezer):
    coordinator = await _coordinator(
        hass, freezer, actual=(2, [0.5] * 2), short_range=(0, [20.0] * 6), prediction=False
    )

    best = _best_by_slot(coordinator)

    assert [best[_at(i)]["source"] for i in range(6)] == [
        SOURCE_SHORT_RANGE_FINAL,
        SOURCE_SHORT_RANGE_FINAL,
        SOURCE_CURRENT_ACTUAL,
        SOURCE_CURRENT_ACTUAL,
        SOURCE_SHORT_RANGE_FINAL,
        SOURCE_SHORT_RANGE_FINAL,
    ]
    assert best[_at(2)]["raw_price"] == 50.0


async def test_short_range_overrides_the_forecast_and_the_forecast_fills_the_gaps(hass, freezer):
    coordinator = await _coordinator(hass, freezer, forecast=(0, [50.0] * 8), short_range=(2, [20.0] * 3))

    best = _best_by_slot(coordinator)

    assert [(best[_at(i)]["source"], best[_at(i)]["raw_price"]) for i in range(8)] == (
        [(SOURCE_PREDICTED, 50.0)] * 2 + [(SOURCE_SHORT_RANGE, 20.0)] * 3 + [(SOURCE_PREDICTED, 50.0)] * 3
    )


async def test_a_short_range_estimate_overrides_the_forecast_even_when_dearer(hass, freezer):
    """Precedence, not 'cheapest wins': the point is to trust the better source."""
    coordinator = await _coordinator(hass, freezer, forecast=(0, [20.0] * 2), short_range=(0, [60.0] * 2))

    assert {s["raw_price"] for s in _best_by_slot(coordinator).values()} == {60.0}


# ---------------------------------------------------------------------------
# What gets scheduled
# ---------------------------------------------------------------------------


def _afternoon_scenario():
    """33p actual rates for slots 4-11, a 50p forecast across slots 0-27, and a day-ahead
    estimate at 20p for slots 14-17 -- i.e. 'tomorrow looks cheaper than anything I have
    real rates for', the afternoon charge-now-or-wait decision."""
    return {
        "actual": (4, [0.33] * 8),
        "forecast": (0, [50.0] * 28),
        "short_range": (14, [20.0] * 4),
    }


async def test_a_cheaper_short_range_window_is_scheduled_and_flagged_estimated(hass, freezer):
    coordinator = await _coordinator(hass, freezer, **_afternoon_scenario())
    await _schedule(coordinator, required_hours=2.0)

    assert _starts(coordinator) == [_at(14)]
    (session,) = coordinator.data["sessions"]
    assert session["avg_price"] == 20.0
    assert session["estimated"] is True
    assert session["confidence"] == 95.0


async def test_the_same_window_firms_up_when_the_source_marks_it_final(hass, freezer):
    """10am -> 4pm: identical prices, but `prediction` flips false. The schedule must
    notice the change (it is not a price change, so a naive cache would miss it)."""
    coordinator = await _coordinator(hass, freezer, **_afternoon_scenario())
    await _schedule(coordinator, required_hours=2.0)
    assert coordinator.data["sessions"][0]["estimated"] is True

    set_short_range_entity(hass, agile_rates_points(_at(14), [20.0] * 4, prediction=False))
    await coordinator.async_refresh()

    (session,) = coordinator.data["sessions"]
    assert _starts(coordinator) == [_at(14)]
    assert session["estimated"] is False
    assert session["confidence"] == 100.0


async def test_final_prices_that_change_reschedule_immediately(hass, freezer):
    coordinator = await _coordinator(hass, freezer, **_afternoon_scenario())
    await _schedule(coordinator, required_hours=2.0)

    # The real auction lands dearer than the estimate, so the 33p actual window now wins.
    set_short_range_entity(hass, agile_rates_points(_at(14), [45.0] * 4, prediction=False))
    await coordinator.async_refresh()

    assert _starts(coordinator) == [_at(4)]
    assert coordinator.data["sessions"][0]["estimated"] is False


async def test_without_short_range_the_same_scenario_picks_the_actual_window(hass, freezer):
    """Control for the test above: this is what the user got before the feature."""
    coordinator = await _coordinator(hass, freezer, options={**FULL_OPTIONS, **_ZERO_HOURS}, **_afternoon_scenario())
    await _schedule(coordinator, required_hours=2.0)

    assert _starts(coordinator) == [_at(4)]
    assert coordinator.data["sessions"][0]["estimated"] is False


async def test_short_range_coverage_gap_falls_back_to_the_forecast(hass, freezer):
    """The short-range source covers 4 slots; the 2h requirement needs 4, but only
    after the estimate is exhausted does the forecast matter."""
    coordinator = await _coordinator(hass, freezer, forecast=(0, [40.0] * 20), short_range=(6, [20.0] * 2))
    await _schedule(coordinator, required_hours=2.0)

    sessions = coordinator.data["sessions"]
    chosen = {
        parse_dt(s["start"]) + timedelta(minutes=30 * k) for s in sessions for k in range(int(s["duration_hours"] * 2))
    }
    assert {_at(6), _at(7)} <= chosen
    assert len(chosen) == 4


@pytest.mark.parametrize("confidence,expected_start", [(95.0, 14), (40.0, 4)])
async def test_confidence_decides_whether_a_cheaper_estimate_beats_a_dearer_real_window(
    hass, freezer, confidence, expected_start
):
    """30p estimate vs 33p real rates at gamble tolerance 50: trusted at 95% the estimate
    wins; at 40% it is discounted to ~43p and the real rates win."""
    options = short_range_options(_BASE, **{CONF_SHORT_RANGE_CONFIDENCE: confidence})
    coordinator = await _coordinator(
        hass, freezer, options=options, actual=(4, [0.33] * 8), short_range=(14, [30.0] * 4)
    )
    await _schedule(coordinator, required_hours=2.0, gamble=50.0)

    assert _starts(coordinator) == [_at(expected_start)]


async def test_gamble_tolerance_zero_means_actual_only_so_estimates_are_excluded(hass, freezer):
    coordinator = await _coordinator(hass, freezer, **_afternoon_scenario())
    await _schedule(coordinator, required_hours=2.0, gamble=0.0)

    assert _starts(coordinator) == [_at(4)]


async def test_gamble_tolerance_zero_still_trusts_prices_the_source_marks_final(hass, freezer):
    coordinator = await _coordinator(hass, freezer, prediction=False, **_afternoon_scenario())
    await _schedule(coordinator, required_hours=2.0, gamble=0.0)

    assert _starts(coordinator) == [_at(14)]
    assert coordinator.data["sessions"][0]["estimated"] is False


async def test_max_price_still_applies_to_short_range_prices(hass, freezer):
    coordinator = await _coordinator(hass, freezer, actual=(4, [0.33] * 8), short_range=(14, [45.0] * 4))
    await _schedule(coordinator, required_hours=2.0, max_price=40.0)

    # Only the 33p actual window is under the 40p cap.
    assert _starts(coordinator) == [_at(4)]


async def test_short_range_entity_going_unavailable_falls_back_and_recovers(hass, freezer):
    coordinator = await _coordinator(hass, freezer, **_afternoon_scenario())
    await _schedule(coordinator, required_hours=2.0)
    assert _starts(coordinator) == [_at(14)]

    hass.states.async_set(SHORT_RANGE_ENTITY, "unavailable", {})
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert _starts(coordinator) == [_at(4)]

    set_short_range_entity(hass, agile_rates_points(_at(14), [20.0] * 4))
    await coordinator.async_refresh()
    assert _starts(coordinator) == [_at(14)]


async def test_short_range_attribute_emptying_out_falls_back_to_the_next_source(hass, freezer):
    coordinator = await _coordinator(hass, freezer, **_afternoon_scenario())
    await _schedule(coordinator, required_hours=2.0)

    hass.states.async_set(SHORT_RANGE_ENTITY, "ok", {"rates": []})
    await coordinator.async_refresh()

    assert _starts(coordinator) == [_at(4)]


async def test_legacy_stored_session_without_an_estimated_key_does_not_crash(hass, freezer):
    """An active session persisted by an earlier version has no `estimated` field and
    is carried over as-is until it ends."""
    coordinator = await _coordinator(hass, freezer, actual=(0, [0.2] * 6))
    await _schedule(coordinator, required_hours=2.0, ready_in_hours=6)
    legacy = {
        "start": (START - timedelta(minutes=30)).isoformat(),
        "end": (START + timedelta(minutes=30)).isoformat(),
        "duration_hours": 1.0,
        "avg_price": 20.0,
        "confidence": 100.0,
    }
    coordinator._stored_sessions = [legacy]

    await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert coordinator.data["state"] == "charging"
    assert legacy in coordinator.data["sessions"]
    assert "estimated" not in legacy


# ---------------------------------------------------------------------------
# Opportunistic tier shares the same prices
# ---------------------------------------------------------------------------


async def test_opportunistic_sessions_see_short_range_prices_and_are_flagged_too(hass, freezer):
    options = short_range_options(OPPORTUNISTIC_OPTIONS, **{CONF_DEFAULT_REQUIRED_HOURS: 0.0})
    coordinator = await _coordinator(
        hass, freezer, options=options, actual=(4, [0.33] * 8), short_range=(14, [20.0] * 4)
    )
    await coordinator.async_set_min_block_hours(0.5)
    await coordinator.async_set_max_price(100.0)
    await coordinator.async_set_ready_by(START + timedelta(hours=14))
    await coordinator.async_set_required_hours(1.0)
    await coordinator.async_set_opportunistic_max_price(100.0)
    await coordinator.async_set_opportunistic_target_hours(1.0)
    await coordinator.async_set_opportunistic_enabled(True)

    sessions = coordinator.data["sessions"]
    hours_by_tier = {
        tier: sum(s["duration_hours"] for s in sessions if s["tier"] == tier) for tier in ("required", "opportunistic")
    }
    covered = {
        parse_dt(s["start"]) + timedelta(minutes=30 * k)
        for s in sessions
        for k in range(round(s["duration_hours"] * 2))
    }

    assert hours_by_tier == {"required": 1.0, "opportunistic": 1.0}
    # All four slots cost the same, so which tier gets which is a tie-break; what matters is
    # that both tiers drew from the short-range window, never overlapped, and flagged it.
    assert covered == {_at(14), _at(15), _at(16), _at(17)}
    assert all(s["estimated"] is True and s["avg_price"] == 20.0 for s in sessions)


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


async def test_price_data_sources_sensor_counts_short_range_sources(hass, freezer):
    await _coordinator(
        hass, freezer, forecast=(0, [50.0] * 2), short_range=(4, [20.0] * 3), prediction=[True, False, True]
    )

    state = hass.states.get("sensor.wholesale_ev_schedule_price_data_sources")

    assert state.attributes["source_counts"] == {
        SOURCE_PREDICTED: 2,
        SOURCE_SHORT_RANGE: 2,
        SOURCE_SHORT_RANGE_FINAL: 1,
    }


async def test_active_providers_includes_the_short_range_provider_only_when_configured(hass, freezer):
    await _coordinator(hass, freezer, short_range=(0, [20.0]))
    state = hass.states.get("sensor.wholesale_ev_schedule_active_providers")

    assert state.state.endswith("Agile Rates API (agilerates.uk, day-ahead)")
    assert state.attributes["short_range_provider"] == "Agile Rates API (agilerates.uk, day-ahead)"


async def test_active_providers_is_unchanged_for_an_entry_that_never_opted_in(hass, freezer):
    await _coordinator(hass, freezer, options={**FULL_OPTIONS, **_ZERO_HOURS})
    state = hass.states.get("sensor.wholesale_ev_schedule_active_providers")

    assert state.state == (
        "Octopus Energy (current/next day rates event entity)"
        " / AgilePredict-compatible API (e.g. AgilePredict, Agile Forecast)"
    )
    assert "short_range_provider" not in state.attributes
    assert set(state.attributes) >= {"rates_provider", "forecast_provider"}


# ---------------------------------------------------------------------------
# Solve cache interplay (issue #60): nothing short-range-specific may go stale
# ---------------------------------------------------------------------------


async def test_unchanged_short_range_data_does_not_resolve_but_changed_data_does(hass, freezer):
    from unittest.mock import patch

    from custom_components.wholesale_ev_schedule import coordinator as coordinator_module

    coordinator = await _coordinator(hass, freezer, **_afternoon_scenario())
    await _schedule(coordinator, required_hours=2.0)

    with patch.object(coordinator_module, "find_optimal_slots", wraps=coordinator_module.find_optimal_slots) as spy:
        await coordinator.async_refresh()
        await coordinator.async_refresh()
        assert spy.call_count == 0

        set_short_range_entity(hass, agile_rates_points(_at(14), [19.0] * 4))
        await coordinator.async_refresh()
        assert spy.call_count >= 1

    assert coordinator.data["sessions"][0]["avg_price"] == pytest.approx(19.0)


async def test_every_session_dict_from_a_run_is_json_serialisable_for_storage(hass, freezer):
    """Sessions are persisted via HA's Store, which serialises to JSON."""
    import json

    coordinator = await _coordinator(hass, freezer, **_afternoon_scenario())
    await _schedule(coordinator, required_hours=2.0)

    json.dumps(coordinator.data["sessions"])
    assert math.isfinite(coordinator.data["sessions"][0]["avg_price"])
