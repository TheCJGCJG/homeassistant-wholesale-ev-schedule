"""Smoke tests exercising the config flow and entity setup against a real
(test-harness) Home Assistant instance, via pytest-homeassistant-custom-component.
These catch import errors, schema mistakes, and setup/entity-registration bugs
that the pure scheduler.py unit tests can't see.
"""
from datetime import timedelta

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
import homeassistant.util.dt as dt_util

from custom_components.wholesale_ev_schedule.const import DEFAULT_NAME, DOMAIN

from .factories import (
    BASE_INPUT,
    EXPECTED_ENTITY_IDS,
    FORECAST_AGILE_PREDICT_INPUT,
    FULL_OPTIONS,
    RATES_OCTOPUS_INPUT,
    async_setup_wholesale_entry,
)


async def test_config_flow_walks_all_steps_and_creates_entry(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**BASE_INPUT, CONF_NAME: DEFAULT_NAME}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "rates_octopus_energy"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], RATES_OCTOPUS_INPUT)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "forecast_agile_predict"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], FORECAST_AGILE_PREDICT_INPUT)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["options"] == FULL_OPTIONS


async def test_options_flow_walks_all_steps_and_updates_entry(hass):
    entry = await async_setup_wholesale_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "init"

    changed_base = {**BASE_INPUT, "update_interval_minutes": 15}
    result = await hass.config_entries.options.async_configure(result["flow_id"], changed_base)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "rates_octopus_energy"

    result = await hass.config_entries.options.async_configure(result["flow_id"], RATES_OCTOPUS_INPUT)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "forecast_agile_predict"

    result = await hass.config_entries.options.async_configure(result["flow_id"], FORECAST_AGILE_PREDICT_INPUT)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert entry.options["update_interval_minutes"] == 15


async def test_setup_entry_registers_all_expected_entities(hass):
    entry = await async_setup_wholesale_entry(hass)

    registry = er.async_get(hass)
    for entity_id in EXPECTED_ENTITY_IDS:
        platform, object_id = entity_id.split(".", 1)
        unique_id_suffix = object_id.removeprefix("wholesale_ev_schedule_")
        registered_id = registry.async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{unique_id_suffix}")
        assert registered_id == entity_id, f"expected {entity_id}, registry has {registered_id}"


async def test_state_sensor_reports_error_on_fresh_setup_with_no_price_data(hass):
    # required_hours defaults to DEFAULT_REQUIRED_HOURS (not idle) and ready_by
    # defaults to the next 7am, so a fresh setup with no price entities
    # configured tries to schedule and finds nothing to work with.
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    state = hass.states.get("sensor.wholesale_ev_schedule_charging_state")
    assert state is not None
    assert state.state == "error"
    assert coordinator.required_hours == 12.0
    assert coordinator.ready_by is not None
    assert coordinator.ready_by.hour == 7


async def test_state_sensor_reports_idle_when_required_hours_explicitly_zeroed(hass):
    entry = await async_setup_wholesale_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await coordinator.async_set_required_hours(0.0)
    await hass.async_block_till_done()

    state = hass.states.get("sensor.wholesale_ev_schedule_charging_state")
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
