"""Config/options-flow coverage for the optional short-range forecast (issue #54), plus
the guarantees that matter most for an existing install: opening and saving the options
flow without touching the new field changes nothing else, and every new screen/field has
its strings (a missing label silently renders as a raw key).
"""

import json
from pathlib import Path

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import FlowResultType, InvalidData

from custom_components.wholesale_ev_schedule import config_flow
from custom_components.wholesale_ev_schedule.const import (
    CONF_DEFAULT_GAMBLE_TOLERANCE,
    CONF_DEFAULT_MAX_PRICE,
    CONF_DEFAULT_MIN_BLOCK_HOURS,
    CONF_DEFAULT_READY_BY_DAY_OFFSET,
    CONF_DEFAULT_READY_BY_HOUR,
    CONF_DEFAULT_REQUIRED_HOURS,
    CONF_ENABLE_OPPORTUNISTIC_CHARGING,
    CONF_FORECAST_ENTITY,
    CONF_FORECAST_PROVIDER,
    CONF_SHORT_RANGE_ATTRIBUTE,
    CONF_SHORT_RANGE_CONFIDENCE,
    CONF_SHORT_RANGE_DATETIME_KEY,
    CONF_SHORT_RANGE_ENTITY,
    CONF_SHORT_RANGE_ESTIMATE_FLAG_KEY,
    CONF_SHORT_RANGE_PRICE_KEY,
    CONF_SHORT_RANGE_PROVIDER,
    CONF_SHORT_RANGE_UNIT_MULTIPLIER,
    DEFAULT_NAME,
    DEFAULT_SHORT_RANGE_CONFIDENCE,
    DOMAIN,
)
from custom_components.wholesale_ev_schedule.providers import (
    FORECAST_PROVIDER_CUSTOM,
    FORECAST_PROVIDER_NONE,
    SHORT_RANGE_PROVIDER_AGILE_PREDICT,
    SHORT_RANGE_PROVIDER_AGILE_RATES_API,
    SHORT_RANGE_PROVIDER_CUSTOM,
    SHORT_RANGE_PROVIDER_NONE,
    SHORT_RANGE_PROVIDERS,
)

from .factories import (
    BASE_INPUT,
    EXPECTED_ENTITY_IDS,
    FORECAST_AGILE_PREDICT_INPUT,
    FULL_OPTIONS,
    RATES_OCTOPUS_INPUT,
    SHORT_RANGE_ENTITY,
    async_setup_wholesale_entry,
    short_range_options,
)

COMPONENT_DIR = Path(config_flow.__file__).parent

_CUSTOM_INPUT = {
    CONF_SHORT_RANGE_ENTITY: SHORT_RANGE_ENTITY,
    CONF_SHORT_RANGE_ATTRIBUTE: "forecast",
    CONF_SHORT_RANGE_DATETIME_KEY: "from",
    CONF_SHORT_RANGE_PRICE_KEY: "pence",
    CONF_SHORT_RANGE_UNIT_MULTIPLIER: 1.0,
    CONF_SHORT_RANGE_ESTIMATE_FLAG_KEY: "is_estimate",
    CONF_SHORT_RANGE_CONFIDENCE: 80,
}


