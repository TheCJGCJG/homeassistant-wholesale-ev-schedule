"""An install upgrading from the previous release (1.2.0 and 1.3.0 share a storage shape): its config
entry and its stored-state file, exactly as that version wrote them, must load, carry on mid-charge
without a blip, and not need migration.

The stored payloads below are that shape verbatim (see its _async_save_stored_state): no
`short_blocks_used`, sessions without `estimated`, local-offset timestamps."""

from datetime import UTC, datetime, timedelta

import homeassistant.util.dt as dt_util
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_NAME
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wholesale_ev_schedule.const import DEFAULT_NAME, DOMAIN

from .factories import (
    CURRENT_RATES_ENTITY,
    EXPECTED_ENTITY_IDS,
    FULL_OPTIONS,
    NEXT_RATES_ENTITY,
    octopus_rate_points,
    set_octopus_rate_entity,
)

UTC = UTC
# What a 1.2.0 entry's options look like: the full flow output of that version, nothing short-range.
V1_2_0_OPTIONS = dict(FULL_OPTIONS)


async def _setup_v1_2_0_install(hass, hass_storage, freezer, stored, options=None):
    await hass.config.async_set_time_zone("Europe/London")
    freezer.move_to("2026-03-03 01:10:00+00:00")
    now = dt_util.now()
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, octopus_rate_points(now.replace(minute=0), 60, 0.10))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_NAME: DEFAULT_NAME},
        options=options or V1_2_0_OPTIONS,
        unique_id="wholesale_ev_schedule",
        title=DEFAULT_NAME,
    )
    entry.add_to_hass(hass)
    key = f"{DOMAIN}_{entry.entry_id}_state"
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": stored}
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, hass.data[DOMAIN][entry.entry_id], key


def _stored_mid_charge():
    """A 1.2.0 install an hour into a 3-hour block (00:00-03:00Z), with an opportunistic tier configured."""
    return {
        "ready_by": "2026-03-03T07:00:00+00:00",
        "required_hours": 3.0,
        "gamble_tolerance": 40.0,
        "min_block_hours": 2.0,
        "max_price": 35.0,
        "charge_override": "auto",
        "optimization_algorithm": "optimal",
        "assumed_charge_kwh": 11.0,
        "sessions": [
            {
                "start": "2026-03-03T00:00:00+00:00",
                "end": "2026-03-03T03:00:00+00:00",
                "duration_hours": 3.0,
                "avg_price": 9.5,
                "confidence": 100.0,
                "tier": "required",
            }
        ],
        "boost_end": None,
        "delivered_hours": 0.0,
        "opportunistic_enabled": False,
        "opportunistic_target_hours": 0.0,
        "opportunistic_max_price": 20.0,
        "opportunistic_ready_by": "2026-03-10T07:00:00+00:00",
        "delivered_opportunistic_hours": 0.0,
    }


async def test_a_1_2_0_install_resumes_mid_charge_without_a_blip(hass, hass_storage, freezer):
    stored = _stored_mid_charge()
    entry, c, _ = await _setup_v1_2_0_install(hass, hass_storage, freezer, stored)

    assert entry.state is ConfigEntryState.LOADED
    assert c.data["state"] == "charging" and c.data["desired"] is True
    assert c.data["sessions"] == stored["sessions"]  # the running block is untouched, byte for byte
    assert hass.states.get("binary_sensor.wholesale_ev_schedule_charging_desired").state == "on"
    # every setting it had is intact
    assert (c.required_hours, c.gamble_tolerance, c.min_block_hours, c.max_price) == (3.0, 40.0, 2.0, 35.0)
    assert c.optimization_algorithm == "optimal" and c.assumed_charge_kwh == 11.0
    assert c.ready_by == datetime(2026, 3, 3, 7, 0, tzinfo=UTC)


async def test_the_old_install_finishes_its_block_and_does_not_charge_again(hass, hass_storage, freezer):
    entry, c, _ = await _setup_v1_2_0_install(hass, hass_storage, freezer, _stored_mid_charge())
    on_minutes = 0
    for _ in range(12):  # 01:10Z -> 03:10Z in 10-minute steps
        freezer.tick(timedelta(minutes=10))
        await c.async_refresh()
        on_minutes += 10 if c.data["desired"] else 0

    assert 100 <= on_minutes <= 120  # the remaining ~1h50 of the block (sampled every 10 min), then it stops
    assert c.data["state"] == "complete" and c.data["desired"] is False
    assert c._delivered_hours == 3.0


async def test_no_entities_appear_disappear_or_get_renamed_on_upgrade(hass, hass_storage, freezer):
    entry, c, _ = await _setup_v1_2_0_install(hass, hass_storage, freezer, _stored_mid_charge())

    ids = {e.entity_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)}

    assert ids == EXPECTED_ENTITY_IDS


async def test_the_state_file_keeps_every_1_2_0_key_and_only_adds_to_it(hass, hass_storage, freezer):
    stored = _stored_mid_charge()
    entry, c, key = await _setup_v1_2_0_install(hass, hass_storage, freezer, dict(stored))
    await c.async_refresh()

    written = hass_storage[key]["data"]

    assert set(stored) <= set(written), "an old key was dropped from the state file"
    assert set(written) - set(stored) == {"short_blocks_used"}  # the one addition, defaulted
    assert written["short_blocks_used"] == 0
    assert written["required_hours"] == 3.0 and written["max_price"] == 35.0


async def test_a_1_2_0_install_with_opportunistic_charging_upgrades_cleanly(hass, hass_storage, freezer):
    stored = _stored_mid_charge()
    stored.update(opportunistic_enabled=True, opportunistic_target_hours=2.0, opportunistic_max_price=15.0)
    options = {**V1_2_0_OPTIONS, "enable_opportunistic_charging": True}

    entry, c, _ = await _setup_v1_2_0_install(hass, hass_storage, freezer, stored, options=options)

    assert c.opportunistic_enabled is True and c.opportunistic_target_hours == 2.0 and c.opportunistic_max_price == 15.0
    assert c.opportunistic_ready_by == datetime(2026, 3, 10, 7, 0, tzinfo=UTC)
    assert c.data["state"] in ("charging", "scheduled")


@pytest.mark.parametrize(
    "missing",
    [
        "opportunistic_enabled",
        "opportunistic_ready_by",
        "delivered_opportunistic_hours",
        "assumed_charge_kwh",
        "optimization_algorithm",
    ],
)
async def test_a_state_file_from_an_even_older_version_missing_newer_keys_still_loads(
    hass, hass_storage, freezer, missing
):
    stored = _stored_mid_charge()
    del stored[missing]

    entry, c, _ = await _setup_v1_2_0_install(hass, hass_storage, freezer, stored)

    assert entry.state is ConfigEntryState.LOADED and c.last_update_success
