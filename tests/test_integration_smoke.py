"""Smoke tests exercising the config flow and entity setup against a real
(test-harness) Home Assistant instance, via pytest-homeassistant-custom-component.
These catch import errors, schema mistakes, and setup/entity-registration bugs
that the pure scheduler.py unit tests can't see.
"""
from datetime import timedelta

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
import homeassistant.util.dt as dt_util

from custom_components.wholesale_ev_schedule.const import DOMAIN

from .factories import (
    ADVANCED_OPTIONS,
    ESSENTIAL_OPTIONS,
    EXPECTED_ENTITY_IDS,
    FULL_OPTIONS,
    async_setup_wholesale_entry,
)


async def test_config_flow_two_steps_creates_entry(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], ESSENTIAL_OPTIONS)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "advanced"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], ADVANCED_OPTIONS)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["options"] == FULL_OPTIONS


async def test_options_flow_two_steps_updates_entry(hass):
    entry = await async_setup_wholesale_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(result["flow_id"], ESSENTIAL_OPTIONS)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "advanced"

    changed_advanced = {**ADVANCED_OPTIONS, "max_price": 15.0}
    result = await hass.config_entries.options.async_configure(result["flow_id"], changed_advanced)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert entry.options["max_price"] == 15.0


async def test_setup_entry_registers_all_expected_entities(hass):
    entry = await async_setup_wholesale_entry(hass)

    registry = er.async_get(hass)
    for entity_id in EXPECTED_ENTITY_IDS:
        platform, object_id = entity_id.split(".", 1)
        unique_id_suffix = object_id.removeprefix("wholesale_ev_schedule_")
        registered_id = registry.async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{unique_id_suffix}")
        assert registered_id == entity_id, f"expected {entity_id}, registry has {registered_id}"


async def test_state_sensor_reports_idle_with_no_inputs_set(hass):
    await async_setup_wholesale_entry(hass)
    state = hass.states.get("sensor.wholesale_ev_schedule_charging_state")
    assert state is not None
    assert state.state == "idle"


async def test_setting_required_hours_and_ready_by_with_no_price_data_reports_error(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await coordinator.async_set_ready_by(dt_util.now() + timedelta(hours=6))
    await coordinator.async_set_required_hours(2.0)
    await hass.async_block_till_done()

    # No price entities exist in this test hass instance, so scheduling has no data
    # to work with — the important thing is a clean error, not a crash.
    assert coordinator.data["state"] == "error"
    assert coordinator.data["error_reason"]
