"""Every supported combination of price sources, created through the real config flow and then run:
rates (Octopus / two custom layouts) x long-range forecast (none / AgilePredict / custom) x
short-range forecast (none / Agile Rates API / AgilePredict / custom with an estimate flag) x
opportunistic charging (off / on). Each combination is fed prices laid out the way *that*
configuration expects (attribute names, units, nested keys, flags), so a mismatch between what the
flow stores and what the coordinator reads shows up as a missed charge."""

import itertools
from datetime import datetime

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import FlowResultType

from custom_components.wholesale_ev_schedule.const import (
    CONF_DEFAULT_REQUIRED_HOURS,
    CONF_ENABLE_OPPORTUNISTIC_CHARGING,
    CONF_FORECAST_PROVIDER,
    CONF_RATES_PROVIDER,
    CONF_SHORT_RANGE_PROVIDER,
    DEFAULT_NAME,
    DOMAIN,
)

from .factories import BASE_INPUT, CURRENT_RATES_ENTITY, FORECAST_ENTITY, NEXT_RATES_ENTITY, SHORT_RANGE_ENTITY
from .sim import AGILE_PREDICT_SHAPE, AGILE_RATES_SHAPE, OCTOPUS_SHAPE, UTC, PriceWorld, Shape, Sim

# provider id -> (flow input for its step(s), the data layout it expects)
RATES = {
    "octopus": ("octopus_energy", {}, OCTOPUS_SHAPE),
    "custom_flat": (
        "custom",
        {
            "rates_attribute": "periods",
            "rate_start_key": "from",
            "rate_value_key": "gbp",
            "rate_unit_multiplier": 100.0,
        },
        Shape("periods", "from", "gbp", scale=0.01),
    ),
    "custom_nested_pence": (
        "custom",
        {
            "rates_attribute": "data",
            "rate_start_key": "t.start",
            "rate_value_key": "price.inc_vat",
            "rate_unit_multiplier": 1.0,
        },
        Shape("data", "t.start", "price.inc_vat", scale=1.0),
    ),
}
FORECASTS = {
    "none": ("none", {}, None),
    "agile_predict": ("agile_predict", {}, AGILE_PREDICT_SHAPE),
    "custom_gbp": (
        "custom",
        {
            "forecast_attribute": "pred",
            "forecast_datetime_key": "ts",
            "forecast_price_key": "p",
            "forecast_unit_multiplier": 100.0,
        },
        Shape("pred", "ts", "p", scale=0.01),
    ),
}
SHORT_RANGES = {
    "none": ("none", {}, None),
    "agile_rates_api": ("agile_rates_api", {}, AGILE_RATES_SHAPE),
    "agile_predict": ("agile_predict", {}, Shape("prices", "date_time", "agile_pred")),
    "custom_flagged": (
        "custom",
        {
            "short_range_attribute": "est",
            "short_range_datetime_key": "t",
            "short_range_price_key": "val.p",
            "short_range_unit_multiplier": 1.0,
            "short_range_estimate_flag_key": "meta.is_estimate",
        },
        Shape("est", "t", "val.p", flag_key="meta.is_estimate"),
    ),
}


