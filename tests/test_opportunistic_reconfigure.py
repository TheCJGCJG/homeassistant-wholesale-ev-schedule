"""Enabling opportunistic charging via reconfiguration -- an existing entry
set up with it off (the default) must pick up the new entities the moment
CONF_ENABLE_OPPORTUNISTIC_CHARGING flips to True, without re-adding the
integration from scratch. Two angles, mirroring the two idioms already used
elsewhere in this suite for option changes:

- test_options_flow_walks_all_steps_and_updates_entry (test_integration_smoke.py)
  walks the actual OptionsFlow UI steps for a different option
  (update_interval_minutes) -- this file does the same walk for the
  opportunistic toggle specifically.
- test_update_interval_option_takes_effect_after_reload
  (test_tolerances_and_reload.py) updates entry.options directly and checks
  the reload took effect -- this file's second test does the same for the
  opportunistic toggle, proving the reload mechanism itself (shared,
  pre-existing code) does pick up this particular option.
"""

from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from custom_components.wholesale_ev_schedule.const import CONF_ENABLE_OPPORTUNISTIC_CHARGING, DOMAIN

from .factories import (
    BASE_INPUT,
    EXPECTED_ENTITY_IDS,
    FORECAST_AGILE_PREDICT_INPUT,
    FULL_OPTIONS,
    OPPORTUNISTIC_OPTIONS,
    RATES_OCTOPUS_INPUT,
    async_setup_wholesale_entry,
    expected_opportunistic_entity_ids,
)


async def _registered_entity_ids(hass) -> set[str]:
    registry = er.async_get(hass)
    return {e.entity_id for e in registry.entities.values() if e.platform == DOMAIN}


async def test_enabling_opportunistic_via_options_flow_adds_the_new_entities(hass):
    # Set up with opportunistic off (the default) first -- this is the
    # existing-install scenario: nothing opportunistic exists yet.
    entry = await async_setup_wholesale_entry(hass)
    assert await _registered_entity_ids(hass) == EXPECTED_ENTITY_IDS

    # Walk the actual options flow, same shape as
    # test_options_flow_walks_all_steps_and_updates_entry, but flipping the
    # opportunistic toggle on in the submitted base-step input.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**BASE_INPUT, CONF_ENABLE_OPPORTUNISTIC_CHARGING: True}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "rates_octopus_energy"

    result = await hass.config_entries.options.async_configure(result["flow_id"], RATES_OCTOPUS_INPUT)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "forecast_agile_predict"

    result = await hass.config_entries.options.async_configure(result["flow_id"], FORECAST_AGILE_PREDICT_INPUT)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert entry.options[CONF_ENABLE_OPPORTUNISTIC_CHARGING] is True
    all_ids = await _registered_entity_ids(hass)
    assert all_ids == EXPECTED_ENTITY_IDS | expected_opportunistic_entity_ids()


async def test_disabling_opportunistic_via_options_flow_removes_the_new_entities(hass):
    # The reverse direction: an entry already configured with opportunistic
    # on, reconfigured to turn it back off, should end up back at exactly
    # the default entity set -- not leave orphaned opportunistic entities
    # behind.
    entry = await async_setup_wholesale_entry(hass, options=OPPORTUNISTIC_OPTIONS)
    assert await _registered_entity_ids(hass) == EXPECTED_ENTITY_IDS | expected_opportunistic_entity_ids()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**BASE_INPUT, CONF_ENABLE_OPPORTUNISTIC_CHARGING: False}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], RATES_OCTOPUS_INPUT)
    result = await hass.config_entries.options.async_configure(result["flow_id"], FORECAST_AGILE_PREDICT_INPUT)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert entry.options[CONF_ENABLE_OPPORTUNISTIC_CHARGING] is False
    assert await _registered_entity_ids(hass) == EXPECTED_ENTITY_IDS


async def test_enabling_opportunistic_via_direct_options_update_takes_effect_after_reload(hass):
    # Same idiom as test_update_interval_option_takes_effect_after_reload:
    # updates entry.options directly (bypassing the flow's form-walking) to
    # isolate and prove specifically that the reload mechanism itself --
    # already-existing, shared code -- does pick up this option, independent
    # of the flow UI tested above.
    entry = await async_setup_wholesale_entry(hass)
    assert await _registered_entity_ids(hass) == EXPECTED_ENTITY_IDS

    hass.config_entries.async_update_entry(entry, options={**FULL_OPTIONS, CONF_ENABLE_OPPORTUNISTIC_CHARGING: True})
    await hass.async_block_till_done()

    reloaded_coordinator = hass.data[DOMAIN][entry.entry_id]
    assert reloaded_coordinator.entry.options[CONF_ENABLE_OPPORTUNISTIC_CHARGING] is True
    assert await _registered_entity_ids(hass) == EXPECTED_ENTITY_IDS | expected_opportunistic_entity_ids()
