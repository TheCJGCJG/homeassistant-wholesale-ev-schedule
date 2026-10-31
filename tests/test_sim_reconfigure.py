"""Changing the integration's options while it is running: the reload that follows must keep the
charger steady, re-plan from the new wiring, and leave nothing duplicated behind."""

from datetime import datetime

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.wholesale_ev_schedule.const import (
    CONF_ENABLE_OPPORTUNISTIC_CHARGING,
    CONF_FORECAST_ENTITY,
    CONF_FORECAST_PROVIDER,
    CONF_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
)

from .factories import EXPECTED_ENTITY_IDS, FULL_OPTIONS, expected_opportunistic_entity_ids, short_range_options
from .sim import UTC, PriceWorld, Sim


def _at(day, hour, minute=0):
    return datetime(2026, 3, day, hour, minute, tzinfo=UTC)


async def _sim(hass, freezer, **kwargs):
    kwargs.setdefault("start", "2026-03-02 16:00:00+00:00")
    kwargs.setdefault("required_hours", 3.0)
    kwargs.setdefault("min_block_hours", 3.0)
    return await Sim.create(hass, freezer, world=PriceWorld(seed=1), **kwargs)


async def _reconfigure(hass, sim, **changes):
    hass.config_entries.async_update_entry(sim.entry, options={**sim.entry.options, **changes})
    await hass.async_block_till_done()


def _entity_ids(hass, sim):
    return {e.entity_id for e in er.async_entries_for_config_entry(er.async_get(hass), sim.entry.entry_id)}


async def test_a_reload_in_the_middle_of_a_charge_never_switches_the_charger_off(hass, freezer):
    sim = await _sim(hass, freezer)
    await sim.run_to(_at(3, 1, 0))
    assert sim.data["desired"] is True

    await _reconfigure(hass, sim, **{CONF_UPDATE_INTERVAL_MINUTES: 15})

    assert sim.data["desired"] is True and sim.data["state"] == "charging"
    sim.timeline.append(sim._observe())
    await sim.run_to(_at(3, 8))
    assert sim.on_minutes(_at(3, 0), _at(3, 7)) == 180  # one uninterrupted 3h


@pytest.mark.parametrize("interval", [1, 15, 60])
async def test_the_new_poll_interval_takes_effect_and_nothing_else_changes(hass, freezer, interval):
    sim = await _sim(hass, freezer)
    settings = (sim.coordinator.required_hours, sim.coordinator.max_price, sim.coordinator.ready_by)
    ids = _entity_ids(hass, sim)

    await _reconfigure(hass, sim, **{CONF_UPDATE_INTERVAL_MINUTES: interval})

    assert sim.coordinator.update_interval.total_seconds() == interval * 60
    assert (sim.coordinator.required_hours, sim.coordinator.max_price, sim.coordinator.ready_by) == settings
    assert _entity_ids(hass, sim) == ids


async def test_dropping_the_long_range_forecast_mid_plan_replans_from_the_remaining_sources(hass, freezer):
    sim = await _sim(hass, freezer, min_block_hours=0.5)
    assert any(p["source"] == "predicted" for p in sim.coordinator._collect_prices())

    await _reconfigure(hass, sim, **{CONF_FORECAST_PROVIDER: "none", CONF_FORECAST_ENTITY: None})

    assert not any(p["source"] == "predicted" for p in sim.coordinator._collect_prices())
    assert sim.data["state"] in ("scheduled", "charging")
    await sim.run_to(_at(3, 8))
    assert sim.on_minutes(_at(2, 16), _at(3, 7)) == 180


async def test_turning_short_range_on_while_running_starts_using_it(hass, freezer):
    sim = await _sim(hass, freezer, start="2026-03-02 11:00:00+00:00", feeds=("actual", "forecast", "short_range"))
    assert not any(p["source"].startswith("short_range") for p in sim.coordinator._collect_prices())

    await _reconfigure(hass, sim, **short_range_options(FULL_OPTIONS))
    await sim.step(5)

    assert any(p["source"].startswith("short_range") for p in sim.coordinator._collect_prices())
    assert sim.coordinator.short_range_provider_label is not None


async def test_enabling_and_disabling_opportunistic_charging_adds_and_removes_exactly_its_entities(hass, freezer):
    sim = await _sim(hass, freezer)
    assert _entity_ids(hass, sim) == EXPECTED_ENTITY_IDS

    await _reconfigure(hass, sim, **{CONF_ENABLE_OPPORTUNISTIC_CHARGING: True})
    assert _entity_ids(hass, sim) == EXPECTED_ENTITY_IDS | expected_opportunistic_entity_ids()

    await _reconfigure(hass, sim, **{CONF_ENABLE_OPPORTUNISTIC_CHARGING: False})
    assert _entity_ids(hass, sim) == EXPECTED_ENTITY_IDS
    assert hass.states.get("switch.wholesale_ev_schedule_opportunistic_charging_enabled") is None


async def test_many_reloads_leave_one_coordinator_and_one_of_every_entity(hass, freezer):
    sim = await _sim(hass, freezer)
    ids = _entity_ids(hass, sim)
    for i in range(8):
        await _reconfigure(hass, sim, **{CONF_UPDATE_INTERVAL_MINUTES: 5 + i % 2})
        await sim.step(5)

    assert _entity_ids(hass, sim) == ids
    assert list(hass.data[DOMAIN]) == [sim.entry.entry_id]
    assert len([s for s in hass.states.async_all() if s.entity_id.startswith("number.wholesale_ev_schedule_")]) == 6


async def test_a_reload_triggers_exactly_one_minute_tick_listener_not_one_per_reload(hass, freezer):
    from unittest.mock import patch

    sim = await _sim(hass, freezer)
    for _ in range(4):
        await sim.restart()
    calls = []
    original = sim.coordinator.async_request_refresh

    async def counting():
        calls.append(1)
        await original()

    with patch.object(sim.coordinator, "async_request_refresh", counting):
        from pytest_homeassistant_custom_component.common import async_fire_time_changed

        freezer.move_to(_at(2, 16, 1))
        async_fire_time_changed(hass, _at(2, 16, 1))
        await hass.async_block_till_done()

    assert len(calls) == 1, (
        f"the minute tick fired {len(calls)} times; listeners from earlier reloads were not torn down"
    )
