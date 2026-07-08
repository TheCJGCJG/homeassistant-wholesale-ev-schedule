"""Manual charge override: a universal force-on/force-off select, replacing
the old charger-specific "work state" entity wiring so this integration
doesn't need to know anything about a particular charger brand.
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


async def _schedule_after(hass, coordinator, ready_in_hours, required_hours):
    await coordinator.async_set_ready_by(dt_util.now() + timedelta(hours=ready_in_hours))
    await coordinator.async_set_required_hours(required_hours)
    await hass.async_block_till_done()


async def test_auto_override_follows_the_computed_schedule(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.charge_override == "auto"

    now = dt_util.now()
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, octopus_rate_points(now, 6, 0.05))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])
    await _schedule_after(hass, coordinator, ready_in_hours=3, required_hours=1.0)

    assert coordinator.data["state"] == "charging"
    assert coordinator.data["desired"] is True


async def test_force_off_overrides_an_active_charging_slot(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    now = dt_util.now()
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, octopus_rate_points(now, 6, 0.05))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])
    await _schedule_after(hass, coordinator, ready_in_hours=3, required_hours=1.0)
    assert coordinator.data["desired"] is True  # sanity: would be charging without the override

    await coordinator.async_set_charge_override("force_off")

    assert coordinator.data["state"] == "charging"  # the schedule itself is unaffected
    assert coordinator.data["desired"] is False  # but the physical output is forced off


async def test_force_on_overrides_idle(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.data["state"] == "idle"

    await coordinator.async_set_charge_override("force_on")

    assert coordinator.data["state"] == "idle"
    assert coordinator.data["desired"] is True


async def test_force_off_overrides_an_active_boost(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await coordinator.async_start_boost(2.0)
    assert coordinator.data["state"] == "boosting"
    assert coordinator.data["desired"] is True  # sanity

    await coordinator.async_set_charge_override("force_off")

    assert coordinator.data["desired"] is False


async def test_override_select_entity_persists_via_coordinator(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": "select.wholesale_ev_schedule_charge_override", "option": "force_on"},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert coordinator.charge_override == "force_on"
    state = hass.states.get("select.wholesale_ev_schedule_charge_override")
    assert state.state == "force_on"


async def test_override_survives_restart(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_set_charge_override("force_on")

    await coordinator.async_load_stored_state()

    assert coordinator.charge_override == "force_on"
