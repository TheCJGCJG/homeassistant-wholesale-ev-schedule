"""Shared fixtures/constants for setting up a Wholesale EV Schedule config entry
in tests without repeating the full options dict everywhere."""
from datetime import datetime, timedelta

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wholesale_ev_schedule.const import (
    CONF_CHARGER_CONNECTED_STATES,
    CONF_CHARGER_STATE_ENTITY,
    CONF_CURRENT_RATES_ENTITY,
    CONF_FORECAST_ATTRIBUTE,
    CONF_FORECAST_DATETIME_KEY,
    CONF_FORECAST_ENTITY,
    CONF_FORECAST_PRICE_KEY,
    CONF_GAMBLE_TOLERANCE,
    CONF_MAX_PRICE,
    CONF_MIN_BLOCK_HOURS,
    CONF_NEXT_RATES_ENTITY,
    CONF_RATE_START_KEY,
    CONF_RATE_UNIT_MULTIPLIER,
    CONF_RATE_VALUE_KEY,
    CONF_RATES_ATTRIBUTE,
    CONF_UPDATE_INTERVAL_MINUTES,
    DEFAULT_CHARGER_CONNECTED_STATES,
    DEFAULT_FORECAST_ATTRIBUTE,
    DEFAULT_FORECAST_DATETIME_KEY,
    DEFAULT_FORECAST_PRICE_KEY,
    DEFAULT_GAMBLE_TOLERANCE,
    DEFAULT_MAX_PRICE,
    DEFAULT_MIN_BLOCK_HOURS,
    DEFAULT_RATE_START_KEY,
    DEFAULT_RATE_UNIT_MULTIPLIER,
    DEFAULT_RATE_VALUE_KEY,
    DEFAULT_RATES_ATTRIBUTE,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
)

CURRENT_RATES_ENTITY = "event.octopus_energy_electricity_current_day_rates"
NEXT_RATES_ENTITY = "event.octopus_energy_electricity_next_day_rates"
FORECAST_ENTITY = "sensor.agile_forecast"
CHARGER_STATE_ENTITY = "sensor.car_charger_work_state"

ESSENTIAL_OPTIONS = {
    CONF_CURRENT_RATES_ENTITY: CURRENT_RATES_ENTITY,
    CONF_NEXT_RATES_ENTITY: NEXT_RATES_ENTITY,
    CONF_FORECAST_ENTITY: FORECAST_ENTITY,
    CONF_CHARGER_STATE_ENTITY: CHARGER_STATE_ENTITY,
    CONF_CHARGER_CONNECTED_STATES: DEFAULT_CHARGER_CONNECTED_STATES,
}

ADVANCED_OPTIONS = {
    CONF_GAMBLE_TOLERANCE: DEFAULT_GAMBLE_TOLERANCE,
    CONF_MIN_BLOCK_HOURS: DEFAULT_MIN_BLOCK_HOURS,
    CONF_MAX_PRICE: DEFAULT_MAX_PRICE,
    CONF_UPDATE_INTERVAL_MINUTES: DEFAULT_UPDATE_INTERVAL_MINUTES,
    CONF_RATE_UNIT_MULTIPLIER: DEFAULT_RATE_UNIT_MULTIPLIER,
    CONF_RATES_ATTRIBUTE: DEFAULT_RATES_ATTRIBUTE,
    CONF_RATE_START_KEY: DEFAULT_RATE_START_KEY,
    CONF_RATE_VALUE_KEY: DEFAULT_RATE_VALUE_KEY,
    CONF_FORECAST_ATTRIBUTE: DEFAULT_FORECAST_ATTRIBUTE,
    CONF_FORECAST_DATETIME_KEY: DEFAULT_FORECAST_DATETIME_KEY,
    CONF_FORECAST_PRICE_KEY: DEFAULT_FORECAST_PRICE_KEY,
}

FULL_OPTIONS = {**ESSENTIAL_OPTIONS, **ADVANCED_OPTIONS}

# Every entity_id this integration creates must start with this prefix — the
# integration is designed to run alongside a pre-existing pyscript-based EV
# charging setup on the same HA instance and must never collide with it.
EXPECTED_ENTITY_IDS = {
    "sensor.wholesale_ev_schedule_charging_state",
    "sensor.wholesale_ev_schedule_charging_schedule",
    "sensor.wholesale_ev_schedule_next_slot_start",
    "sensor.wholesale_ev_schedule_next_slot_end",
    "sensor.wholesale_ev_schedule_hours_remaining",
    "binary_sensor.wholesale_ev_schedule_charging_desired",
    "number.wholesale_ev_schedule_charging_hours_required",
    "number.wholesale_ev_schedule_boost_duration_hours",
    "datetime.wholesale_ev_schedule_ready_by",
    "button.wholesale_ev_schedule_boost_cancel",
    "button.wholesale_ev_schedule_stop",
}

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


async def async_setup_wholesale_entry(hass, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, options=options if options is not None else FULL_OPTIONS)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry
