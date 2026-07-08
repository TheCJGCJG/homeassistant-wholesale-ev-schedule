"""Constants for the Wholesale EV Schedule integration."""
from homeassistant.const import Platform

DOMAIN = "wholesale_ev_schedule"
PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.DATETIME,
    Platform.BUTTON,
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
CONF_CHARGER_STATE_ENTITY = "charger_state_entity"
CONF_CHARGER_CONNECTED_STATES = "charger_connected_states"

# Config / options keys — timing only. Gamble tolerance, min/max block hours,
# and max price used to live here too, but those are the kind of thing you
# tweak day-to-day — they're now live `number` entities (see coordinator.py)
# rather than config-flow options that require a reload to change.
CONF_UPDATE_INTERVAL_MINUTES = "update_interval_minutes"

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

DEFAULT_CHARGER_CONNECTED_STATES = "charger_insert,charger_pause,charger_end,charger_charging,charger_wait"
DEFAULT_GAMBLE_TOLERANCE = 50.0
DEFAULT_MIN_BLOCK_HOURS = 1.0
# 0 is the "unlimited" sentinel — no cap on how long a single contiguous
# charging block may be, matching the pre-existing (pyscript) behaviour.
DEFAULT_MAX_BLOCK_HOURS = 0.0
DEFAULT_MAX_PRICE = 20.0
DEFAULT_UPDATE_INTERVAL_MINUTES = 5

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
