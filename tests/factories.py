"""Shared fixtures/constants for setting up a Wholesale EV Schedule config entry
in tests without repeating the full options dict or walking the whole config
flow everywhere."""
from datetime import datetime, timedelta

from homeassistant.const import CONF_NAME
from homeassistant.util import slugify
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wholesale_ev_schedule.const import (
    CONF_CHARGER_CONNECTED_STATES,
    CONF_CHARGER_STATE_ENTITY,
    CONF_CURRENT_RATES_ENTITY,
    CONF_FORECAST_ATTRIBUTE,
    CONF_FORECAST_DATETIME_KEY,
    CONF_FORECAST_ENTITY,
    CONF_FORECAST_PRICE_KEY,
    CONF_FORECAST_PROVIDER,
    CONF_FORECAST_UNIT_MULTIPLIER,
    CONF_GAMBLE_TOLERANCE,
    CONF_MAX_PRICE,
    CONF_MIN_BLOCK_HOURS,
    CONF_NEXT_RATES_ENTITY,
    CONF_RATE_START_KEY,
    CONF_RATE_UNIT_MULTIPLIER,
    CONF_RATE_VALUE_KEY,
    CONF_RATES_ATTRIBUTE,
    CONF_RATES_PROVIDER,
    CONF_UPDATE_INTERVAL_MINUTES,
    DEFAULT_CHARGER_CONNECTED_STATES,
    DEFAULT_GAMBLE_TOLERANCE,
    DEFAULT_MAX_PRICE,
    DEFAULT_MIN_BLOCK_HOURS,
    DEFAULT_NAME,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
)
from custom_components.wholesale_ev_schedule.providers import (
    FORECAST_PROVIDER_AGILE_PREDICT,
    FORECAST_PROVIDERS,
    RATE_PROVIDER_OCTOPUS_ENERGY,
    RATE_PROVIDERS,
)

CURRENT_RATES_ENTITY = "event.octopus_energy_electricity_current_day_rates"
NEXT_RATES_ENTITY = "event.octopus_energy_electricity_next_day_rates"
FORECAST_ENTITY = "sensor.agile_predict"
CHARGER_STATE_ENTITY = "sensor.car_charger_work_state"

# Inputs matching each config-flow step's schema, for tests that walk the flow.
BASE_INPUT = {
    CONF_CHARGER_STATE_ENTITY: CHARGER_STATE_ENTITY,
    CONF_CHARGER_CONNECTED_STATES: DEFAULT_CHARGER_CONNECTED_STATES,
    CONF_RATES_PROVIDER: RATE_PROVIDER_OCTOPUS_ENERGY,
    CONF_FORECAST_PROVIDER: FORECAST_PROVIDER_AGILE_PREDICT,
}
RATES_OCTOPUS_INPUT = {
    CONF_CURRENT_RATES_ENTITY: CURRENT_RATES_ENTITY,
    CONF_NEXT_RATES_ENTITY: NEXT_RATES_ENTITY,
}
FORECAST_AGILE_PREDICT_INPUT = {
    CONF_FORECAST_ENTITY: FORECAST_ENTITY,
}
ADVANCED_INPUT = {
    CONF_GAMBLE_TOLERANCE: DEFAULT_GAMBLE_TOLERANCE,
    CONF_MIN_BLOCK_HOURS: DEFAULT_MIN_BLOCK_HOURS,
    CONF_MAX_PRICE: DEFAULT_MAX_PRICE,
    CONF_UPDATE_INTERVAL_MINUTES: DEFAULT_UPDATE_INTERVAL_MINUTES,
}

_octopus_profile = RATE_PROVIDERS[RATE_PROVIDER_OCTOPUS_ENERGY]
_agile_predict_profile = FORECAST_PROVIDERS[FORECAST_PROVIDER_AGILE_PREDICT]

# The fully-resolved options dict a real flow through BASE_INPUT ->
# RATES_OCTOPUS_INPUT -> FORECAST_AGILE_PREDICT_INPUT -> ADVANCED_INPUT would
# produce — used to set up a config entry directly, bypassing the flow, for
# tests that only care about the coordinator/entities.
FULL_OPTIONS = {
    **BASE_INPUT,
    **RATES_OCTOPUS_INPUT,
    CONF_RATES_ATTRIBUTE: _octopus_profile["attribute"],
    CONF_RATE_START_KEY: _octopus_profile["start_key"],
    CONF_RATE_VALUE_KEY: _octopus_profile["value_key"],
    CONF_RATE_UNIT_MULTIPLIER: _octopus_profile["unit_multiplier"],
    **FORECAST_AGILE_PREDICT_INPUT,
    CONF_FORECAST_ATTRIBUTE: _agile_predict_profile["attribute"],
    CONF_FORECAST_DATETIME_KEY: _agile_predict_profile["datetime_key"],
    CONF_FORECAST_PRICE_KEY: _agile_predict_profile["price_key"],
    CONF_FORECAST_UNIT_MULTIPLIER: _agile_predict_profile["unit_multiplier"],
    **ADVANCED_INPUT,
}

_ENTITY_SUFFIXES = {
    "sensor": ["charging_state", "charging_schedule", "next_slot_start", "next_slot_end", "hours_remaining"],
    "binary_sensor": ["charging_desired"],
    "number": ["charging_hours_required", "boost_duration_hours"],
    "datetime": ["ready_by"],
    "button": ["boost_cancel", "stop"],
}


def expected_entity_ids(prefix: str = slugify(DEFAULT_NAME)) -> set[str]:
    """Every entity_id a config entry with this name's slug should register."""
    return {
        f"{platform}.{prefix}_{suffix}"
        for platform, suffixes in _ENTITY_SUFFIXES.items()
        for suffix in suffixes
    }


# Every entity_id this integration creates must start with this prefix — the
# integration is designed to run alongside a pre-existing pyscript-based EV
# charging setup on the same HA instance and must never collide with it.
EXPECTED_ENTITY_IDS = expected_entity_ids()

# The pyscript original's entity_ids — asserted-against as "must never appear".
PYSCRIPT_ENTITY_IDS = {
    "input_datetime.ev_charger_ready_by",
    "input_number.polestar_2_charging_hours_required",
    "binary_sensor.ev_charging_desired",
    "sensor.ev_charging_state",
    "sensor.ev_charging_schedule",
    "sensor.ev_charging_next_slot_start",
    "sensor.ev_charging_next_slot_end",
    "sensor.ev_charging_hours_remaining",
}


def octopus_rate_points(start: datetime, count: int, price_gbp_per_kwh: float, step_minutes: int = 30) -> list[dict]:
    """Build `rates` attribute entries shaped like the Octopus Energy integration."""
    return [
        {
            "start": (start + timedelta(minutes=step_minutes * i)).isoformat(),
            "end": (start + timedelta(minutes=step_minutes * (i + 1))).isoformat(),
            "value_inc_vat": price_gbp_per_kwh,
        }
        for i in range(count)
    ]


def set_octopus_rate_entity(hass, entity_id: str, points: list[dict]) -> None:
    hass.states.async_set(entity_id, "populated", {"rates": points})


def set_charger_state(hass, state: str) -> None:
    hass.states.async_set(CHARGER_STATE_ENTITY, state, {})


async def async_setup_wholesale_entry(hass, options: dict | None = None, name: str = DEFAULT_NAME) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_NAME: name},
        options=options if options is not None else FULL_OPTIONS,
        unique_id=slugify(name),
        title=name,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry
