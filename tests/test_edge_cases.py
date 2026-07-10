"""Edge cases: malformed price data, ready_by already in the past, an
in-progress session surviving a price refresh, and a boost expiring naturally
(as opposed to being cancelled via the button)."""

import math
from datetime import timedelta

import homeassistant.util.dt as dt_util

from custom_components.wholesale_ev_schedule.const import DEFAULT_READY_BY_HOUR, DOMAIN

from .factories import (
    CURRENT_RATES_ENTITY,
    FORECAST_ENTITY,
    NEXT_RATES_ENTITY,
    async_setup_wholesale_entry,
    octopus_rate_points,
    set_octopus_rate_entity,
)


async def test_malformed_rate_point_is_skipped_not_crashed(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    now = dt_util.now()
    points = octopus_rate_points(now, 6, 0.05)
    points[2]["value_inc_vat"] = "not-a-number"  # malformed — must be skipped, not raise
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, points)
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])

    await coordinator.async_set_ready_by(now + timedelta(hours=3))
    await coordinator.async_set_required_hours(1.0)
    await hass.async_block_till_done()

    assert coordinator.data["state"] in ("scheduled", "charging")


async def test_malformed_forecast_point_is_skipped_not_crashed(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    now = dt_util.now()
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, octopus_rate_points(now, 6, 0.05))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])
    forecast_points = [
        {"date_time": (now + timedelta(minutes=30 * i)).isoformat(), "agile_pred": 3.0} for i in range(6)
    ]
    forecast_points[3]["agile_pred"] = "not-a-number"  # malformed — must be skipped, not raise
    forecast_points[4] = {"date_time": None, "agile_pred": 3.0}  # missing datetime — must be skipped
    hass.states.async_set(FORECAST_ENTITY, "populated", {"prices": forecast_points})

    await coordinator.async_set_ready_by(now + timedelta(hours=3))
    await coordinator.async_set_required_hours(1.0)
    await hass.async_block_till_done()

    assert coordinator.data["state"] in ("scheduled", "charging")


async def test_rate_attribute_explicitly_none_is_skipped_not_crashed(hass):
    # Regression for issue #24. entity.attributes.get(attribute, []) only
    # applies its default when the key is absent -- an explicit `None` value
    # (a real "not yet populated" pattern some sources use) previously reached
    # `for rate in None:` and crashed the whole coordinator update.
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    now = dt_util.now()
    hass.states.async_set(CURRENT_RATES_ENTITY, "populated", {"rates": None})
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])

    await coordinator.async_set_ready_by(now + timedelta(hours=3))
    await coordinator.async_set_required_hours(1.0)
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    assert coordinator.data["state"] in ("error", "unschedulable")


async def test_forecast_attribute_explicitly_none_is_skipped_not_crashed(hass):
    # Regression for issue #24, forecast side.
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    now = dt_util.now()
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, octopus_rate_points(now, 6, 0.05))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])
    hass.states.async_set(FORECAST_ENTITY, "populated", {"prices": None})

    await coordinator.async_set_ready_by(now + timedelta(hours=3))
    await coordinator.async_set_required_hours(1.0)
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    assert coordinator.data["state"] in ("scheduled", "charging")


async def test_rate_attribute_shaped_as_dict_is_skipped_not_crashed(hass):
    # Regression for issue #32. If the configured attribute is present but
    # shaped as a dict instead of a list of dicts, iterating it yields its
    # string keys -- `rate.get(start_key)` on a str raises AttributeError,
    # which the (TypeError, ValueError) except clause didn't catch.
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    now = dt_util.now()
    hass.states.async_set(CURRENT_RATES_ENTITY, "populated", {"rates": {"start": "bogus"}})
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])

    await coordinator.async_set_ready_by(now + timedelta(hours=3))
    await coordinator.async_set_required_hours(1.0)
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    assert coordinator.data["state"] in ("error", "unschedulable")


async def test_rate_attribute_containing_bare_strings_is_skipped_not_crashed(hass):
    # Regression for issue #32, the "list of non-dict rows" variant.
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    now = dt_util.now()
    hass.states.async_set(CURRENT_RATES_ENTITY, "populated", {"rates": ["not", "a", "dict"]})
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])

    await coordinator.async_set_ready_by(now + timedelta(hours=3))
    await coordinator.async_set_required_hours(1.0)
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    assert coordinator.data["state"] in ("error", "unschedulable")


