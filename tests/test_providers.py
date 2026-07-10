"""Config-flow coverage for the provider selection itself: choosing "custom"
for rates and/or forecast routes to the manual-fields steps, and choosing "no
forecast" skips the forecast step entirely — the branches providers.py and
config_flow.py's _ProviderStepsMixin exist to support.
"""

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import FlowResultType, InvalidData

from custom_components.wholesale_ev_schedule.const import (
    CONF_FORECAST_ATTRIBUTE,
    CONF_FORECAST_DATETIME_KEY,
    CONF_FORECAST_PRICE_KEY,
    CONF_FORECAST_UNIT_MULTIPLIER,
    CONF_RATE_START_KEY,
    CONF_RATE_UNIT_MULTIPLIER,
    CONF_RATE_VALUE_KEY,
    CONF_RATES_ATTRIBUTE,
    DEFAULT_NAME,
    DOMAIN,
)
from custom_components.wholesale_ev_schedule.providers import (
    FORECAST_PROVIDER_CUSTOM,
    FORECAST_PROVIDER_NONE,
    RATE_PROVIDER_CUSTOM,
)

from .factories import BASE_INPUT, FORECAST_AGILE_PREDICT_INPUT, RATES_OCTOPUS_INPUT


async def test_custom_rates_provider_routes_to_manual_fields_step(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**BASE_INPUT, CONF_NAME: DEFAULT_NAME, "rates_provider": RATE_PROVIDER_CUSTOM}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "rates_custom"

    custom_rates_input = {
        **RATES_OCTOPUS_INPUT,
        CONF_RATES_ATTRIBUTE: "forecasts",
        CONF_RATE_START_KEY: "from",
        CONF_RATE_VALUE_KEY: "per_kwh",
        CONF_RATE_UNIT_MULTIPLIER: 1.0,
    }
    result = await hass.config_entries.flow.async_configure(result["flow_id"], custom_rates_input)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "forecast_agile_predict"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], FORECAST_AGILE_PREDICT_INPUT)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_RATES_ATTRIBUTE] == "forecasts"
    assert result["options"][CONF_RATE_START_KEY] == "from"
    assert result["options"][CONF_RATE_VALUE_KEY] == "per_kwh"
    assert result["options"][CONF_RATE_UNIT_MULTIPLIER] == 1.0


async def test_custom_forecast_provider_routes_to_manual_fields_step(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**BASE_INPUT, CONF_NAME: DEFAULT_NAME, "forecast_provider": FORECAST_PROVIDER_CUSTOM}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], RATES_OCTOPUS_INPUT)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "forecast_custom"

    custom_forecast_input = {
        "forecast_entity": "sensor.my_custom_forecast",
        CONF_FORECAST_ATTRIBUTE: "predictions",
        CONF_FORECAST_DATETIME_KEY: "ts",
        CONF_FORECAST_PRICE_KEY: "price",
        CONF_FORECAST_UNIT_MULTIPLIER: 2.0,
    }
    result = await hass.config_entries.flow.async_configure(result["flow_id"], custom_forecast_input)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_FORECAST_ATTRIBUTE] == "predictions"
    assert result["options"][CONF_FORECAST_UNIT_MULTIPLIER] == 2.0


async def test_no_forecast_provider_skips_forecast_step_entirely(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**BASE_INPUT, CONF_NAME: DEFAULT_NAME, "forecast_provider": FORECAST_PROVIDER_NONE}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], RATES_OCTOPUS_INPUT)
    assert result["type"] == FlowResultType.CREATE_ENTRY  # forecast step skipped entirely
    assert result["options"]["forecast_entity"] is None


async def test_blank_rates_attribute_is_rejected_by_schema(hass):
    # Regression for issue #29. A blank/whitespace-only custom attribute name
    # was previously accepted silently, producing a misleading generic "no
    # price data" error at coordinator runtime instead of a validation
    # failure here at setup time. HA's flow manager validates a step's
    # submission against its own data_schema and raises InvalidData (with
    # schema_errors keyed by field) when it fails -- the same mechanism that
    # already correctly rejects an out-of-range NumberSelector value.
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**BASE_INPUT, CONF_NAME: DEFAULT_NAME, "rates_provider": RATE_PROVIDER_CUSTOM}
    )

    custom_rates_input = {
        **RATES_OCTOPUS_INPUT,
        CONF_RATES_ATTRIBUTE: "   ",
        CONF_RATE_START_KEY: "from",
        CONF_RATE_VALUE_KEY: "per_kwh",
        CONF_RATE_UNIT_MULTIPLIER: 1.0,
    }
    with pytest.raises(InvalidData) as exc_info:
        await hass.config_entries.flow.async_configure(result["flow_id"], custom_rates_input)

    assert CONF_RATES_ATTRIBUTE in exc_info.value.schema_errors


async def test_blank_forecast_datetime_key_is_rejected_by_schema(hass):
    # Regression for issue #29, forecast side.
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**BASE_INPUT, CONF_NAME: DEFAULT_NAME, "forecast_provider": FORECAST_PROVIDER_CUSTOM}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], RATES_OCTOPUS_INPUT)

    custom_forecast_input = {
        "forecast_entity": "sensor.my_custom_forecast",
        CONF_FORECAST_ATTRIBUTE: "predictions",
        CONF_FORECAST_DATETIME_KEY: "",
        CONF_FORECAST_PRICE_KEY: "price",
        CONF_FORECAST_UNIT_MULTIPLIER: 2.0,
    }
    with pytest.raises(InvalidData) as exc_info:
        await hass.config_entries.flow.async_configure(result["flow_id"], custom_forecast_input)

    assert CONF_FORECAST_DATETIME_KEY in exc_info.value.schema_errors
