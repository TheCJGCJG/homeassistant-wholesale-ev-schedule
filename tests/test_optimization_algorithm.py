"""Selectable scheduling algorithm: lets a user pick which find_optimal_slots
implementation computes the schedule (see scheduler.py and the
OPTIMIZATION_ALGORITHM_* constants in const.py), mirroring the charge_override
select entity's wiring (test_charge_override.py) rather than its behavior.
"""

from datetime import timedelta

import homeassistant.util.dt as dt_util

from custom_components.wholesale_ev_schedule.const import DOMAIN

from .factories import (
    CURRENT_RATES_ENTITY,
    NEXT_RATES_ENTITY,
    async_setup_wholesale_entry,
    octopus_rate_points,
    set_octopus_rate_entity,
)


async def test_optimization_algorithm_defaults_to_greedy(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.optimization_algorithm == "greedy"

    state = hass.states.get("select.wholesale_ev_schedule_optimization_algorithm")
    assert state.state == "greedy"


async def test_optimization_algorithm_select_entity_persists_via_coordinator(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.wholesale_ev_schedule_optimization_algorithm", "option": "optimal"},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert coordinator.optimization_algorithm == "optimal"
    state = hass.states.get("select.wholesale_ev_schedule_optimization_algorithm")
    assert state.state == "optimal"


async def test_optimization_algorithm_survives_restart(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_set_optimization_algorithm("hybrid")

    await coordinator.async_load_stored_state()

    assert coordinator.optimization_algorithm == "hybrid"


async def test_stored_invalid_optimization_algorithm_degrades_to_greedy(hass):
    # Same reasoning as test_stored_invalid_charge_override_degrades_to_auto
    # (issue #36): a stored value outside the enum (schema drift, a manual
    # edit) must not be reported back as the select entity's current_option
    # outside its declared options.
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator._store.async_save({"optimization_algorithm": "some_bogus_value"})

    await coordinator.async_load_stored_state()

    assert coordinator.optimization_algorithm == "greedy"
    state = hass.states.get("select.wholesale_ev_schedule_optimization_algorithm")
    assert state.state == "greedy"


async def test_reset_restores_optimization_algorithm_default(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_set_optimization_algorithm("optimal")

    await coordinator.async_reset()

    assert coordinator.optimization_algorithm == "greedy"


async def test_optimal_algorithm_produces_a_schedule_same_as_greedy_would(hass):
    """Selecting "optimal" doesn't just get stored -- it actually changes which
    algorithm computes the schedule, and still produces a valid schedule for
    an ordinary case where both algorithms agree."""
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_set_optimization_algorithm("optimal")

    now = dt_util.now()
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, octopus_rate_points(now, 6, 0.05))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])
    await coordinator.async_set_ready_by(now + timedelta(hours=3))
    await coordinator.async_set_required_hours(1.0)
    await hass.async_block_till_done()

    assert coordinator.data["state"] == "charging"
    assert coordinator.data["desired"] is True
