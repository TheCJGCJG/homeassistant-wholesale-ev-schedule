"""Running multiple instances side by side (e.g. one per car): distinct names
produce distinct, non-colliding entity_id prefixes, and the config flow refuses
to create two instances that would slugify to the same prefix.
"""
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from custom_components.wholesale_ev_schedule.const import DOMAIN

from .factories import (
    BASE_INPUT,
    FORECAST_AGILE_PREDICT_INPUT,
    RATES_OCTOPUS_INPUT,
    async_setup_wholesale_entry,
    expected_entity_ids,
)


async def test_two_instances_with_distinct_names_do_not_collide(hass):
    tesla_entry = await async_setup_wholesale_entry(hass, name="Tesla EV Schedule")
    polestar_entry = await async_setup_wholesale_entry(hass, name="Polestar EV Schedule")

    registry = er.async_get(hass)
    tesla_ids = {e.entity_id for e in registry.entities.values() if e.config_entry_id == tesla_entry.entry_id}
    polestar_ids = {e.entity_id for e in registry.entities.values() if e.config_entry_id == polestar_entry.entry_id}

    assert tesla_ids == expected_entity_ids("tesla_ev_schedule")
    assert polestar_ids == expected_entity_ids("polestar_ev_schedule")
    assert not (tesla_ids & polestar_ids)


async def test_config_flow_aborts_on_duplicate_name(hass):
    await async_setup_wholesale_entry(hass, name="Tesla EV Schedule")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**BASE_INPUT, CONF_NAME: "Tesla EV Schedule"}
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_config_flow_aborts_on_duplicate_slug_different_casing(hass):
    # "Tesla EV Schedule" and "tesla ev schedule" slugify to the same prefix.
    await async_setup_wholesale_entry(hass, name="Tesla EV Schedule")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**BASE_INPUT, CONF_NAME: "tesla ev schedule"}
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_config_flow_end_to_end_sets_prefix_from_name(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**BASE_INPUT, CONF_NAME: "Garage EV Schedule"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "rates_octopus_energy"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], RATES_OCTOPUS_INPUT)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "forecast_agile_predict"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], FORECAST_AGILE_PREDICT_INPUT)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Garage EV Schedule"
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{result['result'].entry_id}_charging_state"
    )
    assert entity_id == "sensor.garage_ev_schedule_charging_state"
