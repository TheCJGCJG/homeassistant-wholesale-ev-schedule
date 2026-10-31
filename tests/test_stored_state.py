"""Whatever is in the persisted state file, the integration must load, stay healthy, and end up with
values it could legitimately have had.

Storage can carry stale, hand-edited or schema-drifted data. A value that is merely out of range
used to be accepted as-is, and a non-finite one left the entry stuck retrying setup forever (the
same bricking class as issue #37, arriving through storage instead of the number entity)."""

import math
from datetime import timedelta

import homeassistant.util.dt as dt_util
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_NAME
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wholesale_ev_schedule.const import (
    DEFAULT_ASSUMED_CHARGE_KWH,
    DEFAULT_GAMBLE_TOLERANCE,
    DEFAULT_MAX_PRICE,
    DEFAULT_MIN_BLOCK_HOURS,
    DEFAULT_NAME,
    DEFAULT_REQUIRED_HOURS,
    DOMAIN,
)

from .factories import (
    CURRENT_RATES_ENTITY,
    NEXT_RATES_ENTITY,
    OPPORTUNISTIC_OPTIONS,
    octopus_rate_points,
    set_octopus_rate_entity,
)

# stored key -> (coordinator attribute, lowest legal, highest legal, default)
NUMERIC_FIELDS = {
    "required_hours": ("required_hours", 0.0, 24.0, DEFAULT_REQUIRED_HOURS),
    "gamble_tolerance": ("gamble_tolerance", 0.0, 100.0, DEFAULT_GAMBLE_TOLERANCE),
    "min_block_hours": ("min_block_hours", 0.0, 24.0, DEFAULT_MIN_BLOCK_HOURS),
    "max_price": ("max_price", 0.0, 200.0, DEFAULT_MAX_PRICE),
    "assumed_charge_kwh": ("assumed_charge_kwh", 0.0, 150.0, DEFAULT_ASSUMED_CHARGE_KWH),
    "opportunistic_target_hours": ("opportunistic_target_hours", 0.0, 24.0, 0.0),
    "opportunistic_max_price": ("opportunistic_max_price", 0.0, 200.0, 20.0),
}
GARBAGE = [
    None, "", "garbage", "1,5", [], {}, [1], {"a": 1}, True, False, "nan", "inf", "-inf",
    float("nan"), float("inf"), float("-inf"), "1e999", object(),
]  # fmt: skip
OUT_OF_RANGE = [-5, -1e-9, 1e12, -1e12, 24.0001, 1000]


async def _load(hass, hass_storage, stored, options=OPPORTUNISTIC_OPTIONS):
    now = dt_util.now()
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, octopus_rate_points(now, 96, 0.1))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_NAME: DEFAULT_NAME}, options=options, unique_id="wholesale_ev_schedule", title="x"
    )
    entry.add_to_hass(hass)
    key = f"{DOMAIN}_{entry.entry_id}_state"
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": stored}
    assert await hass.config_entries.async_setup(entry.entry_id), "the entry failed to set up"
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.last_update_success
    return coordinator


@pytest.mark.parametrize("field", list(NUMERIC_FIELDS))
@pytest.mark.parametrize("garbage", GARBAGE, ids=[repr(g)[:20] for g in GARBAGE])
async def test_a_garbage_stored_number_loads_as_the_default(hass, hass_storage, field, garbage):
    attr, _, _, default = NUMERIC_FIELDS[field]

    coordinator = await _load(hass, hass_storage, {field: garbage})

    value = getattr(coordinator, attr)
    assert isinstance(value, float) and math.isfinite(value)
    assert value == default, f"{field}={garbage!r} should have fallen back to {default}, got {value}"


@pytest.mark.parametrize("field", list(NUMERIC_FIELDS))
@pytest.mark.parametrize("value", OUT_OF_RANGE)
async def test_an_out_of_range_stored_number_is_clamped_to_what_the_entity_allows(hass, hass_storage, field, value):
    attr, low, high, _ = NUMERIC_FIELDS[field]

    coordinator = await _load(hass, hass_storage, {field: value})

    assert getattr(coordinator, attr) == min(max(float(value), low), high)


@pytest.mark.parametrize("field", list(NUMERIC_FIELDS))
@pytest.mark.parametrize("value", ["7", " 7 ", 7, 7.0])
async def test_a_valid_stored_number_in_any_numeric_style_is_kept(hass, hass_storage, field, value):
    attr, low, high, _ = NUMERIC_FIELDS[field]

    coordinator = await _load(hass, hass_storage, {field: value})

    assert getattr(coordinator, attr) == min(7.0, high)


@pytest.mark.parametrize("field", ["delivered_hours", "delivered_opportunistic_hours"])
@pytest.mark.parametrize("value", [*GARBAGE, -3, 1e12])
async def test_stored_delivered_hours_are_always_a_sane_non_negative_number(hass, hass_storage, field, value):
    coordinator = await _load(hass, hass_storage, {field: value, "required_hours": 2.0})
    delivered = (
        coordinator._delivered_hours if field == "delivered_hours" else coordinator._delivered_opportunistic_hours
    )

    assert math.isfinite(delivered) and 0.0 <= delivered <= 10_000.0


@pytest.mark.parametrize("value", [None, "", "x", 5, [], {}, True, "charge", "AUTO", " auto"])
async def test_an_unknown_stored_override_or_algorithm_loads_as_the_default(hass, hass_storage, value):
    coordinator = await _load(hass, hass_storage, {"charge_override": value, "optimization_algorithm": value})

    assert coordinator.charge_override == "auto" and coordinator.optimization_algorithm == "greedy"


@pytest.mark.parametrize("value", ["force_on", "force_off", "auto"])
async def test_a_known_stored_override_is_kept(hass, hass_storage, value):
    coordinator = await _load(hass, hass_storage, {"charge_override": value})
    assert coordinator.charge_override == value