async def _to_short_range_step(hass, provider, *, flow=None, **base_overrides):
    """Walk the user flow through rates and forecast, returning the result at the point the
    short-range step (or the end) is reached."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**BASE_INPUT, CONF_NAME: DEFAULT_NAME, CONF_SHORT_RANGE_PROVIDER: provider, **base_overrides},
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], RATES_OCTOPUS_INPUT)
    if result["step_id"] == "forecast_agile_predict":
        result = await hass.config_entries.flow.async_configure(result["flow_id"], FORECAST_AGILE_PREDICT_INPUT)
    return result


def _default_of(result, field):
    marker = next(k for k in result["data_schema"].schema if k.schema == field)
    return marker.default() if callable(marker.default) else marker.default


# ---------------------------------------------------------------------------
# Not opted in
# ---------------------------------------------------------------------------


async def test_default_flow_has_no_extra_step_and_records_short_range_as_off(hass):
    result = await _to_short_range_step(hass, SHORT_RANGE_PROVIDER_NONE)

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_SHORT_RANGE_PROVIDER] == SHORT_RANGE_PROVIDER_NONE
    assert result["options"][CONF_SHORT_RANGE_ENTITY] is None


async def test_a_flow_that_never_mentions_short_range_defaults_to_off(hass):
    """Schemas apply defaults to omitted Required keys, so every pre-existing caller of
    the flow (BASE_INPUT has no short-range key) is unaffected."""
    assert CONF_SHORT_RANGE_PROVIDER not in BASE_INPUT

    result = await _to_short_range_step(hass, SHORT_RANGE_PROVIDER_NONE)
    options = result["options"]

    assert {k: options[k] for k in FULL_OPTIONS} == FULL_OPTIONS


async def test_the_user_form_offers_none_first_and_defaults_to_it(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})

    assert _default_of(result, CONF_SHORT_RANGE_PROVIDER) == SHORT_RANGE_PROVIDER_NONE
    assert list(SHORT_RANGE_PROVIDERS)[0] == SHORT_RANGE_PROVIDER_NONE


# ---------------------------------------------------------------------------
# Named providers
# ---------------------------------------------------------------------------


async def test_agile_rates_api_provider_adds_one_step_and_fills_in_the_key_shape(hass):
    result = await _to_short_range_step(hass, SHORT_RANGE_PROVIDER_AGILE_RATES_API)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "short_range_agile_rates_api"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SHORT_RANGE_ENTITY: SHORT_RANGE_ENTITY, CONF_SHORT_RANGE_CONFIDENCE: 90}
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    options = result["options"]
    assert options[CONF_SHORT_RANGE_PROVIDER] == SHORT_RANGE_PROVIDER_AGILE_RATES_API
    assert options[CONF_SHORT_RANGE_ENTITY] == SHORT_RANGE_ENTITY
    assert options[CONF_SHORT_RANGE_CONFIDENCE] == 90
    assert options[CONF_SHORT_RANGE_ATTRIBUTE] == "rates"
    assert options[CONF_SHORT_RANGE_DATETIME_KEY] == "deliveryStart"
    assert options[CONF_SHORT_RANGE_PRICE_KEY] == "agileRate.result.rate"
    assert options[CONF_SHORT_RANGE_ESTIMATE_FLAG_KEY] == "agileRate.result.prediction"
    assert options[CONF_SHORT_RANGE_UNIT_MULTIPLIER] == 1.0


async def test_confidence_defaults_when_omitted(hass):
    result = await _to_short_range_step(hass, SHORT_RANGE_PROVIDER_AGILE_RATES_API)
    assert _default_of(result, CONF_SHORT_RANGE_CONFIDENCE) == DEFAULT_SHORT_RANGE_CONFIDENCE

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SHORT_RANGE_ENTITY: SHORT_RANGE_ENTITY}
    )

    assert result["options"][CONF_SHORT_RANGE_CONFIDENCE] == DEFAULT_SHORT_RANGE_CONFIDENCE


async def test_agile_predict_provider_has_no_estimate_flag(hass):
    result = await _to_short_range_step(hass, SHORT_RANGE_PROVIDER_AGILE_PREDICT)
    assert result["step_id"] == "short_range_agile_predict"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SHORT_RANGE_ENTITY: SHORT_RANGE_ENTITY}
    )

    options = result["options"]
    assert options[CONF_SHORT_RANGE_ATTRIBUTE] == "prices"
    assert options[CONF_SHORT_RANGE_DATETIME_KEY] == "date_time"
    assert options[CONF_SHORT_RANGE_PRICE_KEY] == "agile_pred"
    assert options[CONF_SHORT_RANGE_ESTIMATE_FLAG_KEY] is None


@pytest.mark.parametrize("confidence", [-1, 100.5, 101, 1000])
async def test_out_of_range_confidence_is_rejected(hass, confidence):
    result = await _to_short_range_step(hass, SHORT_RANGE_PROVIDER_AGILE_RATES_API)

    with pytest.raises(InvalidData) as exc_info:
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_SHORT_RANGE_ENTITY: SHORT_RANGE_ENTITY, CONF_SHORT_RANGE_CONFIDENCE: confidence}
        )

    assert CONF_SHORT_RANGE_CONFIDENCE in exc_info.value.schema_errors


@pytest.mark.parametrize("confidence", [0, 1, 50, 99, 100])
async def test_in_range_confidence_is_accepted(hass, confidence):
    result = await _to_short_range_step(hass, SHORT_RANGE_PROVIDER_AGILE_RATES_API)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SHORT_RANGE_ENTITY: SHORT_RANGE_ENTITY, CONF_SHORT_RANGE_CONFIDENCE: confidence}
    )

    assert result["options"][CONF_SHORT_RANGE_CONFIDENCE] == confidence


async def test_the_entity_is_required(hass):
    result = await _to_short_range_step(hass, SHORT_RANGE_PROVIDER_AGILE_RATES_API)

    with pytest.raises(InvalidData) as exc_info:
        await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert CONF_SHORT_RANGE_ENTITY in exc_info.value.schema_errors


# ---------------------------------------------------------------------------
# Custom provider
# ---------------------------------------------------------------------------


async def test_custom_provider_stores_exactly_what_was_entered(hass):
    result = await _to_short_range_step(hass, SHORT_RANGE_PROVIDER_CUSTOM)
    assert result["step_id"] == "short_range_custom"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], _CUSTOM_INPUT)

    options = result["options"]
    for key, value in _CUSTOM_INPUT.items():
        assert options[key] == value
    assert options[CONF_SHORT_RANGE_PROVIDER] == SHORT_RANGE_PROVIDER_CUSTOM


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
async def test_custom_estimate_flag_key_is_optional_and_blank_means_none(hass, blank):
    result = await _to_short_range_step(hass, SHORT_RANGE_PROVIDER_CUSTOM)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**_CUSTOM_INPUT, CONF_SHORT_RANGE_ESTIMATE_FLAG_KEY: blank}
    )

    assert result["options"][CONF_SHORT_RANGE_ESTIMATE_FLAG_KEY] is None


async def test_custom_estimate_flag_key_may_be_omitted_entirely(hass):
    result = await _to_short_range_step(hass, SHORT_RANGE_PROVIDER_CUSTOM)
    without_flag = {k: v for k, v in _CUSTOM_INPUT.items() if k != CONF_SHORT_RANGE_ESTIMATE_FLAG_KEY}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], without_flag)

    assert result["options"][CONF_SHORT_RANGE_ESTIMATE_FLAG_KEY] is None


async def test_custom_estimate_flag_key_is_trimmed(hass):
    result = await _to_short_range_step(hass, SHORT_RANGE_PROVIDER_CUSTOM)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**_CUSTOM_INPUT, CONF_SHORT_RANGE_ESTIMATE_FLAG_KEY: "  a.b.prediction  "}
    )

    assert result["options"][CONF_SHORT_RANGE_ESTIMATE_FLAG_KEY] == "a.b.prediction"


@pytest.mark.parametrize(
    "field", [CONF_SHORT_RANGE_ATTRIBUTE, CONF_SHORT_RANGE_DATETIME_KEY, CONF_SHORT_RANGE_PRICE_KEY]
)
@pytest.mark.parametrize("blank", ["", "   "])
async def test_custom_required_key_names_reject_blanks(hass, field, blank):
    """Same protection as the existing custom steps (issue #29): a blank key would
    otherwise surface later as a misleading 'no price data' error."""
    result = await _to_short_range_step(hass, SHORT_RANGE_PROVIDER_CUSTOM)

    with pytest.raises(InvalidData) as exc_info:
        await hass.config_entries.flow.async_configure(result["flow_id"], {**_CUSTOM_INPUT, field: blank})

    assert field in exc_info.value.schema_errors


@pytest.mark.parametrize("multiplier", [0, 0.001, -1, 1001])
async def test_custom_multiplier_bounds(hass, multiplier):
    result = await _to_short_range_step(hass, SHORT_RANGE_PROVIDER_CUSTOM)

    with pytest.raises(InvalidData) as exc_info:
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {**_CUSTOM_INPUT, CONF_SHORT_RANGE_UNIT_MULTIPLIER: multiplier}
        )

    assert CONF_SHORT_RANGE_UNIT_MULTIPLIER in exc_info.value.schema_errors


async def test_dotted_key_paths_are_accepted_in_the_custom_step(hass):
    result = await _to_short_range_step(hass, SHORT_RANGE_PROVIDER_CUSTOM)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**_CUSTOM_INPUT, CONF_SHORT_RANGE_PRICE_KEY: "agileRate.result.rate"}
    )

    assert result["options"][CONF_SHORT_RANGE_PRICE_KEY] == "agileRate.result.rate"


# ---------------------------------------------------------------------------
# Routing with the other providers
# ---------------------------------------------------------------------------


async def test_short_range_step_comes_after_the_forecast_step(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**BASE_INPUT, CONF_NAME: DEFAULT_NAME, CONF_SHORT_RANGE_PROVIDER: SHORT_RANGE_PROVIDER_AGILE_RATES_API},
    )
    assert result["step_id"] == "rates_octopus_energy"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], RATES_OCTOPUS_INPUT)
    assert result["step_id"] == "forecast_agile_predict"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], FORECAST_AGILE_PREDICT_INPUT)
    assert result["step_id"] == "short_range_agile_rates_api"


async def test_no_long_range_forecast_still_reaches_the_short_range_step(hass):
    result = await _to_short_range_step(
        hass, SHORT_RANGE_PROVIDER_AGILE_RATES_API, **{CONF_FORECAST_PROVIDER: FORECAST_PROVIDER_NONE}
    )

    assert result["step_id"] == "short_range_agile_rates_api"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SHORT_RANGE_ENTITY: SHORT_RANGE_ENTITY}
    )
    assert result["options"][CONF_FORECAST_ENTITY] is None
    assert result["options"][CONF_SHORT_RANGE_ENTITY] == SHORT_RANGE_ENTITY


async def test_custom_long_range_forecast_then_custom_short_range(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            **BASE_INPUT,
            CONF_NAME: DEFAULT_NAME,
            CONF_FORECAST_PROVIDER: FORECAST_PROVIDER_CUSTOM,
            CONF_SHORT_RANGE_PROVIDER: SHORT_RANGE_PROVIDER_CUSTOM,
        },
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], RATES_OCTOPUS_INPUT)
    assert result["step_id"] == "forecast_custom"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "forecast_entity": "sensor.my_forecast",
            "forecast_attribute": "prices",
            "forecast_datetime_key": "date_time",
            "forecast_price_key": "agile_pred",
            "forecast_unit_multiplier": 1.0,
        },
    )
    assert result["step_id"] == "short_range_custom"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], _CUSTOM_INPUT)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["options"]["forecast_entity"] == "sensor.my_forecast"
    assert result["options"][CONF_SHORT_RANGE_ENTITY] == SHORT_RANGE_ENTITY


# ---------------------------------------------------------------------------
# Options flow: the existing-install guarantees
# ---------------------------------------------------------------------------


async def _walk_options(hass, entry, base_overrides=None, *, steps=()):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**BASE_INPUT, **(base_overrides or {})}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], RATES_OCTOPUS_INPUT)
    if result["step_id"] == "forecast_agile_predict":
        result = await hass.config_entries.options.async_configure(result["flow_id"], FORECAST_AGILE_PREDICT_INPUT)
    for user_input in steps:
        result = await hass.config_entries.options.async_configure(result["flow_id"], user_input)
    await hass.async_block_till_done()
    return result


async def test_saving_the_options_flow_untouched_changes_nothing_for_a_legacy_entry(hass):
    """The golden rule. A legacy entry (no short-range keys at all) re-saved through the
    options flow keeps every one of its existing values, and the only additions are the
    two explicit 'off' keys."""
    entry = await async_setup_wholesale_entry(hass, options=FULL_OPTIONS)
    assert CONF_SHORT_RANGE_PROVIDER not in entry.options

    result = await _walk_options(hass, entry)

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert dict(entry.options) == {**FULL_OPTIONS, CONF_SHORT_RANGE_PROVIDER: "none", CONF_SHORT_RANGE_ENTITY: None}


async def test_a_legacy_entry_reopens_with_none_selected(hass):
    entry = await async_setup_wholesale_entry(hass, options=FULL_OPTIONS)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert _default_of(result, CONF_SHORT_RANGE_PROVIDER) == SHORT_RANGE_PROVIDER_NONE


async def test_an_opted_in_entry_reopens_with_its_provider_and_values_prefilled(hass):
    entry = await async_setup_wholesale_entry(
        hass, options=short_range_options(FULL_OPTIONS, **{CONF_SHORT_RANGE_CONFIDENCE: 77})
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert _default_of(result, CONF_SHORT_RANGE_PROVIDER) == SHORT_RANGE_PROVIDER_AGILE_RATES_API
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**BASE_INPUT, CONF_SHORT_RANGE_PROVIDER: SHORT_RANGE_PROVIDER_AGILE_RATES_API}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], RATES_OCTOPUS_INPUT)
    result = await hass.config_entries.options.async_configure(result["flow_id"], FORECAST_AGILE_PREDICT_INPUT)

    assert result["step_id"] == "short_range_agile_rates_api"
    assert _default_of(result, CONF_SHORT_RANGE_ENTITY) == SHORT_RANGE_ENTITY
    assert _default_of(result, CONF_SHORT_RANGE_CONFIDENCE) == 77


async def test_opting_in_through_the_options_flow_reloads_and_starts_reading_the_entity(hass):
    from .factories import agile_rates_points, set_short_range_entity

    entry = await async_setup_wholesale_entry(hass, options=FULL_OPTIONS)
    set_short_range_entity(
        hass, agile_rates_points(__import__("homeassistant.util.dt", fromlist=["now"]).now(), [20.0, 21.0])
    )
    assert hass.data[DOMAIN][entry.entry_id].short_range_provider_label is None

    result = await _walk_options(
        hass,
        entry,
        {CONF_SHORT_RANGE_PROVIDER: SHORT_RANGE_PROVIDER_AGILE_RATES_API},
        steps=[{CONF_SHORT_RANGE_ENTITY: SHORT_RANGE_ENTITY, CONF_SHORT_RANGE_CONFIDENCE: 88}],
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert (
        coordinator.short_range_provider_label == SHORT_RANGE_PROVIDERS[SHORT_RANGE_PROVIDER_AGILE_RATES_API]["label"]
    )
    assert {s["source"] for s in coordinator._collect_prices()} >= {"short_range"}


async def test_opting_back_out_turns_it_off_even_though_old_keys_remain(hass):
    entry = await async_setup_wholesale_entry(hass, options=short_range_options(FULL_OPTIONS))

    await _walk_options(hass, entry, {CONF_SHORT_RANGE_PROVIDER: SHORT_RANGE_PROVIDER_NONE})

    assert entry.options[CONF_SHORT_RANGE_PROVIDER] == SHORT_RANGE_PROVIDER_NONE
    assert entry.options[CONF_SHORT_RANGE_ENTITY] is None
    assert hass.data[DOMAIN][entry.entry_id].short_range_provider_label is None


async def test_switching_provider_clears_the_previous_providers_estimate_flag(hass):
    entry = await async_setup_wholesale_entry(hass, options=short_range_options(FULL_OPTIONS))
    assert entry.options[CONF_SHORT_RANGE_ESTIMATE_FLAG_KEY] == "agileRate.result.prediction"

    await _walk_options(
        hass,
        entry,
        {CONF_SHORT_RANGE_PROVIDER: SHORT_RANGE_PROVIDER_AGILE_PREDICT},
        steps=[{CONF_SHORT_RANGE_ENTITY: "sensor.other"}],
    )

    assert entry.options[CONF_SHORT_RANGE_ESTIMATE_FLAG_KEY] is None
    assert entry.options[CONF_SHORT_RANGE_DATETIME_KEY] == "date_time"
    assert entry.options[CONF_SHORT_RANGE_ENTITY] == "sensor.other"


async def test_reenabling_after_none_requires_the_entity_cleanly(hass):
    """Same shape as issue #38 for the long-range forecast: the stale explicit None left
    by 'None' must not leak into the entity selector as a default."""
    entry = await async_setup_wholesale_entry(
        hass,
        options={**FULL_OPTIONS, CONF_SHORT_RANGE_PROVIDER: SHORT_RANGE_PROVIDER_NONE, CONF_SHORT_RANGE_ENTITY: None},
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**BASE_INPUT, CONF_SHORT_RANGE_PROVIDER: SHORT_RANGE_PROVIDER_AGILE_RATES_API}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], RATES_OCTOPUS_INPUT)
    result = await hass.config_entries.options.async_configure(result["flow_id"], FORECAST_AGILE_PREDICT_INPUT)
    assert result["step_id"] == "short_range_agile_rates_api"

    with pytest.raises(InvalidData) as exc_info:
        await hass.config_entries.options.async_configure(result["flow_id"], {})

    assert CONF_SHORT_RANGE_ENTITY in exc_info.value.schema_errors
    assert "None" not in str(exc_info.value.schema_errors[CONF_SHORT_RANGE_ENTITY])


# ---------------------------------------------------------------------------
# Existing configs of every era still set up and behave
# ---------------------------------------------------------------------------

_LEGACY_SHAPES = {
    "entity_wiring_and_providers_only": {
        k: v
        for k, v in FULL_OPTIONS.items()
        if not k.startswith("default_") and k != CONF_ENABLE_OPPORTUNISTIC_CHARGING
    },
    "forecast_disabled": {**FULL_OPTIONS, CONF_FORECAST_PROVIDER: FORECAST_PROVIDER_NONE, CONF_FORECAST_ENTITY: None},
    "with_setup_time_defaults": {
        **FULL_OPTIONS,
        CONF_DEFAULT_REQUIRED_HOURS: 6.0,
        CONF_DEFAULT_GAMBLE_TOLERANCE: 20.0,
        CONF_DEFAULT_MAX_PRICE: 30.0,
        CONF_DEFAULT_MIN_BLOCK_HOURS: 1.0,
        CONF_DEFAULT_READY_BY_HOUR: 8,
        CONF_DEFAULT_READY_BY_DAY_OFFSET: "1",
    },
    "opportunistic_enabled": {**FULL_OPTIONS, CONF_ENABLE_OPPORTUNISTIC_CHARGING: True},
}


@pytest.mark.parametrize("shape", list(_LEGACY_SHAPES), ids=list(_LEGACY_SHAPES))
async def test_legacy_option_shapes_set_up_unchanged_with_no_new_entities(hass, shape):
    options = _LEGACY_SHAPES[shape]
    assert CONF_SHORT_RANGE_PROVIDER not in options

    entry = await async_setup_wholesale_entry(hass, options=options)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    assert entry.state is config_entries.ConfigEntryState.LOADED
    assert coordinator.last_update_success
    assert coordinator.short_range_provider_label is None
    assert coordinator._short_range_spec() is None
    assert {s.entity_id for s in hass.states.async_all()} >= EXPECTED_ENTITY_IDS
    assert dict(entry.options) == options  # setup never rewrites the stored options


# ---------------------------------------------------------------------------
# Strings: every new screen and field is labelled, in both files
# ---------------------------------------------------------------------------


def _load(name):
    return json.loads((COMPONENT_DIR / name).read_text(encoding="utf-8"))


def test_strings_json_and_the_english_translation_are_byte_identical():
    """Config-flow labels silently break if these drift (AGENTS.md non-negotiable)."""
    assert (COMPONENT_DIR / "strings.json").read_bytes() == (COMPONENT_DIR / "translations" / "en.json").read_bytes()


def _own_step_ids():
    ids = set()
    for cls in (
        config_flow._ProviderStepsMixin,
        config_flow.WholesaleEvScheduleConfigFlow,
        config_flow.WholesaleEvScheduleOptionsFlow,
    ):
        ids |= {name.removeprefix("async_step_") for name in vars(cls) if name.startswith("async_step_")}
    return ids


_SCHEMAS = {
    "rates_octopus_energy": lambda: config_flow.rates_octopus_energy_schema({}),
    "rates_custom": lambda: config_flow.rates_custom_schema({}),
    "forecast_agile_predict": lambda: config_flow.forecast_agile_predict_schema({}),
    "forecast_custom": lambda: config_flow.forecast_custom_schema({}),
    "short_range_agile_rates_api": lambda: config_flow.short_range_profile_schema({}),
    "short_range_agile_predict": lambda: config_flow.short_range_profile_schema({}),
    "short_range_custom": lambda: config_flow.short_range_custom_schema({}),
}


def test_every_step_the_flow_defines_has_a_schema_mapping_in_this_test():
    """Guards the structural test below against silently skipping a newly-added step."""
    assert _own_step_ids() - {"user", "init"} == set(_SCHEMAS)


@pytest.mark.parametrize("section", ["config", "options"])
@pytest.mark.parametrize("step_id", sorted(_SCHEMAS))
def test_every_field_of_every_step_has_a_label_and_no_description_dangles(section, step_id):
    step = _load("strings.json")[section]["step"][step_id]
    fields = {marker.schema for marker in _SCHEMAS[step_id]().schema}

    assert fields <= set(step["data"]), f"{section}.{step_id} is missing labels for {fields - set(step['data'])}"
    assert step["title"]
    assert set(step.get("data_description", {})) <= set(step["data"])


@pytest.mark.parametrize(
    "section,step_id,include_name", [("config", "user", True), ("options", "init", False)], ids=["user", "init"]
)
def test_the_first_screen_labels_every_field_including_the_new_dropdown(section, step_id, include_name):
    step = _load("strings.json")[section]["step"][step_id]
    fields = {marker.schema for marker in config_flow.base_schema({}, include_name=include_name).schema}

    assert fields <= set(step["data"])
    assert CONF_SHORT_RANGE_PROVIDER in step["data"]
    assert CONF_SHORT_RANGE_PROVIDER in step["data_description"]
