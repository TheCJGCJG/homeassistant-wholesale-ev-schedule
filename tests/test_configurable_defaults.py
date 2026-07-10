"""Setup-time "sensible defaults" (issue #2): CONF_DEFAULT_REQUIRED_HOURS /
CONF_DEFAULT_GAMBLE_TOLERANCE / CONF_DEFAULT_MAX_PRICE /
CONF_DEFAULT_MIN_BLOCK_HOURS / CONF_DEFAULT_READY_BY_HOUR /
CONF_DEFAULT_READY_BY_DAY_OFFSET config-flow options. These control what a
fresh install starts with and what the Reset button restores every
corresponding live value to -- never a live value directly. See the
coordinator.py class docstring.
"""
from datetime import timedelta

import homeassistant.util.dt as dt_util

from custom_components.wholesale_ev_schedule.const import (
    CONF_DEFAULT_GAMBLE_TOLERANCE,
    CONF_DEFAULT_MAX_PRICE,
    CONF_DEFAULT_MIN_BLOCK_HOURS,
    CONF_DEFAULT_READY_BY_DAY_OFFSET,
    CONF_DEFAULT_READY_BY_HOUR,
    CONF_DEFAULT_REQUIRED_HOURS,
    DEFAULT_GAMBLE_TOLERANCE,
    DEFAULT_MAX_PRICE,
    DEFAULT_MIN_BLOCK_HOURS,
    DEFAULT_READY_BY_HOUR,
    DEFAULT_REQUIRED_HOURS,
    DOMAIN,
)

from .factories import FULL_OPTIONS, async_setup_wholesale_entry

CUSTOM_DEFAULT_OPTIONS = {
    **FULL_OPTIONS,
    CONF_DEFAULT_REQUIRED_HOURS: 6.0,
    CONF_DEFAULT_GAMBLE_TOLERANCE: 80.0,
    CONF_DEFAULT_MAX_PRICE: 35.0,
    CONF_DEFAULT_MIN_BLOCK_HOURS: 1.5,
    CONF_DEFAULT_READY_BY_HOUR: 21,
    CONF_DEFAULT_READY_BY_DAY_OFFSET: 2,
}


async def test_fresh_install_uses_configured_custom_defaults(hass):
    entry = await async_setup_wholesale_entry(hass, options=CUSTOM_DEFAULT_OPTIONS)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    assert coordinator.required_hours == 6.0
    assert coordinator.gamble_tolerance == 80.0
    assert coordinator.max_price == 35.0
    assert coordinator.min_block_hours == 1.5
    assert coordinator.ready_by.hour == 21
    # min_day_offset=2 -- ready_by must land at least 2 days out from setup time.
    assert coordinator.ready_by >= dt_util.now().replace(hour=21, minute=0, second=0, microsecond=0) + timedelta(
        days=2
    )


async def test_fresh_install_falls_back_to_hardcoded_defaults_when_unconfigured(hass):
    # Regression check: FULL_OPTIONS has none of the new CONF_DEFAULT_* keys
    # set, so behaviour must be identical to before this feature existed.
    entry = await async_setup_wholesale_entry(hass)  # options=FULL_OPTIONS
    coordinator = hass.data[DOMAIN][entry.entry_id]

    assert coordinator.required_hours == DEFAULT_REQUIRED_HOURS
    assert coordinator.gamble_tolerance == DEFAULT_GAMBLE_TOLERANCE
    assert coordinator.max_price == DEFAULT_MAX_PRICE
    assert coordinator.min_block_hours == DEFAULT_MIN_BLOCK_HOURS
    assert coordinator.ready_by.hour == DEFAULT_READY_BY_HOUR


async def test_reset_restores_configured_custom_defaults_not_hardcoded_ones(hass):
    entry = await async_setup_wholesale_entry(hass, options=CUSTOM_DEFAULT_OPTIONS)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    # Push every live value away from both the custom defaults and the
    # hardcoded ones first.
    await coordinator.async_set_ready_by(dt_util.now() + timedelta(hours=6))
    await coordinator.async_set_required_hours(2.0)
    await coordinator.async_set_gamble_tolerance(10.0)
    await coordinator.async_set_min_block_hours(0.5)
    await coordinator.async_set_max_price(5.0)

    await coordinator.async_reset()

    assert coordinator.required_hours == 6.0
    assert coordinator.gamble_tolerance == 80.0
    assert coordinator.max_price == 35.0
    assert coordinator.min_block_hours == 1.5
    assert coordinator.ready_by.hour == 21
    assert coordinator.ready_by >= dt_util.now().replace(hour=21, minute=0, second=0, microsecond=0) + timedelta(
        days=2
    )
    # None of these should equal the old hardcoded constants (all deliberately
    # distinct in CUSTOM_DEFAULT_OPTIONS above).
    assert coordinator.required_hours != DEFAULT_REQUIRED_HOURS
    assert coordinator.gamble_tolerance != DEFAULT_GAMBLE_TOLERANCE
    assert coordinator.max_price != DEFAULT_MAX_PRICE
    assert coordinator.min_block_hours != DEFAULT_MIN_BLOCK_HOURS
    assert coordinator.ready_by.hour != DEFAULT_READY_BY_HOUR


async def test_reset_falls_back_to_hardcoded_defaults_when_unconfigured(hass):
    entry = await async_setup_wholesale_entry(hass)  # options=FULL_OPTIONS
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await coordinator.async_set_required_hours(2.0)
    await coordinator.async_set_gamble_tolerance(10.0)
    await coordinator.async_set_min_block_hours(0.5)
    await coordinator.async_set_max_price(5.0)

    await coordinator.async_reset()

    assert coordinator.required_hours == DEFAULT_REQUIRED_HOURS
    assert coordinator.gamble_tolerance == DEFAULT_GAMBLE_TOLERANCE
    assert coordinator.max_price == DEFAULT_MAX_PRICE
    assert coordinator.min_block_hours == DEFAULT_MIN_BLOCK_HOURS
    assert coordinator.ready_by.hour == DEFAULT_READY_BY_HOUR


async def test_changing_configured_defaults_via_options_takes_effect_after_reload(hass):
    # These options are only read at coordinator construction (like
    # update_interval_minutes already is), so changing them relies on the
    # existing full-reload update listener in __init__.py -- no new reload
    # wiring should be needed.
    entry = await async_setup_wholesale_entry(hass)  # options=FULL_OPTIONS
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator._default_required_hours == DEFAULT_REQUIRED_HOURS

    hass.config_entries.async_update_entry(entry, options=CUSTOM_DEFAULT_OPTIONS)
    await hass.async_block_till_done()

    reloaded_coordinator = hass.data[DOMAIN][entry.entry_id]
    assert reloaded_coordinator._default_required_hours == 6.0
    assert reloaded_coordinator._default_gamble_tolerance == 80.0
    assert reloaded_coordinator._default_max_price == 35.0
    assert reloaded_coordinator._default_min_block_hours == 1.5
    assert reloaded_coordinator._default_ready_by_hour == 21
    assert reloaded_coordinator._default_ready_by_day_offset == 2
