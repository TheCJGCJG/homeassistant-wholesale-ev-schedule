"""Edge cases: malformed price data, ready_by already in the past, an
in-progress session surviving a price refresh, and a boost expiring naturally
(as opposed to being cancelled via the button)."""
from datetime import timedelta

import homeassistant.util.dt as dt_util

from custom_components.wholesale_ev_schedule.const import DOMAIN

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
        {"date_time": (now + timedelta(minutes=30 * i)).isoformat(), "agile_pred": 3.0}
        for i in range(6)
    ]
    forecast_points[3]["agile_pred"] = "not-a-number"  # malformed — must be skipped, not raise
    forecast_points[4] = {"date_time": None, "agile_pred": 3.0}  # missing datetime — must be skipped
    hass.states.async_set(FORECAST_ENTITY, "populated", {"prices": forecast_points})

    await coordinator.async_set_ready_by(now + timedelta(hours=3))
    await coordinator.async_set_required_hours(1.0)
    await hass.async_block_till_done()

    assert coordinator.data["state"] in ("scheduled", "charging")


async def test_ready_by_in_the_past_reports_error(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await coordinator.async_set_required_hours(1.0)
    await coordinator.async_set_ready_by(dt_util.now() - timedelta(hours=1))
    await hass.async_block_till_done()

    assert coordinator.data["state"] == "error"
    assert "past" in coordinator.data["error_reason"]


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


async def test_boost_expires_naturally_and_resumes_normal_state(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    coordinator._boost_end = dt_util.now() - timedelta(seconds=1)  # already expired
    await coordinator.async_refresh()

    assert coordinator.data["state"] != "boosting"
    assert coordinator._boost_end is None