async def _options_from_the_real_flow(hass, rates, forecast, short_range, opportunistic):
    """Walk the actual config flow for this combination and return the options it stores. (The
    entry the flow creates is removed again; the simulator sets up its own with those options.)"""
    rates_provider, rates_extra, _ = RATES[rates]
    forecast_provider, forecast_extra, _ = FORECASTS[forecast]
    short_provider, short_extra, _ = SHORT_RANGES[short_range]
    inputs = {
        "rates_octopus_energy": {"current_rates_entity": CURRENT_RATES_ENTITY, "next_rates_entity": NEXT_RATES_ENTITY},
        "rates_custom": {
            "current_rates_entity": CURRENT_RATES_ENTITY,
            "next_rates_entity": NEXT_RATES_ENTITY,
            **rates_extra,
        },
        "forecast_agile_predict": {"forecast_entity": FORECAST_ENTITY},
        "forecast_custom": {"forecast_entity": FORECAST_ENTITY, **forecast_extra},
        "short_range_agile_rates_api": {"short_range_entity": SHORT_RANGE_ENTITY},
        "short_range_agile_predict": {"short_range_entity": SHORT_RANGE_ENTITY},
        "short_range_custom": {"short_range_entity": SHORT_RANGE_ENTITY, **short_extra},
    }
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            **BASE_INPUT,
            CONF_NAME: DEFAULT_NAME,
            CONF_RATES_PROVIDER: rates_provider,
            CONF_FORECAST_PROVIDER: forecast_provider,
            CONF_SHORT_RANGE_PROVIDER: short_provider,
            CONF_ENABLE_OPPORTUNISTIC_CHARGING: opportunistic,
            CONF_DEFAULT_REQUIRED_HOURS: 0.0,
        },
    )
    steps = []
    while result["type"] == FlowResultType.FORM:
        steps.append(result["step_id"])
        result = await hass.config_entries.flow.async_configure(result["flow_id"], inputs[result["step_id"]])
    assert result["type"] == FlowResultType.CREATE_ENTRY, result
    options = dict(result["options"])
    await hass.config_entries.async_remove(result["result"].entry_id)
    await hass.async_block_till_done()
    return options, steps


COMBINATIONS = list(itertools.product(RATES, FORECASTS, SHORT_RANGES, [False, True]))


def _id(combo):
    rates, forecast, short, opportunistic = combo
    return f"{rates}-{forecast}-{short}-{'opp' if opportunistic else 'plain'}"


@pytest.mark.parametrize("combo", COMBINATIONS, ids=[_id(c) for c in COMBINATIONS])
async def test_every_provider_combination_created_through_the_flow_delivers_the_requirement(hass, freezer, combo):
    rates, forecast, short_range, opportunistic = combo
    options, steps = await _options_from_the_real_flow(hass, *combo)

    expected_steps = [
        "rates_octopus_energy" if rates == "octopus" else "rates_custom",
        *(
            []
            if forecast == "none"
            else ["forecast_agile_predict" if forecast == "agile_predict" else "forecast_custom"]
        ),
        *(
            []
            if short_range == "none"
            else [f"short_range_{short_range}" if short_range != "custom_flagged" else "short_range_custom"]
        ),
    ]
    assert steps == expected_steps

    feeds = {"actual"}
    shapes = {"actual": RATES[rates][2]}
    if forecast != "none":
        feeds.add("forecast")
        shapes["forecast"] = FORECASTS[forecast][2]
    if short_range != "none":
        feeds.add("short_range")
        shapes["short_range"] = SHORT_RANGES[short_range][2]
    sim = await Sim.create(
        hass,
        freezer,
        start="2026-03-02 16:00:00+00:00",
        options=options,
        feeds=feeds,
        shapes=shapes,
        world=PriceWorld(seed=4),
        required_hours=2.0,
        step_minutes=30,
    )

    assert sim.data["state"] == "scheduled", sim.data["error_reason"]

    # Every configured source must actually be read -- a plan built from the others alone would
    # still look healthy, so check what the coordinator collected, not just the outcome.
    sources = {p["source"] for p in sim.coordinator._collect_prices()}
    assert {"current_actual", "next_actual"} <= sources
    assert ("predicted" in sources) == (forecast != "none")
    assert bool(sources & {"short_range", "short_range_final"}) == (short_range != "none")

    await sim.run_to(datetime(2026, 3, 3, 8, 0, tzinfo=UTC))
    assert sim.charged_minutes[datetime(2026, 3, 3, 7, 0, tzinfo=UTC).isoformat()] == 120


@pytest.mark.parametrize("interval", [1, 5, 15, 60])
async def test_any_poll_interval_gives_the_same_schedule(hass, freezer, interval):
    options, _ = await _options_from_the_real_flow(hass, "octopus", "agile_predict", "none", False)
    options["update_interval_minutes"] = interval
    sim = await Sim.create(
        hass,
        freezer,
        start="2026-03-02 16:00:00+00:00",
        options=options,
        world=PriceWorld(seed=4),
        required_hours=2.0,
        step_minutes=30,
    )
    await sim.run_to(datetime(2026, 3, 3, 8, 0, tzinfo=UTC))

    assert sim.charged_minutes[datetime(2026, 3, 3, 7, 0, tzinfo=UTC).isoformat()] == 120
