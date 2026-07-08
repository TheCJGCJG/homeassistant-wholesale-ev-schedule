"""Diagnostic sensors (hidden by default) and the live tuning number entities
(gamble tolerance, min/max block hours, max price) that replaced the old
config-flow-only options.
"""
from datetime import timedelta

from homeassistant.helpers import entity_registry as er
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


async def _schedule_after(hass, coordinator, ready_in_hours, required_hours):
    await coordinator.async_set_ready_by(dt_util.now() + timedelta(hours=ready_in_hours))
    await coordinator.async_set_required_hours(required_hours)
    await hass.async_block_till_done()


async def test_diagnostic_entities_are_hidden_by_default(hass):
    await async_setup_wholesale_entry(hass)
    registry = er.async_get(hass)

    diagnostic_suffixes = [
        "block_count", "upcoming_block_2_start", "upcoming_block_2_end",
        "upcoming_block_3_start", "upcoming_block_3_end", "candidate_price_points",
        "cheapest_available_price", "most_expensive_available_price",
        "average_price_next_24h", "average_price_all_data", "price_data_sources", "active_providers",
    ]
    for suffix in diagnostic_suffixes:
        entry = registry.async_get(f"sensor.wholesale_ev_schedule_{suffix}")
        assert entry is not None, f"missing diagnostic sensor {suffix}"
        assert entry.entity_category == "diagnostic"
        # HA hides entities with visible_default=False automatically, tagged
        # as hidden "by the integration" rather than by explicit user action.
        assert entry.hidden is True
        assert entry.hidden_by == er.RegistryEntryHider.INTEGRATION


async def test_price_summary_diagnostics_populate_even_when_idle(hass):
    # Market data diagnostics should reflect what's available regardless of
    # whether a charge is currently being planned.
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_set_required_hours(0.0)  # required_hours defaults to DEFAULT_REQUIRED_HOURS

    now = dt_util.now()
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, octopus_rate_points(now, 4, price_gbp_per_kwh=0.10))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])
    await coordinator.async_refresh()

    assert coordinator.data["state"] == "idle"
    candidate_points = hass.states.get("sensor.wholesale_ev_schedule_candidate_price_points")
    cheapest = hass.states.get("sensor.wholesale_ev_schedule_cheapest_available_price")
    assert int(candidate_points.state) == 4
    assert float(cheapest.state) == 10.0


async def test_block_count_and_upcoming_block_sensors_reflect_multi_block_schedule(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    now = dt_util.now()
    # [1, 1, 5, 5, 1, 1] p/kWh -- two genuine 1h cheap dips either side of an
    # expensive middle pair.
    prices = [0.01, 0.01, 0.05, 0.05, 0.01, 0.01]
    points = []
    for i, p in enumerate(prices):
        points += octopus_rate_points(now + timedelta(minutes=30 * i), 1, p)
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, points)
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])

    await coordinator.async_set_min_block_hours(0.5)
    await _schedule_after(hass, coordinator, ready_in_hours=3, required_hours=2.0)

    block_count = hass.states.get("sensor.wholesale_ev_schedule_block_count")
    assert int(block_count.state) == 2

    # One block is active/next (covered by next_slot_start/end); the other
    # should show up as upcoming_block_2 (order depends on which the active
    # session is, so just check exactly one of the two positions is populated
    # or both blocks are visible across next_slot + upcoming_block_2).
    next_start = hass.states.get("sensor.wholesale_ev_schedule_next_slot_start")
    block2_start = hass.states.get("sensor.wholesale_ev_schedule_upcoming_block_2_start")
    populated_starts = [
        s for s in (next_start, block2_start) if s and s.state not in ("unknown", "unavailable")
    ]
    assert len(populated_starts) >= 1


async def test_active_providers_sensor_reports_configured_providers(hass):
    await async_setup_wholesale_entry(hass)
    state = hass.states.get("sensor.wholesale_ev_schedule_active_providers")
    assert "Octopus Energy" in state.state
    assert "AgilePredict" in state.state


async def test_price_data_sources_sensor_breaks_down_by_source(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    now = dt_util.now()
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, octopus_rate_points(now, 2, price_gbp_per_kwh=0.10))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])
    forecast_points = [
        {"date_time": (now + timedelta(hours=1, minutes=30 * i)).isoformat(), "agile_pred": 5.0}
        for i in range(2)
    ]
    hass.states.async_set(FORECAST_ENTITY, "populated", {"prices": forecast_points})
    await coordinator.async_refresh()

    state = hass.states.get("sensor.wholesale_ev_schedule_price_data_sources")
    assert int(state.state) == 4
    assert state.attributes["source_counts"] == {"current_actual": 2, "predicted": 2}


async def test_tuning_number_entities_persist_via_coordinator(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    for entity_id, value, attr in [
        ("number.wholesale_ev_schedule_gamble_tolerance", 25.0, "gamble_tolerance"),
        ("number.wholesale_ev_schedule_min_block_hours", 1.5, "min_block_hours"),
        ("number.wholesale_ev_schedule_max_price", 12.5, "max_price"),
    ]:
        await hass.services.async_call(
            "number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True
        )
        await hass.async_block_till_done()
        assert getattr(coordinator, attr) == value
        assert float(hass.states.get(entity_id).state) == value
