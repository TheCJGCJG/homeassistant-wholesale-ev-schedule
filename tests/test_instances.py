"""Two cars, one Home Assistant: instances must be fully independent -- settings, schedules,
stored state, restarts and removal -- and removing one must leave nothing behind."""

from datetime import datetime, timedelta

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.wholesale_ev_schedule.const import (
    CONF_DEFAULT_READY_BY_HOUR,
    CONF_DEFAULT_REQUIRED_HOURS,
    DOMAIN,
)
from custom_components.wholesale_ev_schedule.scheduler import parse_dt

from .factories import FULL_OPTIONS, async_setup_wholesale_entry
from .sim import UTC, PriceWorld, Sim

START = "2026-03-02 16:00:00+00:00"


def _at(day, hour, minute=0):
    return datetime(2026, 3, day, hour, minute, tzinfo=UTC)


def _storage_key(entry):
    return f"{DOMAIN}_{entry.entry_id}_state"


async def _two_cars(hass, freezer):
    """Car A is the simulator's own entry; car B shares the same price entities."""
    a = await Sim.create(hass, freezer, start=START, world=PriceWorld(seed=1), required_hours=2.0, ready_hour=7)
    options_b = {**FULL_OPTIONS, CONF_DEFAULT_REQUIRED_HOURS: 0.0, CONF_DEFAULT_READY_BY_HOUR: 5}
    entry_b = await async_setup_wholesale_entry(hass, options=options_b, name="Car B")
    coordinator_b = hass.data[DOMAIN][entry_b.entry_id]
    await coordinator_b.async_set_min_block_hours(0.5)
    await coordinator_b.async_set_max_price(100.0)
    await coordinator_b.async_set_required_hours(4.0)
    return a, entry_b, coordinator_b


async def test_two_instances_keep_separate_settings_schedules_and_deadlines(hass, freezer):
    a, entry_b, b = await _two_cars(hass, freezer)

    assert (a.coordinator.required_hours, b.required_hours) == (2.0, 4.0)
    assert a.coordinator.ready_by.hour == 7 and b.ready_by.hour == 5
    assert hass.states.get("number.wholesale_ev_schedule_charging_hours_required").state == "2.0"
    assert hass.states.get("number.car_b_charging_hours_required").state == "4.0"

    on_a = on_b = 0
    while a.now < _at(3, 8):
        t = await a.step(10)
        await b.async_refresh()
        on_a += 10 if t.desired else 0
        on_b += 10 if b.data["desired"] else 0
    assert on_a >= 120 and on_b >= 240  # each got what it asked for, independently


async def test_changing_one_instance_does_not_touch_the_other(hass, freezer):
    a, entry_b, b = await _two_cars(hass, freezer)
    await b.async_set_charge_override("force_on")
    await b.async_set_gamble_tolerance(5.0)
    a.setting_edits = True
    await a.step(5)

    assert a.coordinator.charge_override == "auto" and a.coordinator.gamble_tolerance == 50.0
    assert a.data["desired"] is False and b.data["desired"] is True


async def test_each_instance_stores_its_own_state_under_its_own_key(hass, freezer, hass_storage):
    a, entry_b, b = await _two_cars(hass, freezer)
    await b.async_set_max_price(77.0)

    assert _storage_key(a.entry) in hass_storage and _storage_key(entry_b) in hass_storage
    assert hass_storage[_storage_key(entry_b)]["data"]["max_price"] == 77.0
    assert hass_storage[_storage_key(a.entry)]["data"]["max_price"] == 100.0


async def test_restarting_one_instance_leaves_the_other_running_untouched(hass, freezer):
    a, entry_b, b = await _two_cars(hass, freezer)
    snapshot_b = (b.required_hours, b.ready_by, b.data["sessions"])

    await a.restart()

    assert hass.data[DOMAIN][entry_b.entry_id] is b  # not rebuilt
    assert (b.required_hours, b.ready_by, b.data["sessions"]) == snapshot_b


async def test_unloading_one_instance_leaves_the_other_fully_working(hass, freezer):
    a, entry_b, b = await _two_cars(hass, freezer)

    assert await hass.config_entries.async_unload(entry_b.entry_id)
    await hass.async_block_till_done()
    a.setting_edits = True
    await a.step(30)

    assert entry_b.entry_id not in hass.data[DOMAIN]
    assert a.data["state"] == "scheduled"
    assert hass.states.get("number.wholesale_ev_schedule_charging_hours_required").state == "2.0"


async def test_removing_an_instance_deletes_its_registry_entries_and_its_stored_state(hass, freezer, hass_storage):
    a, entry_b, b = await _two_cars(hass, freezer)
    registry = er.async_get(hass)
    assert er.async_entries_for_config_entry(registry, entry_b.entry_id)
    assert _storage_key(entry_b) in hass_storage

    await hass.config_entries.async_remove(entry_b.entry_id)
    await hass.async_block_till_done()

    assert not er.async_entries_for_config_entry(registry, entry_b.entry_id)
    assert _storage_key(entry_b) not in hass_storage, "the removed instance's state file was left behind"
    assert _storage_key(a.entry) in hass_storage  # the other car is untouched
    assert hass.states.get("number.car_b_charging_hours_required") is None


async def test_re_adding_an_instance_with_the_same_name_starts_from_a_clean_slate(hass, freezer):
    a, entry_b, b = await _two_cars(hass, freezer)
    await b.async_set_max_price(77.0)
    await b.async_set_required_hours(9.0)
    await hass.config_entries.async_remove(entry_b.entry_id)
    await hass.async_block_till_done()

    entry_again = await async_setup_wholesale_entry(
        hass, options={**FULL_OPTIONS, CONF_DEFAULT_REQUIRED_HOURS: 0.0}, name="Car B"
    )
    again = hass.data[DOMAIN][entry_again.entry_id]

    assert again.max_price == 20.0 and again.required_hours == 0.0


async def test_instances_have_independent_solve_caches(hass, freezer):
    a, entry_b, b = await _two_cars(hass, freezer)
    assert a.coordinator._solve_cache is not b._solve_cache
    await b.async_set_max_price(5.0)
    assert a.coordinator._solve_cache is not b._solve_cache


@pytest.mark.parametrize("names", [("Car A", "Car B", "Car C", "Car D")])
async def test_four_instances_coexist_without_entity_collisions(hass, freezer, names):
    await Sim.create(hass, freezer, start=START, world=PriceWorld(seed=1), required_hours=1.0)
    entries = [
        await async_setup_wholesale_entry(hass, options={**FULL_OPTIONS, CONF_DEFAULT_REQUIRED_HOURS: 1.0}, name=name)
        for name in names
    ]
    registry = er.async_get(hass)

    all_ids = [e.entity_id for entry in entries for e in er.async_entries_for_config_entry(registry, entry.entry_id)]

    assert (
        len(all_ids) == len(set(all_ids)) == 4 * len(er.async_entries_for_config_entry(registry, entries[0].entry_id))
    )
    for entry, name in zip(entries, names, strict=True):
        prefix = name.lower().replace(" ", "_")
        assert all(
            e.entity_id.split(".", 1)[1].startswith(prefix + "_")
            for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        )
        assert parse_dt(hass.states.get(f"sensor.{prefix}_next_slot_start").state) > datetime(
            2026, 3, 2, tzinfo=UTC
        ) + timedelta(hours=15)
