"""Next-slot average price and the estimated-cost sensor derived from it via
the assumed_charge_kwh live number entity.
"""
from datetime import timedelta

import homeassistant.util.dt as dt_util

from custom_components.wholesale_ev_schedule.const import DEFAULT_ASSUMED_CHARGE_KWH, DOMAIN

from .factories import (
    CURRENT_RATES_ENTITY,
    NEXT_RATES_ENTITY,
    async_setup_wholesale_entry,
    octopus_rate_points,
    set_octopus_rate_entity,
)


async def _schedule_after(hass, coordinator, ready_in_hours, required_hours):
    await coordinator.async_set_ready_by(dt_util.now() + timedelta(hours=ready_in_hours))
    await coordinator.async_set_required_hours(required_hours)
    await hass.async_block_till_done()


async def test_next_slot_average_price_and_estimated_cost_reflect_the_schedule(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.assumed_charge_kwh == DEFAULT_ASSUMED_CHARGE_KWH

    # Expensive now, cheap later — forces a genuine next_slot (see the
    # identical pattern in test_price_parsing.py's timestamp regression test).
    now = dt_util.now()
    expensive_now = octopus_rate_points(now, 2, price_gbp_per_kwh=0.50)
    cheap_later = octopus_rate_points(now + timedelta(hours=1), 4, price_gbp_per_kwh=0.10)
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, expensive_now + cheap_later)
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])

    await _schedule_after(hass, coordinator, ready_in_hours=3, required_hours=1.0)

    next_slot = coordinator.data["next_slot"]
    assert next_slot is not None
    assert next_slot["avg_price"] == 10.0  # 0.10 GBP/kWh * default 100x multiplier

    avg_price_state = hass.states.get("sensor.wholesale_ev_schedule_next_slot_average_price")
    cost_state = hass.states.get("sensor.wholesale_ev_schedule_next_slot_estimated_cost")
    assert float(avg_price_state.state) == 10.0
    assert float(cost_state.state) == 70.0  # 10.0 * 7.0 (default assumed kWh)


async def test_estimated_cost_updates_when_assumed_charge_kwh_changes(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    now = dt_util.now()
    expensive_now = octopus_rate_points(now, 2, price_gbp_per_kwh=0.50)
    cheap_later = octopus_rate_points(now + timedelta(hours=1), 4, price_gbp_per_kwh=0.20)
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, expensive_now + cheap_later)
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])
    await _schedule_after(hass, coordinator, ready_in_hours=3, required_hours=1.0)

    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": "number.wholesale_ev_schedule_assumed_charge_kwh", "value": 10.0},
        blocking=True,
    )
    await hass.async_block_till_done()

    cost_state = hass.states.get("sensor.wholesale_ev_schedule_next_slot_estimated_cost")
    assert float(cost_state.state) == 200.0  # 20.0 avg_price * 10.0 kWh


async def test_next_slot_sensors_are_unknown_when_nothing_scheduled(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_set_required_hours(0.0)  # explicit idle
    await hass.async_block_till_done()

    avg_price_state = hass.states.get("sensor.wholesale_ev_schedule_next_slot_average_price")
    cost_state = hass.states.get("sensor.wholesale_ev_schedule_next_slot_estimated_cost")
    assert avg_price_state.state == "unknown"
    assert cost_state.state == "unknown"


async def test_assumed_charge_kwh_persists_via_coordinator(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": "number.wholesale_ev_schedule_assumed_charge_kwh", "value": 22.5},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert coordinator.assumed_charge_kwh == 22.5
    await coordinator.async_load_stored_state()
    assert coordinator.assumed_charge_kwh == 22.5


async def test_reset_restores_assumed_charge_kwh_default(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_set_assumed_charge_kwh(50.0)
    assert coordinator.assumed_charge_kwh == 50.0

    await coordinator.async_reset()

    assert coordinator.assumed_charge_kwh == DEFAULT_ASSUMED_CHARGE_KWH