async def test_nan_price_value_is_skipped_not_silently_poisoning_diagnostics(hass):
    # Regression for issue #33. float() accepts the strings "nan"/"inf"
    # without error (unlike "not-a-number", already covered above) -- must be
    # rejected the same way, not silently poison min/max/average price
    # diagnostics or scheduling ranking via NaN's broken comparison
    # semantics.
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    now = dt_util.now()
    points = octopus_rate_points(now, 6, 0.05)
    points[2]["value_inc_vat"] = "nan"
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, points)
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])

    await coordinator.async_set_ready_by(now + timedelta(hours=3))
    await coordinator.async_set_required_hours(1.0)
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    price_summary = coordinator.data["price_summary"]
    assert price_summary["count"] == 5  # the nan point was skipped, not counted
    assert price_summary["average_price"] == price_summary["average_price"]  # not NaN (NaN != NaN)
    assert price_summary["cheapest_price"] == price_summary["cheapest_price"]
    assert price_summary["most_expensive_price"] == price_summary["most_expensive_price"]


async def test_infinite_forecast_price_value_is_skipped_not_silently_poisoning_diagnostics(hass):
    # Regression for issue #33, forecast side.
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    now = dt_util.now()
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, octopus_rate_points(now, 6, 0.05))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])
    forecast_points = [
        {"date_time": (now + timedelta(minutes=30 * i)).isoformat(), "agile_pred": 3.0} for i in range(6)
    ]
    forecast_points[3]["agile_pred"] = "inf"
    hass.states.async_set(FORECAST_ENTITY, "populated", {"prices": forecast_points})

    await coordinator.async_set_ready_by(now + timedelta(hours=3))
    await coordinator.async_set_required_hours(1.0)
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    price_summary = coordinator.data["price_summary"]
    assert price_summary["count"] == 11  # the infinite forecast point was skipped, not counted
    assert math.isfinite(price_summary["average_price"])
    assert math.isfinite(price_summary["cheapest_price"])
    assert math.isfinite(price_summary["most_expensive_price"])


async def test_malformed_stored_ready_by_degrades_to_default_instead_of_crashing(hass):
    # Regression for issue #25. A valid-JSON-but-wrong-shaped ready_by (schema
    # drift, a manual edit -- HA's own Store helper only protects against
    # outright corrupt/non-JSON files) previously crashed async_load_stored_state
    # with an unhandled ValueError, blocking setup entirely.
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator._store.async_save({"ready_by": "not-a-real-timestamp"})

    await coordinator.async_load_stored_state()

    assert coordinator.ready_by is not None


async def test_malformed_stored_boost_end_degrades_to_none_instead_of_crashing(hass):
    # Regression for issue #25, boost_end side.
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator._store.async_save({"boost_end": "not-a-real-timestamp"})

    await coordinator.async_load_stored_state()

    assert coordinator._boost_end is None


async def test_stored_sessions_shaped_as_dict_is_discarded_instead_of_crashing(hass):
    # Regression for issue #34. Iterating a dict yields its string keys, so
    # `s["start"]` on a string previously raised an unhandled TypeError inside
    # prune_and_classify the next time the coordinator ran an update.
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator._store.async_save({"sessions": {"start": "bogus"}})

    await coordinator.async_load_stored_state()

    assert coordinator._stored_sessions == []


async def test_stored_session_missing_start_key_is_discarded_instead_of_crashing(hass):
    # Regression for issue #34, the "list with a malformed entry" variant --
    # a session missing "start" previously raised an unhandled KeyError.
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    now = dt_util.now()
    good_session = {"start": now.isoformat(), "end": (now + timedelta(hours=1)).isoformat(), "duration_hours": 1.0}
    bad_session = {"end": (now + timedelta(hours=2)).isoformat()}
    await coordinator._store.async_save({"sessions": [good_session, bad_session]})

    await coordinator.async_load_stored_state()

    assert coordinator._stored_sessions == [good_session]


async def test_stored_session_list_containing_a_bare_string_is_discarded_instead_of_crashing(hass):
    # Regression for issue #34, a list whose entries are the wrong type
    # entirely (not dicts at all).
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    now = dt_util.now()
    good_session = {"start": now.isoformat(), "end": (now + timedelta(hours=1)).isoformat(), "duration_hours": 1.0}
    await coordinator._store.async_save({"sessions": [good_session, "not-a-session"]})

    await coordinator.async_load_stored_state()

    assert coordinator._stored_sessions == [good_session]


async def test_stored_required_hours_as_a_numeric_string_is_coerced_instead_of_crashing(hass):
    # Regression for issue #35. required_hours was loaded via bare
    # data.get(key, default) with no float() cast -- a stored numeric string
    # (e.g. from a hand-edited store file) crashed the very next `<= 0`
    # comparison in _async_update_data. A numeric string is coercible, so the
    # fix is to actually use it (float("1.0") == 1.0), not fall back.
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator._store.async_save({"required_hours": "1.0"})

    await coordinator.async_load_stored_state()

    assert coordinator.required_hours == 1.0


async def test_stored_required_hours_as_a_non_numeric_string_degrades_to_default_instead_of_crashing(hass):
    # The genuinely-not-coercible case -- must fall back to the default
    # rather than raise.
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator._store.async_save({"required_hours": "not-a-number"})

    await coordinator.async_load_stored_state()

    assert coordinator.required_hours == coordinator._default_required_hours


async def test_stored_min_block_hours_as_none_degrades_to_default_instead_of_crashing(hass):
    # Regression for issue #35. A stored explicit `null` (e.g. a partially
    # written save) crashed inside scheduler.find_optimal_slots's
    # `min_block_hours * 2` arithmetic instead of degrading.
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator._store.async_save({"min_block_hours": None})

    await coordinator.async_load_stored_state()

    assert coordinator.min_block_hours == coordinator._default_min_block_hours


async def test_ready_by_in_the_past_rolls_forward_instead_of_erroring(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    now = dt_util.now()
    # A full day of cheap data so scheduling succeeds regardless of exactly
    # how far away the next DEFAULT_READY_BY_HOUR occurrence is from "now".
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, octopus_rate_points(now, 48, price_gbp_per_kwh=0.05))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])

    await coordinator.async_set_required_hours(1.0)
    await coordinator.async_set_ready_by(now - timedelta(hours=1))  # already in the past
    await hass.async_block_till_done()

    # No longer errors — ready_by silently rolls forward to the next
    # DEFAULT_READY_BY_HOUR occurrence and scheduling proceeds normally.
    assert coordinator.ready_by > now
    assert coordinator.ready_by.hour == DEFAULT_READY_BY_HOUR
    assert coordinator.data["state"] in ("scheduled", "charging")


async def test_active_session_survives_a_price_refresh(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    now = dt_util.now()
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, octopus_rate_points(now, 6, 0.05))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])
    await coordinator.async_set_ready_by(now + timedelta(hours=3))
    await coordinator.async_set_required_hours(1.0)
    await hass.async_block_till_done()

    active_before = coordinator.data["active_slot"]
    assert active_before is not None

    # Prices change on the next refresh (e.g. a forecast update) — the already
    # in-progress session must not be evicted by the optimizer re-running.
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, octopus_rate_points(now, 6, 0.01))
    await coordinator.async_refresh()

    assert coordinator.data["active_slot"] == active_before


async def test_active_session_survives_all_price_entities_going_unavailable(hass):
    # Regression for issue #31. All price sources going unavailable at once
    # (e.g. right after an HA restart, before the price-source integration
    # has finished loading) is exactly the scenario _error_result exists to
    # report -- but it must not silently drop an already in-progress session;
    # charging_desired flipping off mid-session over a transient, unrelated
    # price-source hiccup would stop the physical charge for no real reason.
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    now = dt_util.now()
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, octopus_rate_points(now, 6, 0.05))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])
    await coordinator.async_set_ready_by(now + timedelta(hours=3))
    await coordinator.async_set_required_hours(1.0)
    await hass.async_block_till_done()

    active_before = coordinator.data["active_slot"]
    assert active_before is not None
    assert coordinator.data["desired"] is True

    hass.states.async_set(CURRENT_RATES_ENTITY, "unavailable", {})
    hass.states.async_set(NEXT_RATES_ENTITY, "unavailable", {})
    hass.states.async_set(FORECAST_ENTITY, "unavailable", {})
    await coordinator.async_refresh()

    assert coordinator.data["state"] == "error"  # still surfaced -- price data really is missing
    assert coordinator.data["active_slot"] == active_before
    assert coordinator.data["desired"] is True


async def test_boost_expires_naturally_and_resumes_normal_state(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    coordinator._boost_end = dt_util.now() - timedelta(seconds=1)  # already expired
    await coordinator.async_refresh()

    assert coordinator.data["state"] != "boosting"
    assert coordinator._boost_end is None
