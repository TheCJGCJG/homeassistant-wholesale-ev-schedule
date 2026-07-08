"""Boost and stop lifecycle: number.set_value / button.press against a real
config entry setup."""
from datetime import timedelta

import homeassistant.util.dt as dt_util

from custom_components.wholesale_ev_schedule.const import DOMAIN

from .factories import async_setup_wholesale_entry


async def test_boost_number_starts_boost_and_self_resets(hass):
    entry = await async_setup_wholesale_entry(hass)

    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": "number.wholesale_ev_schedule_boost_duration_hours", "value": 2.0},
        blocking=True,
    )
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.data["state"] == "boosting"
    assert coordinator.data["desired"] is True

    # The slider must self-reset to 0 so it doesn't silently re-trigger a new
    # boost once this one expires.
    slider = hass.states.get("number.wholesale_ev_schedule_boost_duration_hours")
    assert float(slider.state) == 0.0


async def test_boost_cancel_button_ends_boost(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await coordinator.async_start_boost(2.0)
    assert coordinator.data["state"] == "boosting"

    await hass.services.async_call(
        "button", "press",
        {"entity_id": "button.wholesale_ev_schedule_boost_cancel"},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert coordinator.data["state"] != "boosting"


async def test_stop_button_clears_schedule_and_boost(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await coordinator.async_set_ready_by(dt_util.now() + timedelta(hours=6))
    await coordinator.async_set_required_hours(2.0)
    await coordinator.async_start_boost(1.0)
    assert coordinator.data["state"] == "boosting"

    await hass.services.async_call(
        "button", "press",
        {"entity_id": "button.wholesale_ev_schedule_stop"},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert coordinator.data["state"] == "idle"
    assert coordinator.required_hours == 0.0
    hours_entity = hass.states.get("number.wholesale_ev_schedule_charging_hours_required")
    assert float(hours_entity.state) == 0.0


async def test_stop_button_does_not_clear_ready_by(hass):
    # Distinguishes stop (cancel today, deadline still applies tomorrow) from
    # reset (full clean slate, e.g. on charger-unplugged).
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    ready_by = dt_util.now() + timedelta(hours=6)
    await coordinator.async_set_ready_by(ready_by)
    await coordinator.async_set_required_hours(2.0)

    await hass.services.async_call(
        "button", "press",
        {"entity_id": "button.wholesale_ev_schedule_stop"},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert coordinator.ready_by == ready_by


async def test_reset_button_clears_everything_including_ready_by(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_set_ready_by(dt_util.now() + timedelta(hours=6))
    await coordinator.async_set_required_hours(2.0)
    await coordinator.async_start_boost(1.0)
    assert coordinator.data["state"] == "boosting"

    await hass.services.async_call(
        "button", "press",
        {"entity_id": "button.wholesale_ev_schedule_reset"},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert coordinator.data["state"] == "idle"
    assert coordinator.ready_by is None
    assert coordinator.required_hours == 0.0
    ready_by_entity = hass.states.get("datetime.wholesale_ev_schedule_ready_by")
    assert ready_by_entity.state == "unknown"


async def test_boost_ends_at_sensor_reflects_boost_end_and_clears_after(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await coordinator.async_start_boost(2.0)
    await hass.async_block_till_done()
    boost_ends_at = hass.states.get("sensor.wholesale_ev_schedule_boost_ends_at")
    assert boost_ends_at.state not in ("unknown", "unavailable", None)

    await coordinator.async_cancel_boost()
    await hass.async_block_till_done()
    boost_ends_at = hass.states.get("sensor.wholesale_ev_schedule_boost_ends_at")
    assert boost_ends_at.state == "unknown"
