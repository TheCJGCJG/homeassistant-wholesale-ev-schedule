"""Registry of known wholesale price data source "providers".

Each entry maps a provider id to the fixed attribute/key shape that source
exposes, so users pick a name in the config flow instead of typing raw HA
attribute/key names. "custom" is always available as an escape hatch for a
source not modelled here — see config_flow.py's *_custom steps.

To add a new provider: add an entry to RATE_PROVIDERS, FORECAST_PROVIDERS or
SHORT_RANGE_PROVIDERS, add its label to strings.json/translations, and add one
config_flow.py step mirroring the existing octopus_energy / agile_predict ones.

Sources verified directly against the upstream integrations on 2026-07-08:
- Octopus Energy: BottlecapDave/HomeAssistant-OctopusEnergy
  (coordinators/__init__.py:__raise_rate_event, utils/__init__.py:private_rates_to_public_rates)
  — event entity `rates` attribute, each item {start, end, value_inc_vat}, value_inc_vat in GBP/kWh.
- AgilePredict: https://agilepredict.com/v2/api_how_to/ and the live /api/{region} JSON response
  — `prices` list, each item {date_time, agile_pred}, agile_pred already in p/kWh.
  Agile Forecast is a known fork/service that exposes the identical API shape,
  so this provider entry covers both under a shared "AgilePredict-compatible"
  label — see FORECAST_PROVIDER_AGILE_PREDICT below.

Verified against the upstream docs and a live REST sensor on 2026-10-30:
- Agile Rates API: https://agilerates.uk/api/ (agile_rates_region_{CODE}.json)
  — `rates` list, each item {deliveryStart, agileRate: {result: {rate, prediction,
  source}}}, rate already in p/kWh inc. VAT. `prediction` is true while a slot is
  estimated from the ~10:00 UK hourly auctions and false once the ~16:00 30-minute
  auction (the one Octopus prices Agile from) has published it.
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
        "label": "AgilePredict-compatible API (e.g. AgilePredict, Agile Forecast)",
        "attribute": "prices",
        "datetime_key": "date_time",
        "price_key": "agile_pred",
        "unit_multiplier": 1.0,  # already reports p/kWh
    },
    FORECAST_PROVIDER_CUSTOM: {
        "label": "Custom / other",
    },
}

# A short-range forecast: a more accurate source covering fewer days ahead than the
# long-range forecast above. It overrides that forecast wherever it has data (and is
# itself overridden by actual rates) -- see scheduler.deduplicate_and_sort_prices.
# Profiles may also carry `estimate_flag_key`: a per-point flag that is falsy once the
# price is final, which makes final prices count as actual-grade (price_parsing.is_estimate).
SHORT_RANGE_PROVIDER_NONE = "none"
SHORT_RANGE_PROVIDER_AGILE_RATES_API = "agile_rates_api"
SHORT_RANGE_PROVIDER_AGILE_PREDICT = FORECAST_PROVIDER_AGILE_PREDICT
SHORT_RANGE_PROVIDER_CUSTOM = "custom"

SHORT_RANGE_PROVIDERS = {
    SHORT_RANGE_PROVIDER_NONE: {
        "label": "None — long-range forecast only",
    },
    SHORT_RANGE_PROVIDER_AGILE_RATES_API: {
        "label": "Agile Rates API (agilerates.uk, day-ahead)",
        "attribute": "rates",
        "datetime_key": "deliveryStart",
        "price_key": "agileRate.result.rate",
        "estimate_flag_key": "agileRate.result.prediction",
        "unit_multiplier": 1.0,  # already p/kWh inc. VAT
    },
    SHORT_RANGE_PROVIDER_AGILE_PREDICT: FORECAST_PROVIDERS[FORECAST_PROVIDER_AGILE_PREDICT],
    SHORT_RANGE_PROVIDER_CUSTOM: {
        "label": "Custom / other",
    },
}
