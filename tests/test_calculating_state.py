"""The state sensor reports "calculating" while a user edit is being recomputed, so
changing a setting isn't followed by silence until the new schedule lands.

Observed by patching `_collect_prices` -- the first thing `_async_update_data` does --
to snapshot the sensor mid-refresh, since by the time an awaited setter returns the
state has necessarily already settled again.
"""

from unittest.mock import patch

import pytest

from custom_components.wholesale_ev_schedule.const import DOMAIN

from .factories import async_setup_wholesale_entry

STATE_SENSOR = "sensor.wholesale_ev_schedule_charging_state"
DESIRED_SENSOR = "binary_sensor.wholesale_ev_schedule_charging_desired"


def _spy_during_refresh(hass, coordinator, seen):
    """Wrap _collect_prices so every call records (state, desired) as HA sees them."""
    original = coordinator._collect_prices

    def spy():
        seen.append((hass.states.get(STATE_SENSOR).state, hass.states.get(DESIRED_SENSOR).state))
        return original()

    return patch.object(coordinator, "_collect_prices", side_effect=spy)


async def test_state_is_calculating_while_a_setter_recomputes_then_settles(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    seen: list = []

    with _spy_during_refresh(hass, coordinator, seen):
        await coordinator.async_set_required_hours(2.0)
        await hass.async_block_till_done()

    assert seen and seen[0][0] == "calculating"
    assert coordinator.calculating is False
    assert hass.states.get(STATE_SENSOR).state != "calculating"


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.async_set_max_price(30.0),
        lambda c: c.async_set_min_block_hours(1.0),
        lambda c: c.async_set_gamble_tolerance(10.0),
        lambda c: c.async_set_optimization_algorithm("hybrid"),
        lambda c: c.async_set_charge_override("force_on"),
        lambda c: c.async_start_boost(1.0),
        lambda c: c.async_cancel_boost(),
        lambda c: c.async_stop(),
        lambda c: c.async_reset(),
    ],
)
async def test_every_user_edit_path_shows_calculating(hass, call):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    seen: list = []

    with _spy_during_refresh(hass, coordinator, seen):
        await call(coordinator)

    # Boost short-circuits before price work for the *state*, but _collect_prices
    # always runs first, so every path is observable at the same point.
    assert seen[0][0] == "calculating"
    assert hass.states.get(STATE_SENSOR).state != "calculating"


async def test_periodic_refresh_does_not_show_calculating(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    seen: list = []

    with _spy_during_refresh(hass, coordinator, seen):
        await coordinator.async_refresh()

    assert seen and all(state != "calculating" for state, _ in seen)


async def test_calculating_does_not_flip_charging_desired(hass):
    """The binary sensor is driven by the schedule + override, not the display
    state, so a recompute must not make a charger glitch off and back on."""
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_start_boost(2.0)
    assert hass.states.get(DESIRED_SENSOR).state == "on"
    seen: list = []

    with _spy_during_refresh(hass, coordinator, seen):
        await coordinator.async_set_max_price(30.0)

    assert seen[0] == ("calculating", "on")


async def test_calculating_is_cleared_even_if_the_refresh_fails(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    with patch.object(coordinator, "_collect_prices", side_effect=RuntimeError("boom")):
        await coordinator.async_set_max_price(30.0)
        await hass.async_block_till_done()

    assert coordinator.calculating is False
    assert hass.states.get(STATE_SENSOR).state != "calculating"


async def test_attributes_remain_available_while_calculating(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    attrs: list = []
    original = coordinator._collect_prices

    def spy():
        attrs.append(dict(hass.states.get(STATE_SENSOR).attributes))
        return original()

    with patch.object(coordinator, "_collect_prices", side_effect=spy):
        await coordinator.async_set_max_price(30.0)

    assert "error_reason" in attrs[0] and "boost_ends_at" in attrs[0]