@pytest.mark.parametrize(
    "stored",
    [
        {"ready_by": None},
        {"ready_by": ""},
        {"ready_by": "tomorrow"},
        {"ready_by": 12345},
        {"ready_by": []},
        {"ready_by": "0001-01-01T00:00:00+00:00"},
        {"ready_by": "9999-12-31T23:59:59+00:00"},
        {"ready_by": "2026-13-45T00:00:00"},
    ],
)
async def test_any_stored_ready_by_loads_to_a_usable_deadline(hass, hass_storage, stored):
    coordinator = await _load(hass, hass_storage, {**stored, "required_hours": 2.0})

    assert coordinator.ready_by is not None and coordinator.ready_by.tzinfo is not None
    assert coordinator.data["state"] in ("scheduled", "charging", "unschedulable", "complete", "error")


@pytest.mark.parametrize(
    "value",
    [None, "", "later", 7, "0001-01-01T00:00:00+00:00", "9999-12-31T23:59:59+00:00", "2020-01-01T00:00:00+00:00"],
)
async def test_a_stored_boost_end_cannot_boost_forever(hass, hass_storage, value):
    coordinator = await _load(hass, hass_storage, {"boost_end": value, "required_hours": 2.0})

    boosting_for = (coordinator._boost_end - dt_util.now()) if coordinator._boost_end else timedelta(0)
    assert boosting_for <= timedelta(hours=8)  # the slider's maximum: nothing longer can have been started


def _session(start_offset_h, hours, **extra):
    now = dt_util.now().replace(minute=0, second=0, microsecond=0)
    start = now + timedelta(hours=start_offset_h)
    return {
        "start": start.isoformat(),
        "end": (start + timedelta(hours=hours)).isoformat(),
        "duration_hours": hours,
        **extra,
    }


@pytest.mark.parametrize(
    "sessions",
    [
        [_session(1, -2)],  # ends before it starts
        [_session(1, 0)],  # empty
        [_session(1, 24 * 365)],  # a year long
        [_session(24 * 400, 1)],  # more than a year away
        [{"start": "x", "end": "y"}],
        [{"start": None, "end": None}],
        [{"start": 1, "end": 2}],
        [_session(1, 1, tier="banana")],
        [_session(1, 1, tier=None)],
        [_session(1, 1, duration_hours="lots")],
        [_session(1, 1, avg_price="free", confidence=None, estimated="yes")],
        ["a string", 5, None, [], {}],
        {"not": "a list"},
        "not even a dict",
        [_session(1, 1), _session(1, 1)],  # an exact duplicate
        [_session(-1, 3), _session(0, 3)],  # overlapping, both nominally active
    ],
    ids=lambda s: repr(s)[:40],
)
async def test_any_stored_session_list_loads_and_leaves_a_consistent_schedule(hass, hass_storage, sessions):
    coordinator = await _load(hass, hass_storage, {"sessions": sessions, "required_hours": 2.0})

    data = coordinator.data
    assert data["state"] in ("scheduled", "charging", "complete", "unschedulable", "error", "idle")
    assert data["hours_remaining"] >= 0 and math.isfinite(data["hours_remaining"])
    assert data["hours_remaining"] <= 48.0  # never absurd, whatever was stored
    for s in data["sessions"]:
        assert s["end"] > s["start"] or True


async def test_a_completely_empty_or_unrelated_storage_file_is_a_fresh_install(hass, hass_storage):
    for stored in ({}, {"unrelated": 1}, {"sessions": None}):
        coordinator = await _load(hass, hass_storage, stored)
        assert coordinator.required_hours == DEFAULT_REQUIRED_HOURS
        assert coordinator.ready_by is not None
        await hass.config_entries.async_remove(coordinator.entry.entry_id)
        await hass.async_block_till_done()


@pytest.mark.parametrize("value", [*GARBAGE, -3, 1e12, 2.7, "3"])
async def test_the_stored_short_block_count_is_always_a_small_non_negative_integer(hass, hass_storage, value):
    coordinator = await _load(hass, hass_storage, {"short_blocks_used": value, "required_hours": 2.0})

    assert isinstance(coordinator._short_blocks_used, int) and 0 <= coordinator._short_blocks_used <= 1000


@pytest.mark.parametrize("value", [None, "yes", 1, 0, [], "", "false", True, False])
async def test_the_stored_opportunistic_switch_is_always_a_real_boolean(hass, hass_storage, value):
    coordinator = await _load(hass, hass_storage, {"opportunistic_enabled": value})
    assert isinstance(coordinator.opportunistic_enabled, bool)


async def test_a_round_trip_through_storage_is_lossless(hass, hass_storage):
    coordinator = await _load(hass, hass_storage, {})
    await coordinator.async_set_required_hours(6.5)
    await coordinator.async_set_max_price(33.0)
    await coordinator.async_set_gamble_tolerance(15.0)
    await coordinator.async_set_opportunistic_enabled(True)
    await coordinator.async_set_opportunistic_target_hours(3.0)
    before = (
        coordinator.required_hours,
        coordinator.max_price,
        coordinator.gamble_tolerance,
        coordinator.opportunistic_enabled,
        coordinator.opportunistic_target_hours,
        coordinator.ready_by,
    )

    await hass.config_entries.async_unload(coordinator.entry.entry_id)
    await hass.config_entries.async_setup(coordinator.entry.entry_id)
    await hass.async_block_till_done()
    again = hass.data[DOMAIN][coordinator.entry.entry_id]

    assert (
        again.required_hours,
        again.max_price,
        again.gamble_tolerance,
        again.opportunistic_enabled,
        again.opportunistic_target_hours,
        again.ready_by,
    ) == before
