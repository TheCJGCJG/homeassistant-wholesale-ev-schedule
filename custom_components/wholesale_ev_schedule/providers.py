"""Registry of known wholesale price data source "providers".

Each entry maps a provider id to the fixed attribute/key shape that source
exposes, so users pick a name in the config flow instead of typing raw HA
attribute/key names. "custom" is always available as an escape hatch for a
source not modelled here — see config_flow.py's *_custom steps.

To add a new provider: add an entry to RATE_PROVIDERS or FORECAST_PROVIDERS,
add its label to strings.json/translations, and add one config_flow.py step
mirroring the existing octopus_energy / agile_predict ones.

Sources verified directly against the upstream integrations on 2026-07-08:
- Octopus Energy: BottlecapDave/HomeAssistant-OctopusEnergy
  (coordinators/__init__.py:__raise_rate_event, utils/__init__.py:private_rates_to_public_rates)
  — event entity `rates` attribute, each item {start, end, value_inc_vat}, value_inc_vat in GBP/kWh.
- AgilePredict: https://agilepredict.com/v2/api_how_to/ and the live /api/{region} JSON response
  — `prices` list, each item {date_time, agile_pred}, agile_pred already in p/kWh.
"""

RATE_PROVIDER_OCTOPUS_ENERGY = "octopus_energy"
RATE_PROVIDER_CUSTOM = "custom"

RATE_PROVIDERS = {
    RATE_PROVIDER_OCTOPUS_ENERGY: {
        "label": "Octopus Energy (current/next day rates event entity)",
        "attribute": "rates",
        "start_key": "start",
        "value_key": "value_inc_vat",
        "unit_multiplier": 100.0,  # the event entity reports GBP/kWh; we work in p/kWh
    },
    RATE_PROVIDER_CUSTOM: {
        "label": "Custom / other",
    },
}

FORECAST_PROVIDER_NONE = "none"
FORECAST_PROVIDER_AGILE_PREDICT = "agile_predict"
FORECAST_PROVIDER_CUSTOM = "custom"

FORECAST_PROVIDERS = {
    FORECAST_PROVIDER_NONE: {
        "label": "None — schedule from actual rates only",
    },
    FORECAST_PROVIDER_AGILE_PREDICT: {
        "label": "AgilePredict (agilepredict.com)",
        "attribute": "prices",
        "datetime_key": "date_time",
        "price_key": "agile_pred",
        "unit_multiplier": 1.0,  # already reports p/kWh
    },
    FORECAST_PROVIDER_CUSTOM: {
        "label": "Custom / other",
    },
}
