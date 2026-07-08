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

# All entity_ids are forced to start with this prefix (see entity.py) so this
# integration can run alongside another EV-charging setup (e.g. a pyscript-based
# one) on the same HA instance without ever colliding with its entity_ids.
ENTITY_ID_PREFIX = "wholesale_ev_schedule"

# Config / options keys — entity wiring.
CONF_CURRENT_RATES_ENTITY = "current_rates_entity"
CONF_NEXT_RATES_ENTITY = "next_rates_entity"
CONF_FORECAST_ENTITY = "forecast_entity"
CONF_CHARGER_STATE_ENTITY = "charger_state_entity"
CONF_CHARGER_CONNECTED_STATES = "charger_connected_states"

# Config / options keys — scheduling tolerances.
CONF_GAMBLE_TOLERANCE = "gamble_tolerance"
CONF_MIN_BLOCK_HOURS = "min_block_hours"
CONF_MAX_PRICE = "max_price"
CONF_UPDATE_INTERVAL_MINUTES = "update_interval_minutes"

# Config / options keys — price entity parsing. Defaults match the Octopus Energy
# integration's event-entity shape; override these to point at a different
# wholesale price source (Amber, Nordpool, a template sensor, etc).
CONF_RATE_UNIT_MULTIPLIER = "rate_unit_multiplier"
CONF_RATES_ATTRIBUTE = "rates_attribute"
CONF_RATE_START_KEY = "rate_start_key"
CONF_RATE_VALUE_KEY = "rate_value_key"
CONF_FORECAST_ATTRIBUTE = "forecast_attribute"
CONF_FORECAST_DATETIME_KEY = "forecast_datetime_key"
CONF_FORECAST_PRICE_KEY = "forecast_price_key"

DEFAULT_CHARGER_CONNECTED_STATES = "charger_insert,charger_pause,charger_end,charger_charging,charger_wait"
DEFAULT_GAMBLE_TOLERANCE = 50.0
DEFAULT_MIN_BLOCK_HOURS = 1.0
DEFAULT_MAX_PRICE = 20.0
DEFAULT_UPDATE_INTERVAL_MINUTES = 5

DEFAULT_RATE_UNIT_MULTIPLIER = 100.0
DEFAULT_RATES_ATTRIBUTE = "rates"
DEFAULT_RATE_START_KEY = "start"
DEFAULT_RATE_VALUE_KEY = "value_inc_vat"
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
