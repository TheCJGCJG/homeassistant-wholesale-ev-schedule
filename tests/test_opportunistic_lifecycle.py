"""Opportunistic charging (issue #53) across the coordinator's other lifecycle
paths: Stop (cancel today, keep deadlines/preferences), Reset (full clean
slate, including opportunistic_ready_by re-deriving immediately rather than
being left unset until the next natural rollover -- a real gap the initial
implementation had and this file catches), and a genuine restart (unload +
re-setup, not just re-calling async_load_stored_state on the same object).
"""

from datetime import timedelta

import homeassistant.util.dt as dt_util

from custom_components.wholesale_ev_schedule.const import (
    CONF_DEFAULT_OPPORTUNISTIC_MAX_PRICE,
    CONF_DEFAULT_OPPORTUNISTIC_OFFSET_DAYS,
    CONF_DEFAULT_OPPORTUNISTIC_TARGET_HOURS,
    DOMAIN,
)

from .factories import (
    CURRENT_RATES_ENTITY,
    NEXT_RATES_ENTITY,
    OPPORTUNISTIC_OPTIONS,
    async_setup_wholesale_entry,
    octopus_rate_points,
    set_octopus_rate_entity,
)


async def _setup(hass):
    now = dt_util.now()
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, octopus_rate_points(now, 12, price_gbp_per_kwh=0.05))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])
    entry = await async_setup_wholesale_entry(hass, options=OPPORTUNISTIC_OPTIONS)
    return entry, hass.data[DOMAIN][entry.entry_id]


async def test_stop_zeroes_opportunistic_target_but_keeps_max_price_and_ready_by(hass):
    _, coordinator = await _setup(hass)
    await coordinator.async_set_opportunistic_target_hours(3.0)
    await coordinator.async_set_opportunistic_max_price(8.0)
    await coordinator.async_set_opportunistic_enabled(True)
    ready_by_before = coordinator.opportunistic_ready_by

    await coordinator.async_stop()

    # Symmetric with required_hours dropping to 0 on Stop.
    assert coordinator.opportunistic_target_hours == 0.0
    assert coordinator._delivered_opportunistic_hours == 0.0
    # Tuning preferences and the deadline are NOT tuning-day resets -- Stop
    # is "cancel today", not "forget my settings".
    assert coordinator.opportunistic_max_price == 8.0
    assert coordinator.opportunistic_enabled is True
    assert coordinator.opportunistic_ready_by == ready_by_before


async def test_reset_restores_opportunistic_defaults_and_rederives_ready_by_immediately(hass):
    _, coordinator = await _setup(hass)
    await coordinator.async_set_opportunistic_target_hours(3.0)
    await coordinator.async_set_opportunistic_max_price(8.0)
    await coordinator.async_set_opportunistic_enabled(True)

    await coordinator.async_reset()

    assert coordinator.opportunistic_target_hours == OPPORTUNISTIC_OPTIONS[CONF_DEFAULT_OPPORTUNISTIC_TARGET_HOURS]
    assert coordinator.opportunistic_max_price == OPPORTUNISTIC_OPTIONS[CONF_DEFAULT_OPPORTUNISTIC_MAX_PRICE]
    assert coordinator._delivered_opportunistic_hours == 0.0

    # opportunistic_ready_by must be immediately consistent with the reset
    # ready_by, not left None until ready_by happens to expire naturally --
    # async_reset sets ready_by directly to a fresh future value, so
    # _async_update_data's own rollover check (`ready_by <= now_dt`) never
    # fires to derive it on the next refresh; async_reset must derive it
    # itself. A None here would silently stop opportunistic scheduling
    # (_compute_opportunistic_sessions short-circuits on a falsy
    # opportunistic_ready_by) with no visible error.
    assert coordinator.opportunistic_ready_by is not None
    offset_days = OPPORTUNISTIC_OPTIONS[CONF_DEFAULT_OPPORTUNISTIC_OFFSET_DAYS]
    assert coordinator.opportunistic_ready_by == coordinator.ready_by + timedelta(days=offset_days)


async def test_reset_button_also_rederives_opportunistic_ready_by(hass):
    # Same as the coordinator-level test above, but via the actual button
    # entity a user/automation would press -- see test_boost_stop.py's
    # test_reset_button_restores_every_default for the equivalent
    # required-tier coverage this mirrors.
    _, coordinator = await _setup(hass)
    await coordinator.async_set_opportunistic_target_hours(3.0)
    await coordinator.async_set_opportunistic_enabled(True)

    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": "button.wholesale_ev_schedule_reset"},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert coordinator.opportunistic_ready_by is not None
    offset_days = OPPORTUNISTIC_OPTIONS[CONF_DEFAULT_OPPORTUNISTIC_OFFSET_DAYS]
    assert coordinator.opportunistic_ready_by == coordinator.ready_by + timedelta(days=offset_days)


async def test_opportunistic_state_survives_a_genuine_restart(hass):
    # Regression-style coverage mirroring
    # test_long_running_simulation.py::test_restart_from_scratch_rehydrates_full_state
    # -- a real unload + re-setup produces a brand-new coordinator instance
    # that must rehydrate opportunistic state entirely from the Store, not
    # just re-run async_load_stored_state() on the same object.
    entry, coordinator_before = await _setup(hass)
    await coordinator_before.async_set_opportunistic_target_hours(3.0)
    await coordinator_before.async_set_opportunistic_max_price(9.0)
    await coordinator_before.async_set_opportunistic_enabled(True)

    ready_by_before = coordinator_before.opportunistic_ready_by
    sessions_before = coordinator_before.data["sessions"]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator_after = hass.data[DOMAIN][entry.entry_id]
    assert coordinator_after is not coordinator_before

    assert coordinator_after.opportunistic_target_hours == 3.0
    assert coordinator_after.opportunistic_max_price == 9.0
    assert coordinator_after.opportunistic_enabled is True
    assert coordinator_after.opportunistic_ready_by == ready_by_before
    assert coordinator_after.data["sessions"] == sessions_before


async def test_pre_opportunistic_stored_sessions_default_to_required_tier_on_upgrade(hass):
    # Simulates upgrading from an install that predates opportunistic
    # charging: its stored sessions have no "tier" key at all (schema drift
    # from before SESSION_TIER_REQUIRED/SESSION_TIER_OPPORTUNISTIC existed).
    # _accrue_delivered_hours must default an untagged session to required,
    # not silently drop it or misroute it to the opportunistic counter.
    now = dt_util.now()
    set_octopus_rate_entity(hass, CURRENT_RATES_ENTITY, octopus_rate_points(now, 12, price_gbp_per_kwh=0.05))
    set_octopus_rate_entity(hass, NEXT_RATES_ENTITY, [])
    entry = await async_setup_wholesale_entry(hass, options=OPPORTUNISTIC_OPTIONS)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    untagged_session = {
        "start": now.isoformat(),
        "end": (now + timedelta(hours=1)).isoformat(),
        "duration_hours": 1.0,
        "avg_price": 5.0,
        "confidence": 100.0,
        # deliberately no "tier" key
    }
    coordinator._stored_sessions = [untagged_session]

    coordinator._accrue_delivered_hours(now + timedelta(hours=2))

    assert coordinator._delivered_hours == 1.0
    assert coordinator._delivered_opportunistic_hours == 0.0
