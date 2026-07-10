"""Constants for the Wholesale EV Schedule integration."""

from homeassistant.const import Platform

DOMAIN = "wholesale_ev_schedule"
PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.DATETIME,
    Platform.BUTTON,
    Platform.SELECT,
]

# Default value for CONF_NAME — this is slugified into the entity_id prefix
# (see entity.py), so it stays the default "wholesale_ev_schedule_..." prefix
# used by the first/only instance. Set a distinct name per instance to run
# multiple copies of this integration (e.g. one per car) side by side without
# entity_id collisions.
DEFAULT_NAME = "Wholesale EV Schedule"

# Config / options keys — entity wiring.
CONF_CURRENT_RATES_ENTITY = "current_rates_entity"
CONF_NEXT_RATES_ENTITY = "next_rates_entity"
CONF_FORECAST_ENTITY = "forecast_entity"

# Config / options keys — timing only. Gamble tolerance, min block hours, and
# max price used to live here too, but those are the kind of thing you tweak
# day-to-day — they're now live `number` entities (see coordinator.py) rather
# than config-flow options that require a reload to change.
CONF_UPDATE_INTERVAL_MINUTES = "update_interval_minutes"

# Config / options keys — setup-time defaults. These don't affect the live
# values directly; they're only the starting point on a fresh install (no
# stored state yet) and what the "Reset" button restores every live value to
# (see coordinator.py's async_load_stored_state/async_reset). All fall back to
# the hardcoded DEFAULT_* constants below (or 0 for the day offset) when unset,
# so an install with none of these customized behaves exactly as before.
CONF_DEFAULT_REQUIRED_HOURS = "default_required_hours"
CONF_DEFAULT_GAMBLE_TOLERANCE = "default_gamble_tolerance"
CONF_DEFAULT_MAX_PRICE = "default_max_price"
CONF_DEFAULT_MIN_BLOCK_HOURS = "default_min_block_hours"
CONF_DEFAULT_READY_BY_HOUR = "default_ready_by_hour"
# How many days ahead of "as soon as possible" the default ready_by should
# land: 0 = next occurrence of default_ready_by_hour (today if not yet passed,
# otherwise tomorrow) — the "Next day" option; 1/2/3 push it at least that
# many days further out ("Next day + 1/2/3"). See scheduler.next_ready_by.
CONF_DEFAULT_READY_BY_DAY_OFFSET = "default_ready_by_day_offset"
DEFAULT_READY_BY_DAY_OFFSET = 0

# Config / options keys — which named provider (see providers.py) supplies each
# price source. "custom" unlocks the raw attribute/key fields below for a
# source not modelled as a named provider.
CONF_RATES_PROVIDER = "rates_provider"
CONF_FORECAST_PROVIDER = "forecast_provider"

# Config / options keys — price entity parsing. For a named provider these are
# filled in automatically from providers.py; for "custom" the user supplies
# them directly to point at an unsupported wholesale price source.
CONF_RATE_UNIT_MULTIPLIER = "rate_unit_multiplier"
CONF_RATES_ATTRIBUTE = "rates_attribute"
CONF_RATE_START_KEY = "rate_start_key"
CONF_RATE_VALUE_KEY = "rate_value_key"
CONF_FORECAST_UNIT_MULTIPLIER = "forecast_unit_multiplier"
CONF_FORECAST_ATTRIBUTE = "forecast_attribute"
CONF_FORECAST_DATETIME_KEY = "forecast_datetime_key"
CONF_FORECAST_PRICE_KEY = "forecast_price_key"

# Manual override for the charging_desired output — deliberately not tied to
# any charger-specific "work state" entity, so this integration doesn't need
# to know anything about your particular charger brand. Wire desired to
# whatever actually controls your charger (a switch, an API call, etc) and
# use this to force it on/off regardless of the computed schedule.
CHARGE_OVERRIDE_AUTO = "auto"
CHARGE_OVERRIDE_FORCE_ON = "force_on"
CHARGE_OVERRIDE_FORCE_OFF = "force_off"
DEFAULT_CHARGE_OVERRIDE = CHARGE_OVERRIDE_AUTO

DEFAULT_GAMBLE_TOLERANCE = 50.0
# The only block-length knob — a floor on how short a single charging block
# may be, to avoid rapidly cycling the charger. 0 means no minimum at all.
# There's deliberately no separate "max block hours": it only ever capped
# window *selection* size without guaranteeing an actual rest period (see
# the find_optimal_slots docstring in scheduler.py), which wasn't worth the
# extra knob.
DEFAULT_MIN_BLOCK_HOURS = 4.0
DEFAULT_MAX_PRICE = 20.0
DEFAULT_UPDATE_INTERVAL_MINUTES = 5
DEFAULT_REQUIRED_HOURS = 12.0
# A rough assumed energy draw per charging session, purely for the estimated
# cost sensor — this integration only ever knows time slots and price, never
# actual delivered kWh, so this is a stand-in the user sets to roughly match
# their car/charger.
DEFAULT_ASSUMED_CHARGE_KWH = 7.0

# ready_by has no fixed default — it rolls forward automatically. Both on
# first setup (no stored value yet) and whenever the current ready_by is
# reached, it's set to the next occurrence of this hour, local time — so
# "charge N hours by 7am" is a standing target that renews itself daily
# without needing to be reset manually. See scheduler.next_ready_by.
DEFAULT_READY_BY_HOUR = 7

DEFAULT_RATE_UNIT_MULTIPLIER = 100.0
DEFAULT_RATES_ATTRIBUTE = "rates"
DEFAULT_RATE_START_KEY = "start"
DEFAULT_RATE_VALUE_KEY = "value_inc_vat"
DEFAULT_FORECAST_UNIT_MULTIPLIER = 1.0
DEFAULT_FORECAST_ATTRIBUTE = "prices"
DEFAULT_FORECAST_DATETIME_KEY = "date_time"
DEFAULT_FORECAST_PRICE_KEY = "agile_pred"

STORAGE_VERSION = 1

STATE_IDLE = "idle"
STATE_SCHEDULED = "scheduled"
STATE_CHARGING = "charging"
STATE_BOOSTING = "boosting"
STATE_COMPLETE = "complete"
STATE_UNSCHEDULABLE = "unschedulable"
STATE_ERROR = "error"
