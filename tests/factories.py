"""Shared fixtures/constants for setting up a Wholesale EV Schedule config entry
in tests without repeating the full options dict or walking the whole config
flow everywhere."""

from datetime import datetime, timedelta

from homeassistant.const import CONF_NAME
from homeassistant.util import slugify
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wholesale_ev_schedule.const import (
    CONF_CURRENT_RATES_ENTITY,
    CONF_DEFAULT_GAMBLE_TOLERANCE,
    CONF_DEFAULT_MAX_PRICE,
    CONF_DEFAULT_MIN_BLOCK_HOURS,
    CONF_DEFAULT_OPPORTUNISTIC_MAX_PRICE,
    CONF_DEFAULT_OPPORTUNISTIC_OFFSET_DAYS,
    CONF_DEFAULT_OPPORTUNISTIC_TARGET_HOURS,
    CONF_DEFAULT_READY_BY_DAY_OFFSET,
    CONF_DEFAULT_READY_BY_HOUR,
    CONF_DEFAULT_REQUIRED_HOURS,
    CONF_ENABLE_OPPORTUNISTIC_CHARGING,
    CONF_FORECAST_ATTRIBUTE,
    CONF_FORECAST_DATETIME_KEY,
    CONF_FORECAST_ENTITY,
    CONF_FORECAST_PRICE_KEY,
    CONF_FORECAST_PROVIDER,
    CONF_FORECAST_UNIT_MULTIPLIER,
    CONF_NEXT_RATES_ENTITY,
    CONF_RATE_START_KEY,
    CONF_RATE_UNIT_MULTIPLIER,
    CONF_RATE_VALUE_KEY,
    CONF_RATES_ATTRIBUTE,
    CONF_RATES_PROVIDER,
    CONF_UPDATE_INTERVAL_MINUTES,
    DEFAULT_ENABLE_OPPORTUNISTIC_CHARGING,
    DEFAULT_GAMBLE_TOLERANCE,
    DEFAULT_MAX_PRICE,
    DEFAULT_MIN_BLOCK_HOURS,
    DEFAULT_NAME,
    DEFAULT_OPPORTUNISTIC_MAX_PRICE,
    DEFAULT_OPPORTUNISTIC_OFFSET_DAYS,
    DEFAULT_OPPORTUNISTIC_TARGET_HOURS,
    DEFAULT_READY_BY_DAY_OFFSET,
    DEFAULT_READY_BY_HOUR,
    DEFAULT_REQUIRED_HOURS,
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

# Inputs matching each config-flow step's schema, for tests that walk the flow.
# The flow is: user/init -> rates_octopus_energy -> forecast_agile_predict ->
# (entry created). Scheduling tolerances (gamble tolerance, block hours, max
# price) and the manual charge override aren't part of the flow at all — they're
# live number/select entities set via the coordinator setters (see
# async_setup_wholesale_entry). There's no charger-state entity wiring either;
# charging_desired is computed purely from the schedule + the override.
BASE_INPUT = {
    CONF_UPDATE_INTERVAL_MINUTES: DEFAULT_UPDATE_INTERVAL_MINUTES,
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

_octopus_profile = RATE_PROVIDERS[RATE_PROVIDER_OCTOPUS_ENERGY]
_agile_predict_profile = FORECAST_PROVIDERS[FORECAST_PROVIDER_AGILE_PREDICT]

# The fully-resolved options dict a real flow through BASE_INPUT ->
# RATES_OCTOPUS_INPUT -> FORECAST_AGILE_PREDICT_INPUT would produce — used to
# set up a config entry directly, bypassing the flow, for tests that only
# care about the coordinator/entities. Since BASE_INPUT doesn't set any of the
# CONF_DEFAULT_* setup-time-default fields (see const.py), a real flow leaves
# them at the schema's own defaults — which are the pre-existing DEFAULT_*
# constants (0 for the day offset), so this is identical to how a fresh
# install behaved before that feature existed.
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
    CONF_DEFAULT_REQUIRED_HOURS: DEFAULT_REQUIRED_HOURS,
    CONF_DEFAULT_GAMBLE_TOLERANCE: DEFAULT_GAMBLE_TOLERANCE,
    CONF_DEFAULT_MAX_PRICE: DEFAULT_MAX_PRICE,
    CONF_DEFAULT_MIN_BLOCK_HOURS: DEFAULT_MIN_BLOCK_HOURS,
    CONF_DEFAULT_READY_BY_HOUR: DEFAULT_READY_BY_HOUR,
    CONF_DEFAULT_READY_BY_DAY_OFFSET: str(DEFAULT_READY_BY_DAY_OFFSET),
    # Opportunistic charging (issue #53) -- off by default, same as a real
    # flow through base_schema() produces when left untouched. See
    # coordinator.py/switch.py/number.py/datetime.py/sensor.py's
    # CONF_ENABLE_OPPORTUNISTIC_CHARGING guards: False here means this
    # options dict (used by the vast majority of tests via
    # async_setup_wholesale_entry) creates zero opportunistic entities and
    # exercises zero opportunistic scheduling logic, identical to how a
    # fresh install behaved before this feature existed.
    CONF_ENABLE_OPPORTUNISTIC_CHARGING: DEFAULT_ENABLE_OPPORTUNISTIC_CHARGING,
    CONF_DEFAULT_OPPORTUNISTIC_OFFSET_DAYS: DEFAULT_OPPORTUNISTIC_OFFSET_DAYS,
    CONF_DEFAULT_OPPORTUNISTIC_TARGET_HOURS: DEFAULT_OPPORTUNISTIC_TARGET_HOURS,
    CONF_DEFAULT_OPPORTUNISTIC_MAX_PRICE: DEFAULT_OPPORTUNISTIC_MAX_PRICE,
}

# FULL_OPTIONS with opportunistic charging turned on -- for tests that need
# the opportunistic entities/scheduling to actually exist. Purely additive:
# FULL_OPTIONS itself (and every test using it unmodified) is untouched.
OPPORTUNISTIC_OPTIONS = {**FULL_OPTIONS, CONF_ENABLE_OPPORTUNISTIC_CHARGING: True}

_ENTITY_SUFFIXES = {
    "sensor": [
        "charging_state",
        "charging_schedule",
        "next_slot_start",
        "next_slot_end",
        "next_slot_average_price",
        "next_slot_estimated_cost",
        "hours_remaining",
        "time_remaining",
        "boost_ends_at",
        "block_count",
        "upcoming_block_2_start",
        "upcoming_block_2_end",
        "upcoming_block_3_start",
        "upcoming_block_3_end",
        "candidate_price_points",
        "cheapest_available_price",
        "most_expensive_available_price",
        "average_price_next_24h",
        "average_price_all_data",
        "price_data_sources",
        "active_providers",
    ],
    "binary_sensor": ["charging_desired"],
    "number": [
        "charging_hours_required",
        "boost_duration_hours",
        "gamble_tolerance",
        "min_block_hours",
        "max_price",
        "assumed_charge_kwh",
    ],
    "datetime": ["ready_by"],
    "select": ["charge_override", "optimization_algorithm"],
    "button": ["boost_cancel", "stop", "reset"],
}


def expected_entity_ids(prefix: str = slugify(DEFAULT_NAME)) -> set[str]:
    """Every entity_id a config entry with this name's slug should register."""
    return {f"{platform}.{prefix}_{suffix}" for platform, suffixes in _ENTITY_SUFFIXES.items() for suffix in suffixes}


# Every entity_id this integration creates must start with this prefix — the
# integration is designed to run alongside a pre-existing pyscript-based EV
# charging setup on the same HA instance and must never collide with it.
EXPECTED_ENTITY_IDS = expected_entity_ids()

# The additional entity_ids created only when opportunistic charging is
# enabled (see CONF_ENABLE_OPPORTUNISTIC_CHARGING) -- kept separate from
# _ENTITY_SUFFIXES/EXPECTED_ENTITY_IDS above so those stay exactly what a
# default (opportunistic-off) install produces, unchanged.
_OPPORTUNISTIC_ENTITY_SUFFIXES = {
    "switch": ["opportunistic_charging_enabled"],
    "number": ["opportunistic_target_hours", "opportunistic_max_price"],
    "datetime": ["opportunistic_ready_by"],
    "sensor": ["opportunistic_hours_remaining", "opportunistic_next_slot_start", "opportunistic_next_slot_end"],
}


def expected_opportunistic_entity_ids(prefix: str = slugify(DEFAULT_NAME)) -> set[str]:
    """The entity_ids created in addition to EXPECTED_ENTITY_IDS when
    opportunistic charging is enabled."""
    return {
        f"{platform}.{prefix}_{suffix}"
        for platform, suffixes in _OPPORTUNISTIC_ENTITY_SUFFIXES.items()
        for suffix in suffixes
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


def priced_points(start: datetime, prices: list[float], step_minutes: int = 30) -> list[dict]:
    """Build `rates` attribute entries from an explicit per-slot price list --
    for tests that need a specific price shape (cheap dips at chosen
    positions, spikes, etc.) rather than octopus_rate_points' single uniform
    rate."""
    return [
        {
            "start": (start + timedelta(minutes=step_minutes * i)).isoformat(),
            "end": (start + timedelta(minutes=step_minutes * (i + 1))).isoformat(),
            "value_inc_vat": price,
        }
        for i, price in enumerate(prices)
    ]


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
