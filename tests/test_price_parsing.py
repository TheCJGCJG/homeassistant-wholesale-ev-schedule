"""Coordinator-level tests for configuration variants: custom price-entity
shapes, missing optional entities, gamble tolerance, and charger connected
states — the axes that make this integration "highly configurable" beyond the
Octopus Energy defaults.
"""
from datetime import timedelta

import homeassistant.util.dt as dt_util

from custom_components.wholesale_ev_schedule.const import (
    CONF_CHARGER_CONNECTED_STATES,
    CONF_FORECAST_ENTITY,
    CONF_GAMBLE_TOLERANCE,
    CONF_RATE_START_KEY,
    CONF_RATE_UNIT_MULTIPLIER,
    CONF_RATE_VALUE_KEY,
    CONF_RATES_ATTRIBUTE,
    DOMAIN,
)
from custom_components.wholesale_ev_schedule.scheduler import parse_dt

from .factories import (
    CHARGER_STATE_ENTITY,
    CURRENT_RATES_ENTITY,
    FORECAST_ENTITY,
    FULL_OPTIONS,
    NEXT_RATES_ENTITY,
    async_setup_wholesale_entry,
    octopus_rate_points,
    set_charger_state,
    set_octopus_rate_entity,
)


async def _schedule_after(hass, coordinator, ready_in_hours, required_hours):
    await coordinator.async_set_ready_by(dt_util.now() + timedelta(hours=ready_in_hours))
    await coordinator.async_set_required_hours(required_hours)
    await hass.async_block_till_done()


async def test_default_octopus_shaped_rates_produce_a_schedule(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    now = dt_util.now()
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, octopus_rate_points(now, 6, 0.05))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])

    await _schedule_after(hass, coordinator, ready_in_hours=3, required_hours=1.0)

    assert coordinator.data["state"] in ("scheduled", "charging")
    sessions = coordinator.data["sessions"]
    assert sessions
    assert sessions[0]["avg_price"] == 5.0  # 0.05 GBP/kWh * default 100x multiplier


async def test_next_slot_sensors_render_as_real_timestamps_not_strings(hass):
    # Regression test: the next_slot_start/end sensors declare device_class
    # "timestamp", which requires native_value to be a real datetime — not
    # the raw ISO string stored in the session dict. Getting this wrong
    # doesn't raise where you'd expect: HA's coordinator listener dispatch
    # swallows the exception from async_write_ha_state and just logs it, so
    # the bug silently leaves the entity's state stuck at "unknown" rather
    # than crashing the test. Assert on the actual rendered state value.
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    # Expensive now, cheap later — forces the optimizer to schedule a slot
    # that starts in the future (next_slot) rather than immediately (which
    # would leave next_slot None, a legitimately different, uninteresting case).
    now = dt_util.now()
    expensive_now = octopus_rate_points(now, 2, price_gbp_per_kwh=0.50)
    cheap_later = octopus_rate_points(now + timedelta(hours=1), 4, price_gbp_per_kwh=0.05)
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, expensive_now + cheap_later)
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])

    await _schedule_after(hass, coordinator, ready_in_hours=3, required_hours=1.0)

    next_slot = coordinator.data["next_slot"]
    assert next_slot is not None

    start_state = hass.states.get("sensor.wholesale_ev_schedule_next_slot_start")
    end_state = hass.states.get("sensor.wholesale_ev_schedule_next_slot_end")

    assert start_state.state not in ("unknown", "unavailable", None)
    assert end_state.state not in ("unknown", "unavailable", None)
    # HA's timestamp device_class rendering truncates to whole seconds, so
    # compare within a tolerance rather than exact equality.
    assert abs(parse_dt(start_state.state) - parse_dt(next_slot["start"])) < timedelta(seconds=1)
    assert abs(parse_dt(end_state.state) - parse_dt(next_slot["end"])) < timedelta(seconds=1)


async def test_custom_rate_attribute_and_keys_are_parsed(hass):
    options = {
        **FULL_OPTIONS,
        CONF_RATES_ATTRIBUTE: "forecasts",
        CONF_RATE_START_KEY: "from",
        CONF_RATE_VALUE_KEY: "per_kwh",
        CONF_RATE_UNIT_MULTIPLIER: 1.0,  # this fake source already reports pence
    }
    entry = await async_setup_wholesale_entry(hass, options)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    now = dt_util.now()
    custom_points = [
        {"from": (now + timedelta(minutes=30 * i)).isoformat(), "per_kwh": 8.0}
        for i in range(6)
    ]
    hass.states.async_set(CURRENT_RATES_ENTITY, "populated", {"forecasts": custom_points})
    hass.states.async_set(NEXT_RATES_ENTITY, "populated", {"forecasts": []})

    await _schedule_after(hass, coordinator, ready_in_hours=3, required_hours=1.0)

    sessions = coordinator.data["sessions"]
    assert sessions
    assert sessions[0]["avg_price"] == 8.0  # multiplier=1 -> no x100 conversion


async def test_missing_forecast_entity_still_schedules_from_actual_rates_only(hass):
    options = {k: v for k, v in FULL_OPTIONS.items() if k != CONF_FORECAST_ENTITY}
    entry = await async_setup_wholesale_entry(hass, options)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    now = dt_util.now()
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, octopus_rate_points(now, 6, 0.05))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])

    await _schedule_after(hass, coordinator, ready_in_hours=3, required_hours=1.0)

    assert coordinator.data["state"] in ("scheduled", "charging")


async def test_gamble_tolerance_zero_excludes_predicted_only_data(hass):
    options = {**FULL_OPTIONS, CONF_GAMBLE_TOLERANCE: 0.0}
    entry = await async_setup_wholesale_entry(hass, options)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    now = dt_util.now()
    # Only forecast data available -- no actual rates at all.
    forecast_points = [
        {"date_time": (now + timedelta(minutes=30 * i)).isoformat(), "agile_pred": 1.0}
        for i in range(6)
    ]
    hass.states.async_set(FORECAST_ENTITY, "populated", {"prices": forecast_points})
    hass.states.async_set(CURRENT_RATES_ENTITY, "populated", {"rates": []})
    hass.states.async_set(NEXT_RATES_ENTITY, "populated", {"rates": []})

    await _schedule_after(hass, coordinator, ready_in_hours=3, required_hours=1.0)

    # gamble_tolerance<=0 hard-excludes predicted-only data, so nothing can be
    # scheduled even though cheap forecast prices exist.
    assert coordinator.data["sessions"] == []


async def test_custom_charger_connected_states_drive_desired(hass):
    options = {**FULL_OPTIONS, CONF_CHARGER_CONNECTED_STATES: "plugged_in,topping_up"}
    entry = await async_setup_wholesale_entry(hass, options)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    now = dt_util.now()
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, octopus_rate_points(now, 6, 0.05))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])
    set_charger_state(hass, "plugged_in")

    await _schedule_after(hass, coordinator, ready_in_hours=3, required_hours=1.0)

    assert coordinator.data["state"] == "charging"
    assert coordinator.data["desired"] is True


async def test_charger_state_outside_connected_states_keeps_desired_false(hass):
    options = {**FULL_OPTIONS, CONF_CHARGER_CONNECTED_STATES: "plugged_in,topping_up"}
    entry = await async_setup_wholesale_entry(hass, options)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    now = dt_util.now()
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, octopus_rate_points(now, 6, 0.05))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])
    set_charger_state(hass, "unplugged")

    await _schedule_after(hass, coordinator, ready_in_hours=3, required_hours=1.0)

    assert coordinator.data["desired"] is False
