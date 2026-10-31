"""Data update coordinator for Wholesale EV Schedule."""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta

import homeassistant.util.dt as dt_util
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import slugify

from .const import (
    CHARGE_OVERRIDE_AUTO,
    CHARGE_OVERRIDE_FORCE_OFF,
    CHARGE_OVERRIDE_FORCE_ON,
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
    CONF_SHORT_RANGE_ATTRIBUTE,
    CONF_SHORT_RANGE_CONFIDENCE,
    CONF_SHORT_RANGE_DATETIME_KEY,
    CONF_SHORT_RANGE_ENTITY,
    CONF_SHORT_RANGE_ESTIMATE_FLAG_KEY,
    CONF_SHORT_RANGE_PRICE_KEY,
    CONF_SHORT_RANGE_PROVIDER,
    CONF_SHORT_RANGE_UNIT_MULTIPLIER,
    CONF_UPDATE_INTERVAL_MINUTES,
    DEFAULT_ASSUMED_CHARGE_KWH,
    DEFAULT_CHARGE_OVERRIDE,
    DEFAULT_FORECAST_ATTRIBUTE,
    DEFAULT_FORECAST_DATETIME_KEY,
    DEFAULT_FORECAST_PRICE_KEY,
    DEFAULT_FORECAST_UNIT_MULTIPLIER,
    DEFAULT_GAMBLE_TOLERANCE,
    DEFAULT_MAX_PRICE,
    DEFAULT_MIN_BLOCK_HOURS,
    DEFAULT_NAME,
    DEFAULT_OPPORTUNISTIC_MAX_PRICE,
    DEFAULT_OPPORTUNISTIC_OFFSET_DAYS,
    DEFAULT_OPPORTUNISTIC_TARGET_HOURS,
    DEFAULT_OPTIMIZATION_ALGORITHM,
    DEFAULT_RATE_START_KEY,
    DEFAULT_RATE_UNIT_MULTIPLIER,
    DEFAULT_RATE_VALUE_KEY,
    DEFAULT_RATES_ATTRIBUTE,
    DEFAULT_READY_BY_DAY_OFFSET,
    DEFAULT_READY_BY_HOUR,
    DEFAULT_REQUIRED_HOURS,
    DEFAULT_SHORT_RANGE_CONFIDENCE,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    OPTIMIZATION_ALGORITHM_GREEDY,
    OPTIMIZATION_ALGORITHM_HYBRID,
    OPTIMIZATION_ALGORITHM_OPTIMAL,
    STATE_BOOSTING,
    STATE_ERROR,
    STATE_IDLE,
    STATE_UNSCHEDULABLE,
    STORAGE_VERSION,
)
from .price_parsing import PointSpec, is_estimate, resolve_key
from .providers import (
    FORECAST_PROVIDER_AGILE_PREDICT,
    FORECAST_PROVIDERS,
    RATE_PROVIDER_OCTOPUS_ENERGY,
    RATE_PROVIDERS,
    SHORT_RANGE_PROVIDER_NONE,
    SHORT_RANGE_PROVIDERS,
)
from .scheduler import (
    SOURCE_CURRENT_ACTUAL,
    SOURCE_NEXT_ACTUAL,
    SOURCE_PREDICTED,
    SOURCE_SHORT_RANGE,
    SOURCE_SHORT_RANGE_FINAL,
    TIER_ACTUAL,
    add_local_days,
    assign_credibilities,
    clip_active_session,
    compute_hours_remaining,
    deduplicate_and_sort_prices,
    determine_state,
    find_optimal_slots,
    next_ready_by,
    parse_dt,
    prune_and_classify,
    slots_to_sessions,
    summarize_prices,
    to_utc,
)

_LOGGER = logging.getLogger(__name__)
_VALID_CHARGE_OVERRIDES = {CHARGE_OVERRIDE_AUTO, CHARGE_OVERRIDE_FORCE_ON, CHARGE_OVERRIDE_FORCE_OFF}
_VALID_OPTIMIZATION_ALGORITHMS = {
    OPTIMIZATION_ALGORITHM_GREEDY,
    OPTIMIZATION_ALGORITHM_OPTIMAL,
    OPTIMIZATION_ALGORITHM_HYBRID,
}

# Which charging tier a session dict belongs to (see _compute_sessions and
# _accrue_delivered_hours) -- distinct from scheduler.py's TIER_ACTUAL/
# TIER_PREDICTED_* price-credibility tiers, which mean something unrelated.
# SESSION_TIER_REQUIRED is also the implicit default for any session dict
# with no "tier" key at all (`.get("tier", SESSION_TIER_REQUIRED)`), which
# covers every session persisted by an install from before opportunistic
# charging existed -- an upgrade must never misroute or drop that state.
SESSION_TIER_REQUIRED = "required"
SESSION_TIER_OPPORTUNISTIC = "opportunistic"


def _non_blank_text(value) -> str | None:
    """`value` stripped, or None if it isn't a string or is blank -- for an optional
    text option that may be stored as "", whitespace, None, or (from a hand-edited
    entry) something that isn't text at all."""
    return value.strip() or None if isinstance(value, str) else None


class WholesaleEvScheduleCoordinator(DataUpdateCoordinator[dict]):
    """Reads wholesale price entities and computes the EV charging schedule.

    Ready-by time, hours-required, and the scheduling tolerances (gamble
    tolerance, min block length, max price) are all live, user-adjustable
    values exposed as `datetime`/`number` entities — they're the kind of thing
    you tweak day-to-day, not one-time setup. Only entity wiring, the chosen
    price-source provider, and the poll interval live in config-entry options.

    ready_by has no fixed default: it's set to the next occurrence of
    self._default_ready_by_hour on first setup, and automatically rolls
    forward to the next occurrence again once reached (see
    _async_update_data), so "charge N hours by 7am" renews itself daily
    without manual resetting.

    The self._default_* attributes below are read once at construction
    from the config entry's options (CONF_DEFAULT_*, see const.py) — they're
    the setup-time "sensible defaults" from issue #2: what a fresh install
    (no stored state yet) starts with, and what the "Reset" button restores
    every corresponding live value to. They deliberately aren't live values
    themselves; changing them via the options flow triggers a full reload
    (same as CONF_UPDATE_INTERVAL_MINUTES already does), which re-reads them
    here — it does not retroactively touch whatever the live values currently
    are.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        update_minutes = float(entry.options.get(CONF_UPDATE_INTERVAL_MINUTES, DEFAULT_UPDATE_INTERVAL_MINUTES))
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(minutes=update_minutes),
        )
        self.entry = entry
        self._calculating = False
        # tier -> (inputs key, selected date_times); see _solve. In-memory only.
        self._solve_cache: dict[str, tuple[tuple, frozenset]] = {}
        self._store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}_state")

        # Setup-time defaults (see class docstring) -- fall back to the
        # hardcoded DEFAULT_* constants / 0 so an install with none of these
        # customized behaves exactly as before.
        self._default_required_hours: float = float(
            entry.options.get(CONF_DEFAULT_REQUIRED_HOURS, DEFAULT_REQUIRED_HOURS)
        )
        self._default_gamble_tolerance: float = float(
            entry.options.get(CONF_DEFAULT_GAMBLE_TOLERANCE, DEFAULT_GAMBLE_TOLERANCE)
        )
        self._default_max_price: float = float(entry.options.get(CONF_DEFAULT_MAX_PRICE, DEFAULT_MAX_PRICE))
        self._default_min_block_hours: float = float(
            entry.options.get(CONF_DEFAULT_MIN_BLOCK_HOURS, DEFAULT_MIN_BLOCK_HOURS)
        )
        self._default_ready_by_hour: int = int(entry.options.get(CONF_DEFAULT_READY_BY_HOUR, DEFAULT_READY_BY_HOUR))
        self._default_ready_by_day_offset: int = int(
            entry.options.get(CONF_DEFAULT_READY_BY_DAY_OFFSET, DEFAULT_READY_BY_DAY_OFFSET)
        )
        # Opportunistic charging (issue #53) setup-time defaults -- same
        # "sensible defaults" role as the block above, just for the opt-in
        # second tier. See const.py's CONF_ENABLE_OPPORTUNISTIC_CHARGING block.
        self._default_opportunistic_offset_days: int = int(
            entry.options.get(CONF_DEFAULT_OPPORTUNISTIC_OFFSET_DAYS, DEFAULT_OPPORTUNISTIC_OFFSET_DAYS)
        )
        self._default_opportunistic_target_hours: float = float(
            entry.options.get(CONF_DEFAULT_OPPORTUNISTIC_TARGET_HOURS, DEFAULT_OPPORTUNISTIC_TARGET_HOURS)
        )
        self._default_opportunistic_max_price: float = float(
            entry.options.get(CONF_DEFAULT_OPPORTUNISTIC_MAX_PRICE, DEFAULT_OPPORTUNISTIC_MAX_PRICE)
        )

        self.ready_by: datetime | None = None
        self.required_hours: float = self._default_required_hours
        self.gamble_tolerance: float = self._default_gamble_tolerance
        self.min_block_hours: float = self._default_min_block_hours
        self.max_price: float = self._default_max_price
        self.charge_override: str = DEFAULT_CHARGE_OVERRIDE
        self.optimization_algorithm: str = DEFAULT_OPTIMIZATION_ALGORITHM
        self.assumed_charge_kwh: float = DEFAULT_ASSUMED_CHARGE_KWH

        # Opportunistic charging live state. opportunistic_ready_by has no
        # fixed default of its own -- like ready_by, it's set on first use and
        # re-derived every time ready_by rolls forward (see
        # _async_update_data): always `ready_by + default_opportunistic_offset_days`
        # at that moment. A live edit via async_set_opportunistic_ready_by
        # holds only until the next such rollover, then snaps back -- it is
        # deliberately NOT an independent value the way ready_by itself is.
        self.opportunistic_enabled: bool = False
        self.opportunistic_target_hours: float = self._default_opportunistic_target_hours
        self.opportunistic_max_price: float = self._default_opportunistic_max_price
        self.opportunistic_ready_by: datetime | None = None
        self._delivered_opportunistic_hours: float = 0.0

        self._stored_sessions: list[dict] = []
        self._boost_end: datetime | None = None
        self._delivered_hours: float = 0.0

    @property
    def instance_name(self) -> str:
        return self.entry.data.get(CONF_NAME, DEFAULT_NAME)

    @property
    def entity_prefix(self) -> str:
        """Slugified instance name — the entity_id prefix for this config entry.
        Uniqueness across instances is enforced at config-flow time via the
        entry's unique_id, so this is always collision-free."""
        return slugify(self.instance_name)

    @property
    def rates_provider_label(self) -> str:
        provider_id = self.entry.options.get(CONF_RATES_PROVIDER, RATE_PROVIDER_OCTOPUS_ENERGY)
        return RATE_PROVIDERS.get(provider_id, {}).get("label", provider_id)

    @property
    def forecast_provider_label(self) -> str:
        provider_id = self.entry.options.get(CONF_FORECAST_PROVIDER, FORECAST_PROVIDER_AGILE_PREDICT)
        return FORECAST_PROVIDERS.get(provider_id, {}).get("label", provider_id)

    @property
    def short_range_provider_label(self) -> str | None:
        """The configured short-range provider's label, or None when it isn't in use
        (so callers can leave it out entirely for an install that never opted in)."""
        if self._short_range_spec() is None:
            return None
        provider_id = self.entry.options.get(CONF_SHORT_RANGE_PROVIDER, SHORT_RANGE_PROVIDER_NONE)
        return SHORT_RANGE_PROVIDERS.get(provider_id, {}).get("label", provider_id)

    async def async_load_stored_state(self) -> None:
        """Restore live inputs and the in-progress schedule after a restart.
        ready_by defaults to the next self._default_ready_by_hour (at least
        self._default_ready_by_day_offset days out) if never set."""
        data = await self._store.async_load() or {}
        # Stored JSON is only guaranteed well-formed by HA's Store helper (which
        # handles outright corrupt/non-JSON files); a valid-JSON-but-wrong-shaped
        # value here (schema drift, a manual edit) is our own responsibility. A
        # malformed ready_by/boost_end degrades to "never set" rather than crashing
        # setup entirely.
        self.ready_by = self._parse_stored_dt(data.get("ready_by"), "ready_by")
        if self.ready_by is None:
            self.ready_by = next_ready_by(dt_util.now(), self._default_ready_by_hour, self._default_ready_by_day_offset)
        self.required_hours = self._parse_stored_float(
            data.get("required_hours"), self._default_required_hours, "required_hours"
        )
        self.gamble_tolerance = self._parse_stored_float(
            data.get("gamble_tolerance"), self._default_gamble_tolerance, "gamble_tolerance"
        )
        self.min_block_hours = self._parse_stored_float(
            data.get("min_block_hours"), self._default_min_block_hours, "min_block_hours"
        )
        self.max_price = self._parse_stored_float(data.get("max_price"), self._default_max_price, "max_price")
        self.charge_override = self._parse_stored_charge_override(data.get("charge_override"))
        self.optimization_algorithm = self._parse_stored_optimization_algorithm(data.get("optimization_algorithm"))
        self.assumed_charge_kwh = self._parse_stored_float(
            data.get("assumed_charge_kwh"), DEFAULT_ASSUMED_CHARGE_KWH, "assumed_charge_kwh"
        )
        self._stored_sessions = self._sanitize_stored_sessions(data.get("sessions", []))
        self._boost_end = self._parse_stored_dt(data.get("boost_end"), "boost_end")
        self._delivered_hours = self._parse_stored_float(data.get("delivered_hours"), 0.0, "delivered_hours")

        # Opportunistic charging state. A missing/invalid opportunistic_enabled
        # degrades to False (same "off by default" stance as a fresh install),
        # not raise -- a stored non-bool here (schema drift, a manual edit) is
        # our own responsibility same as every other _parse_stored_* helper.
        self.opportunistic_enabled = bool(data.get("opportunistic_enabled", False))
        self.opportunistic_target_hours = self._parse_stored_float(
            data.get("opportunistic_target_hours"),
            self._default_opportunistic_target_hours,
            "opportunistic_target_hours",
        )
        self.opportunistic_max_price = self._parse_stored_float(
            data.get("opportunistic_max_price"), self._default_opportunistic_max_price, "opportunistic_max_price"
        )
        self.opportunistic_ready_by = self._parse_stored_dt(
            data.get("opportunistic_ready_by"), "opportunistic_ready_by"
        )
        if self.opportunistic_ready_by is None:
            # Never stored (a fresh install, or an upgrade from before
            # opportunistic charging existed) -- seed it the same way it
            # would be derived on the next required-ready_by rollover
            # (_async_update_data), rather than leaving it None until that
            # rollover actually happens, which could be hours or days away.
            self.opportunistic_ready_by = add_local_days(self.ready_by, self._default_opportunistic_offset_days)
        self._delivered_opportunistic_hours = self._parse_stored_float(
            data.get("delivered_opportunistic_hours"), 0.0, "delivered_opportunistic_hours"
        )
        await self._async_save_stored_state()

    def _sanitize_stored_sessions(self, raw: object) -> list[dict]:
        """Drop any stored session that isn't a well-formed {start, end, ...} dict
        -- schema drift, a manual edit, or a partial/interrupted write could
        otherwise leave one that crashes prune_and_classify at coordinator-update
        time rather than at load time (issue #34)."""
        if not isinstance(raw, list):
            if raw:
                _LOGGER.warning("Stored sessions is not a list (%s); discarding", type(raw).__name__)
            return []
        sessions = []
        for s in raw:
            if not isinstance(s, dict):
                _LOGGER.warning("Discarding malformed stored session (not a dict): %r", s)
                continue
            try:
                parse_dt(s["start"])
                parse_dt(s["end"])
            except (KeyError, TypeError, ValueError) as err:
                _LOGGER.warning("Discarding malformed stored session (%s): %r", err, s)
                continue
            sessions.append(s)
        return sessions

    def _parse_stored_dt(self, value, field_name: str) -> datetime | None:
        """parse_dt a stored value, degrading to None (rather than raising) if it's
        present but not a parseable datetime -- see async_load_stored_state."""
        if not value:
            return None
        try:
            return parse_dt(value)
        except (TypeError, ValueError) as err:
            _LOGGER.warning("Stored %s %r is invalid (%s); treating as unset", field_name, value, err)
            return None

    def _parse_stored_float(self, value, default: float, field_name: str) -> float:
        """Coerce a stored numeric tuning value to float, degrading to `default`
        (rather than raising downstream, e.g. `if self.required_hours <= 0` or
        scheduler.py arithmetic on a str/None) if it's missing or not coercible
        -- see issue #35."""
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError) as err:
            _LOGGER.warning("Stored %s %r is invalid (%s); using default %s", field_name, value, err, default)
            return default

    def _parse_stored_charge_override(self, value: str | None) -> str:
        """Validate a stored charge_override against the enum, degrading to
        DEFAULT_CHARGE_OVERRIDE if it's anything else -- an unrecognized value
        would otherwise silently behave like "auto" (neither force branch in
        _with_diagnostics matches it) while still being reported back as the
        select entity's current_option, outside its declared options (#36)."""
        if value in _VALID_CHARGE_OVERRIDES:
            return value
        if value is not None:
            _LOGGER.warning("Stored charge_override %r is invalid; using default %s", value, DEFAULT_CHARGE_OVERRIDE)
        return DEFAULT_CHARGE_OVERRIDE

    def _parse_stored_optimization_algorithm(self, value: str | None) -> str:
        """Validate a stored optimization_algorithm against the enum, degrading to
        DEFAULT_OPTIMIZATION_ALGORITHM if it's anything else -- same reasoning as
        _parse_stored_charge_override (#36): an unrecognized value would otherwise
        silently fall through find_optimal_slots' own default (harmless, but
        reported back as the select entity's current_option outside its declared
        options)."""
        if value in _VALID_OPTIMIZATION_ALGORITHMS:
            return value
        if value is not None:
            _LOGGER.warning(
                "Stored optimization_algorithm %r is invalid; using default %s",
                value,
                DEFAULT_OPTIMIZATION_ALGORITHM,
            )
        return DEFAULT_OPTIMIZATION_ALGORITHM

    async def _async_save_stored_state(self) -> None:
        await self._store.async_save(
            {
                "ready_by": self.ready_by.isoformat() if self.ready_by else None,
                "required_hours": self.required_hours,
                "gamble_tolerance": self.gamble_tolerance,
                "min_block_hours": self.min_block_hours,
                "max_price": self.max_price,
                "charge_override": self.charge_override,
                "optimization_algorithm": self.optimization_algorithm,
                "assumed_charge_kwh": self.assumed_charge_kwh,
                "sessions": self._stored_sessions,
                "boost_end": self._boost_end.isoformat() if self._boost_end else None,
                "delivered_hours": self._delivered_hours,
                "opportunistic_enabled": self.opportunistic_enabled,
                "opportunistic_target_hours": self.opportunistic_target_hours,
                "opportunistic_max_price": self.opportunistic_max_price,
                "opportunistic_ready_by": self.opportunistic_ready_by.isoformat()
                if self.opportunistic_ready_by
                else None,
                "delivered_opportunistic_hours": self._delivered_opportunistic_hours,
            }
        )

    async def async_set_ready_by(self, value: datetime) -> None:
        self.ready_by = dt_util.as_local(value)
        await self._async_save_stored_state()
        await self._async_recalculate()

    def _reject_non_finite(self, value: float, field_name: str) -> None:
        """Raise if `value` isn't finite. HA's own number-entity min/max guard
        silently lets NaN through (`nan < min` and `nan > max` are both False),
        so this is the last line of defense before persisting it -- a
        persisted NaN serializes to JSON `null` via orjson, and on the next
        reload that becomes None and crashes downstream arithmetic, leaving
        the whole config entry stuck in ConfigEntryState.SETUP_RETRY (#37)."""
        if not math.isfinite(value):
            raise ServiceValidationError(f"{field_name} must be a finite number, got {value!r}")

    async def async_set_required_hours(self, value: float) -> None:
        self._reject_non_finite(value, "required_hours")
        self.required_hours = value
        # A new target starts fresh -- hours delivered under whatever the
        # previous required_hours was don't carry over and offset it.
        self._delivered_hours = 0.0
        await self._async_save_stored_state()
        await self._async_recalculate()

    async def async_set_opportunistic_enabled(self, value: bool) -> None:
        """Live pause/resume for opportunistic charging -- unlike the setup-time
        CONF_ENABLE_OPPORTUNISTIC_CHARGING toggle (which controls whether the
        entities exist at all), this doesn't touch opportunistic_target_hours
        or any other stored opportunistic value: off just means _compute_sessions
        treats the opportunistic target as 0 for this cycle, so re-enabling
        instantly resumes with everything intact."""
        self.opportunistic_enabled = value
        await self._async_save_stored_state()
        await self._async_recalculate()

    async def async_set_opportunistic_target_hours(self, value: float) -> None:
        self._reject_non_finite(value, "opportunistic_target_hours")
        self.opportunistic_target_hours = value
        # Same reasoning as async_set_required_hours: a new target starts
        # fresh, unoffset by whatever was delivered under the previous one.
        self._delivered_opportunistic_hours = 0.0
        await self._async_save_stored_state()
        await self._async_recalculate()

    async def async_set_opportunistic_max_price(self, value: float) -> None:
        self._reject_non_finite(value, "opportunistic_max_price")
        self.opportunistic_max_price = value
        await self._async_save_stored_state()
        await self._async_recalculate()

    async def async_set_opportunistic_ready_by(self, value: datetime) -> None:
        """Live-editable, but only as a one-cycle override: the next time the
        required ready_by itself rolls forward (see _async_update_data), this
        gets forcibly re-derived as `ready_by + default_opportunistic_offset_days`
        regardless of what was set here -- "unless it's been hand-modified, it
        always just +7 days" (the offset is configurable; 7 is only the
        out-of-the-box default), and a hand-modification only holds for the
        current cycle, not indefinitely."""
        self.opportunistic_ready_by = dt_util.as_local(value)
        await self._async_save_stored_state()
        await self._async_recalculate()

    async def async_set_gamble_tolerance(self, value: float) -> None:
        self._reject_non_finite(value, "gamble_tolerance")
        self.gamble_tolerance = value
        await self._async_save_stored_state()
        await self._async_recalculate()

    async def async_set_min_block_hours(self, value: float) -> None:
        self._reject_non_finite(value, "min_block_hours")
        self.min_block_hours = value
        await self._async_save_stored_state()
        await self._async_recalculate()

    async def async_set_max_price(self, value: float) -> None:
        self._reject_non_finite(value, "max_price")
        self.max_price = value
        await self._async_save_stored_state()
        await self._async_recalculate()

    async def async_set_assumed_charge_kwh(self, value: float) -> None:
        self._reject_non_finite(value, "assumed_charge_kwh")
        self.assumed_charge_kwh = value
        await self._async_save_stored_state()
        await self._async_recalculate()

    async def async_set_charge_override(self, value: str) -> None:
        self.charge_override = value
        await self._async_save_stored_state()
        await self._async_recalculate()

    async def async_set_optimization_algorithm(self, value: str) -> None:
        self.optimization_algorithm = value
        await self._async_save_stored_state()
        await self._async_recalculate()

    async def async_start_boost(self, duration_hours: float) -> None:
        # Absolute arithmetic (UTC) so a boost spanning a DST change lasts the hours asked for.
        self._boost_end = dt_util.as_local(dt_util.utcnow() + timedelta(hours=duration_hours))
        await self._async_save_stored_state()
        await self._async_recalculate()

    async def async_cancel_boost(self) -> None:
        self._boost_end = None
        await self._async_save_stored_state()
        await self._async_recalculate()

    async def async_stop(self) -> None:
        """Kill/end the current session: cancel today's schedule and any
        boost, and drop required_hours (and, symmetrically,
        opportunistic_target_hours) to 0 -- but keep ready_by/opportunistic_ready_by
        and every tuning preference (including opportunistic_max_price and
        whether opportunistic is enabled) untouched. Useful when you've
        decided not to charge today but the same deadlines and preferences
        still apply tomorrow."""
        self._stored_sessions = []
        self._boost_end = None
        self.required_hours = 0.0
        self._delivered_hours = 0.0
        self.opportunistic_target_hours = 0.0
        self._delivered_opportunistic_hours = 0.0
        await self._async_save_stored_state()
        await self._async_recalculate()

    async def async_reset(self) -> None:
        """Put everything back to the configured setup-time defaults (see
        class docstring): ready_by (next self._default_ready_by_hour, at
        least self._default_ready_by_day_offset days out), required_hours,
        gamble tolerance, min block hours, max price, assumed charge kWh, the
        charge override, the optimization algorithm, and -- symmetrically --
        opportunistic_target_hours/opportunistic_max_price back to their own
        setup-time defaults, and opportunistic_ready_by back to
        ready_by + default_opportunistic_offset_days, on top of clearing the
        schedule and any boost like async_stop does. Intended to be triggered
        by an automation on charger-unplugged, so the next plug-in starts
        from a completely clean slate rather than carrying over yesterday's
        tweaks.

        opportunistic_ready_by is re-derived directly here rather than left
        None for _async_update_data's rollover check to pick up next cycle --
        that check only fires when self.ready_by <= now_dt, which is never
        true immediately after this method sets ready_by to a fresh future
        value, so leaving it None would silently stop opportunistic
        scheduling (_compute_opportunistic_sessions short-circuits on a falsy
        opportunistic_ready_by) until the reset ready_by itself later expires
        -- the same gap async_load_stored_state's own derivation exists to
        avoid on a fresh install/restart.
        """
        self.ready_by = next_ready_by(dt_util.now(), self._default_ready_by_hour, self._default_ready_by_day_offset)
        self.required_hours = self._default_required_hours
        self.gamble_tolerance = self._default_gamble_tolerance
        self.min_block_hours = self._default_min_block_hours
        self.max_price = self._default_max_price
        self.assumed_charge_kwh = DEFAULT_ASSUMED_CHARGE_KWH
        self.charge_override = DEFAULT_CHARGE_OVERRIDE
        self.optimization_algorithm = DEFAULT_OPTIMIZATION_ALGORITHM
        self.opportunistic_target_hours = self._default_opportunistic_target_hours
        self.opportunistic_max_price = self._default_opportunistic_max_price
        self.opportunistic_ready_by = add_local_days(self.ready_by, self._default_opportunistic_offset_days)
        self._delivered_opportunistic_hours = 0.0
        self._stored_sessions = []
        self._boost_end = None
        self._delivered_hours = 0.0
        await self._async_save_stored_state()
        await self._async_recalculate()

    @property
    def _options(self) -> dict:
        return self.entry.options

    def _entity_state(self, entity_id: str | None):
        if not entity_id:
            return None
        entity = self.hass.states.get(entity_id)
        if not entity or entity.state in ("unknown", "unavailable", None, ""):
            return None
        return entity

    def _rates_spec(self) -> PointSpec:
        options = self._options
        return PointSpec(
            attribute=options.get(CONF_RATES_ATTRIBUTE, DEFAULT_RATES_ATTRIBUTE),
            time_key=options.get(CONF_RATE_START_KEY, DEFAULT_RATE_START_KEY),
            price_key=options.get(CONF_RATE_VALUE_KEY, DEFAULT_RATE_VALUE_KEY),
            multiplier=float(options.get(CONF_RATE_UNIT_MULTIPLIER, DEFAULT_RATE_UNIT_MULTIPLIER)),
        )

    def _forecast_spec(self) -> PointSpec:
        options = self._options
        return PointSpec(
            attribute=options.get(CONF_FORECAST_ATTRIBUTE, DEFAULT_FORECAST_ATTRIBUTE),
            time_key=options.get(CONF_FORECAST_DATETIME_KEY, DEFAULT_FORECAST_DATETIME_KEY),
            price_key=options.get(CONF_FORECAST_PRICE_KEY, DEFAULT_FORECAST_PRICE_KEY),
            multiplier=float(options.get(CONF_FORECAST_UNIT_MULTIPLIER, DEFAULT_FORECAST_UNIT_MULTIPLIER)),
        )

    def _short_range_spec(self) -> PointSpec | None:
        """The short-range forecast's parsing spec, or None when it isn't configured.

        "Configured" needs both a non-"none" provider and an entity: the options flow
        leaves stale short-range keys behind when switching back to "none" (the same
        way it does for the long-range forecast), and an entry created before this
        feature has no short-range keys at all -- both must read as simply off.
        """
        options = self._options
        provider = options.get(CONF_SHORT_RANGE_PROVIDER, SHORT_RANGE_PROVIDER_NONE)
        if provider == SHORT_RANGE_PROVIDER_NONE or not options.get(CONF_SHORT_RANGE_ENTITY):
            return None
        return PointSpec(
            attribute=options.get(CONF_SHORT_RANGE_ATTRIBUTE, DEFAULT_FORECAST_ATTRIBUTE),
            time_key=options.get(CONF_SHORT_RANGE_DATETIME_KEY, DEFAULT_FORECAST_DATETIME_KEY),
            price_key=options.get(CONF_SHORT_RANGE_PRICE_KEY, DEFAULT_FORECAST_PRICE_KEY),
            multiplier=float(options.get(CONF_SHORT_RANGE_UNIT_MULTIPLIER, DEFAULT_FORECAST_UNIT_MULTIPLIER)),
            estimate_flag_key=_non_blank_text(options.get(CONF_SHORT_RANGE_ESTIMATE_FLAG_KEY)),
        )

    def _short_range_base_credibility(self) -> float:
        """The configured short-range confidence as the 0-1 base credibility the
        scheduler wants. An unparseable stored value degrades to the default rather
        than failing the whole update; range clamping is the scheduler's job."""
        try:
            percent = float(self._options.get(CONF_SHORT_RANGE_CONFIDENCE, DEFAULT_SHORT_RANGE_CONFIDENCE))
        except (TypeError, ValueError):
            return DEFAULT_SHORT_RANGE_CONFIDENCE / 100.0
        return percent / 100.0 if math.isfinite(percent) else DEFAULT_SHORT_RANGE_CONFIDENCE / 100.0

    def _parse_points(self, entity_id: str | None, spec: PointSpec, source_for_point) -> list[dict]:
        """Read one price source into slot dicts. `source_for_point(point)` tags each
        slot with its scheduler.SOURCE_* label; malformed points are skipped (logged at
        debug), never fatal -- one bad row must not take the whole update down."""
        entity = self._entity_state(entity_id)
        if not entity:
            return []

        slots = []
        # `.get(attribute, [])`'s default only applies when the key is absent -- an
        # explicit `None` (a real pattern for "not yet populated" attributes) would
        # otherwise reach `for point in None:` and crash the whole coordinator update.
        for point in entity.attributes.get(spec.attribute) or []:
            try:
                time_val = resolve_key(point, spec.time_key)
                price_val = resolve_key(point, spec.price_key)
                if time_val is None or price_val is None:
                    continue
                slot_dt = dt_util.as_utc(parse_dt(time_val))
                raw_price = float(price_val)
                # float() accepts the strings "nan"/"inf" without error (unlike
                # "N/A", which correctly raises below) -- treat as malformed the
                # same way rather than silently poisoning downstream min/max/sum
                # diagnostics and ranking, since NaN's comparison semantics make
                # it not reliably sort to the back (issue #33).
                if not math.isfinite(raw_price):
                    raise ValueError(f"non-finite price: {price_val!r}")
                slots.append(
                    {
                        "date_time": slot_dt,
                        "raw_price": round(raw_price * spec.multiplier, 4),
                        "source": source_for_point(point),
                    }
                )
            except (TypeError, ValueError, AttributeError) as err:
                # AttributeError covers a non-dict row (e.g. the attribute shaped as
                # a dict instead of a list, so `point` is a plain str key) -- not
                # TypeError/ValueError (issue #32).
                _LOGGER.debug("Skipping malformed price point from %s: %s", entity_id, err)
        return slots

    def _parse_short_range(self) -> list[dict]:
        spec = self._short_range_spec()
        if spec is None:
            return []

        def source_for_point(point) -> str:
            if spec.estimate_flag_key is None or is_estimate(resolve_key(point, spec.estimate_flag_key)):
                return SOURCE_SHORT_RANGE
            return SOURCE_SHORT_RANGE_FINAL

        slots = self._parse_points(self._options.get(CONF_SHORT_RANGE_ENTITY), spec, source_for_point)
        # Only estimates carry the configured confidence; a point the source marks
        # final is actual-grade and ranks at full confidence like any real rate.
        confidence = self._short_range_base_credibility()
        for slot in slots:
            if slot["source"] == SOURCE_SHORT_RANGE:
                slot["base_credibility"] = confidence
        return slots

    def _collect_prices(self) -> list[dict]:
        options = self._options
        rates_spec = self._rates_spec()
        return (
            self._parse_points(options.get(CONF_CURRENT_RATES_ENTITY), rates_spec, lambda _p: SOURCE_CURRENT_ACTUAL)
            + self._parse_points(options.get(CONF_NEXT_RATES_ENTITY), rates_spec, lambda _p: SOURCE_NEXT_ACTUAL)
            + self._parse_points(options.get(CONF_FORECAST_ENTITY), self._forecast_spec(), lambda _p: SOURCE_PREDICTED)
            + self._parse_short_range()
        )

    def _accrue_delivered_hours(self, now_dt: datetime) -> None:
        """Credit self._delivered_hours/_delivered_opportunistic_hours with the
        duration of any stored session that has now fully ended, before
        prune_and_classify drops it for good -- routed by the session's own
        "tier" tag (see SESSION_TIER_REQUIRED/SESSION_TIER_OPPORTUNISTIC),
        defaulting missing/untagged sessions to required so state persisted by
        an install from before opportunistic charging existed still accrues
        correctly on upgrade.

        required_hours/opportunistic_target_hours have no other memory of
        charging completed in an earlier update cycle -- prune_and_classify
        only ever reports the *current* active/future split, so once a
        finished session ages out, nothing else records that it happened.
        Without this, _compute_sessions would see "no active session" and the
        target still at its original value and schedule a brand new session
        from scratch, even though the requirement was already met
        (hours_remaining bouncing back up after completion).
        """
        # Consuming what is counted (rather than leaving ended sessions in the list until
        # the next recompute replaces it) makes this idempotent: it is called both just
        # before ready_by rolls forward and again while computing the schedule, and a
        # session must never be credited twice.
        still_pending = []
        for s in self._stored_sessions:
            end = parse_dt(s["end"])
            if end > now_dt:
                still_pending.append(s)
                continue
            start = parse_dt(s["start"])
            duration = (end - start).total_seconds() / 3600
            if s.get("tier", SESSION_TIER_REQUIRED) == SESSION_TIER_OPPORTUNISTIC:
                self._delivered_opportunistic_hours += duration
            else:
                self._delivered_hours += duration
        self._stored_sessions = still_pending

    def _opportunistic_allowance_slots(self) -> int:
        """Half-hours of opportunistic charging still wanted: none while it's switched off, has
        no target, or has no deadline yet; otherwise the target less what has been delivered."""
        if not self.opportunistic_enabled or self.opportunistic_target_hours <= 0 or not self.opportunistic_ready_by:
            return 0
        wanted = max(1, math.ceil(self.opportunistic_target_hours * 2))
        return max(0, wanted - math.ceil(round(self._delivered_opportunistic_hours * 2, 6)))

    def _clip_active_session(self, active_session: dict | None, now_dt: datetime) -> dict | None:
        """Stop an in-progress block from outliving what its tier still wants.

        A session is planned once and carried forward untouched while it runs, so lowering the
        requirement, pausing opportunistic charging, or pulling a deadline earlier would
        otherwise change nothing until the block happened to end (see
        scheduler.clip_active_session). A session stopped here is credited with the hours it
        actually ran, exactly as if it had ended on its own.
        """
        if active_session is None:
            return None
        if active_session.get("tier", SESSION_TIER_REQUIRED) == SESSION_TIER_OPPORTUNISTIC:
            allowed, deadline = self._opportunistic_allowance_slots(), self.opportunistic_ready_by
        else:
            wanted = max(1, math.ceil(self.required_hours * 2))
            allowed, deadline = max(0, wanted - math.ceil(round(self._delivered_hours * 2, 6))), self.ready_by
        clipped, credit = clip_active_session(active_session, now_dt, allowed, deadline)
        if credit:
            if active_session.get("tier", SESSION_TIER_REQUIRED) == SESSION_TIER_OPPORTUNISTIC:
                self._delivered_opportunistic_hours += credit
            else:
                self._delivered_hours += credit
        return clipped

    def _compute_sessions(self, all_prices: list[dict], now_dt: datetime) -> list[dict]:
        self._accrue_delivered_hours(now_dt)
        active_session, _ = prune_and_classify(self._stored_sessions, now_dt)
        active_session = self._clip_active_session(active_session, now_dt)
        active_tier = active_session.get("tier", SESSION_TIER_REQUIRED) if active_session else None

        required_slots = max(1, math.ceil(self.required_hours * 2))
        delivered_slots = math.ceil(round(self._delivered_hours * 2, 6))
        slots_still_needed = max(0, required_slots - delivered_slots)

        now_prices = deduplicate_and_sort_prices(all_prices, now_dt)
        if not now_prices:
            return [active_session] if active_session else []

        adjusted = assign_credibilities(now_prices, now_dt, self.gamble_tolerance)
        if self.gamble_tolerance <= 0:
            adjusted = [s for s in adjusted if s["tier"] == TIER_ACTUAL]

        candidate_slots = adjusted
        if active_session:
            active_start = parse_dt(active_session["start"])
            active_end = parse_dt(active_session["end"])
            candidate_slots = [s for s in adjusted if not (active_start <= s["date_time"] < active_end)]
            if active_tier == SESSION_TIER_REQUIRED:
                # The active session's own remaining duration also counts
                # toward what's still needed, on top of delivered_slots
                # (which only covers sessions that have already fully ended).
                duration_h = active_session.get("duration_hours")
                if duration_h is None:
                    end = parse_dt(active_session["end"])
                    start = parse_dt(active_session["start"])
                    duration_h = (end - start).total_seconds() / 3600
                slots_still_needed = max(0, slots_still_needed - math.ceil(duration_h * 2))

        required_future_sessions = []
        required_future_dts: set = set()
        if slots_still_needed > 0:
            required_future_slots = self._solve(
                SESSION_TIER_REQUIRED,
                candidate_slots,
                slots_still_needed,
                self.ready_by,
                self.max_price,
            )
            required_future_dts = {s["date_time"] for s in required_future_slots}
            required_future_sessions = self._sessions_from_slots(required_future_slots, SESSION_TIER_REQUIRED)
        if active_session and active_tier == SESSION_TIER_REQUIRED:
            active_session["tier"] = SESSION_TIER_REQUIRED

        opportunistic_sessions = self._compute_opportunistic_sessions(
            candidate_slots, required_future_dts, active_session, active_tier, now_dt
        )

        return ([active_session] if active_session else []) + required_future_sessions + opportunistic_sessions

    def _solve(
        self,
        tier: str,
        candidate_slots: list[dict],
        required_slots: int,
        ready_by: datetime,
        max_price: float,
    ) -> list[dict]:
        """find_optimal_slots, skipped when nothing it reads has changed since this
        tier's previous call (issue #60).

        The wall-clock minute tick re-runs the whole schedule every minute, but the
        answer only moves when prices, settings or the 30-minute slot boundary do --
        and with the optimal/hybrid algorithms a solve can take hundreds of ms on a
        Pi, on the event loop. The key is everything the solver (and
        slots_to_sessions, via tier) reads: each candidate's time, raw and effective
        price and tier -- effective_price is in there so credibility tiers shifting
        as time passes invalidate it -- plus every parameter. One entry per tier
        (required/opportunistic) is enough: only the latest inputs can recur. Only the
        selected date_times are cached, and the result is rebuilt from the *current*
        candidate dicts, so a hit never hands back stale slot objects.
        """
        key = (
            required_slots,
            ready_by,
            self.min_block_hours,
            max_price,
            self.optimization_algorithm,
            tuple((s["date_time"], s["raw_price"], s["effective_price"], s["tier"]) for s in candidate_slots),
        )
        cached = self._solve_cache.get(tier)
        if cached is not None and cached[0] == key:
            return [s for s in candidate_slots if s["date_time"] in cached[1]]
        result = find_optimal_slots(
            candidate_slots,
            required_slots,
            ready_by,
            self.min_block_hours,
            max_price=max_price,
            algorithm=self.optimization_algorithm,
        )
        self._solve_cache[tier] = (key, frozenset(s["date_time"] for s in result))
        return result

    @staticmethod
    def _sessions_from_slots(slots: list[dict], tier: str) -> list[dict]:
        """Group selected slots into sessions tagged with their tier.

        All scheduling arithmetic is done on UTC instants (see scheduler.to_utc), but the
        schedule the user sees -- and the one persisted -- has always carried Home
        Assistant's local UTC offset, so start/end are rendered back into local time here.
        """
        sessions = slots_to_sessions(slots)
        for session in sessions:
            session["tier"] = tier
            for key in ("start", "end"):
                session[key] = dt_util.as_local(parse_dt(session[key])).isoformat()
        return sessions

    def _compute_opportunistic_sessions(
        self,
        candidate_slots: list[dict],
        required_future_dts: set,
        active_session: dict | None,
        active_tier: str | None,
        now_dt: datetime,
    ) -> list[dict]:
        """The opportunistic tier's own version of the future-session block in
        _compute_sessions above: nets opportunistic_target_hours against
        _delivered_opportunistic_hours (and the active session's own
        remaining duration, if it's an opportunistic one that's currently
        running), then searches for extra slots -- excluding whatever the
        required tier already claimed this cycle -- capped by
        opportunistic_ready_by and opportunistic_max_price instead of the
        required tier's own ready_by/max_price.

        Opportunistic never runs at all if disabled, has no target hours set,
        or has no opportunistic_ready_by yet (the very first cycle before any
        ready_by rollover has ever happened) -- required-tier scheduling above
        is completely unaffected either way, since this only ever consumes
        slots required did NOT claim.
        """
        if not self.opportunistic_enabled or self.opportunistic_target_hours <= 0 or not self.opportunistic_ready_by:
            return []

        opp_required_slots = max(1, math.ceil(self.opportunistic_target_hours * 2))
        opp_delivered_slots = math.ceil(round(self._delivered_opportunistic_hours * 2, 6))
        opp_slots_still_needed = max(0, opp_required_slots - opp_delivered_slots)

        opp_candidate_slots = candidate_slots
        if active_session and active_tier == SESSION_TIER_OPPORTUNISTIC:
            duration_h = active_session.get("duration_hours")
            if duration_h is None:
                end = parse_dt(active_session["end"])
                start = parse_dt(active_session["start"])
                duration_h = (end - start).total_seconds() / 3600
            opp_slots_still_needed = max(0, opp_slots_still_needed - math.ceil(duration_h * 2))

        if opp_slots_still_needed <= 0:
            return []

        opp_candidate_slots = [s for s in opp_candidate_slots if s["date_time"] not in required_future_dts]
        opp_slots = self._solve(
            SESSION_TIER_OPPORTUNISTIC,
            opp_candidate_slots,
            opp_slots_still_needed,
            self.opportunistic_ready_by,
            self.opportunistic_max_price,
        )
        return self._sessions_from_slots(opp_slots, SESSION_TIER_OPPORTUNISTIC)

    async def _async_recalculate(self) -> None:
        """Refresh in response to a user/automation edit, surfacing STATE_CALCULATING
        on the state sensor for the duration.

        The scheduler runs synchronously on the event loop, so without this a
        changed setting looks like nothing happened until the new schedule lands
        (noticeably long with the optimal algorithm on a long price horizon). The
        flag is cleared inside _async_update_data (always, via finally) rather than
        here, because DataUpdateCoordinator notifies listeners at the end of
        async_refresh -- clearing it afterwards would publish one more update
        still saying "calculating". The `sleep(0)` yields once so the state change
        is actually flushed to clients before the blocking computation starts.
        Only edits go through here: the periodic/minute-tick refreshes stay silent.
        """
        self._calculating = True
        self.async_update_listeners()
        await asyncio.sleep(0)
        try:
            await self.async_refresh()
        finally:
            self._calculating = False

    @property
    def calculating(self) -> bool:
        return self._calculating

    async def _async_update_data(self) -> dict:
        try:
            return await self._async_compute_data()
        finally:
            self._calculating = False

    async def _async_compute_data(self) -> dict:
        now_dt = dt_util.now()
        # Collected unconditionally (even when idle/erroring on the schedule
        # itself) so the market-data diagnostics stay populated regardless of
        # whether a charge is actually being planned right now.
        all_prices = self._collect_prices()
        price_summary = summarize_prices(all_prices, now_dt)

        if self._boost_end and to_utc(self._boost_end) > to_utc(now_dt):
            return self._boosting_result(now_dt, price_summary)
        if self._boost_end:
            self._boost_end = None

        # ready_by never just expires — once reached (or if somehow unset) it
        # rolls forward to the next self._default_ready_by_hour automatically,
        # so "charge N hours by 7am" is a standing target rather than
        # something that needs resetting by hand every day.
        if self.ready_by is None or to_utc(self.ready_by) <= to_utc(now_dt):
            # Credit anything that has just finished to the cycle it belongs to BEFORE the
            # counters reset. A session that ends exactly at ready_by (the cheapest window
            # often runs right up to the deadline) is first seen as ended on this very
            # refresh; crediting it after the reset would count it toward the NEW cycle
            # and the next requirement would read as already delivered -- no charging
            # at all on alternate days.
            self._accrue_delivered_hours(now_dt)
            self.ready_by = next_ready_by(now_dt, self._default_ready_by_hour, self._default_ready_by_day_offset)
            # A new ready_by cycle is a fresh "charge N hours by this deadline"
            # request -- hours delivered toward the previous deadline don't
            # carry over and offset the next one (see issue: hours_remaining
            # bouncing back up after a completed overnight session).
            self._delivered_hours = 0.0
            # opportunistic_ready_by always re-derives here, every time
            # required ready_by rolls forward -- this is the ONE place a live
            # edit to it (via async_set_opportunistic_ready_by) gets
            # overwritten. That's deliberate: a manual edit is a one-cycle
            # override, not a permanent switch to independence -- "unless
            # it's been hand-modified, it always just +N days" holds for
            # exactly one required-ready_by cycle at a time.
            self.opportunistic_ready_by = add_local_days(self.ready_by, self._default_opportunistic_offset_days)
            self._delivered_opportunistic_hours = 0.0
            await self._async_save_stored_state()

        if self.required_hours <= 0:
            self._stored_sessions = []
            await self._async_save_stored_state()
            return self._idle_result(now_dt, price_summary)

        if not all_prices:
            return self._error_result("No price data available from configured entities", now_dt, price_summary)

        sessions = self._compute_sessions(all_prices, now_dt)
        self._stored_sessions = sessions
        await self._async_save_stored_state()
        return self._schedule_result(sessions, now_dt, price_summary, all_prices)

    def _opportunistic_diagnostics(self, sessions: list[dict], now_dt: datetime) -> dict:
        """The opportunistic-tier equivalents of active_slot/next_slot/
        upcoming_slots/hours_remaining -- same prune_and_classify +
        compute_hours_remaining pair the combined (required + opportunistic)
        fields already use, just restricted to sessions tagged
        SESSION_TIER_OPPORTUNISTIC. `sessions` here is a list already
        containing both tiers (self._stored_sessions, or a freshly computed
        one) -- filtering happens here rather than requiring every call site
        to pre-filter."""
        opportunistic_sessions = [
            s for s in sessions if s.get("tier", SESSION_TIER_REQUIRED) == SESSION_TIER_OPPORTUNISTIC
        ]
        active, future = prune_and_classify(opportunistic_sessions, now_dt)
        return {
            "opportunistic_active_slot": active,
            "opportunistic_next_slot": future[0] if future else None,
            "opportunistic_upcoming_slots": future,
            "opportunistic_hours_remaining": compute_hours_remaining(future, active, now_dt),
        }

    def _with_diagnostics(self, result: dict, price_summary: dict) -> dict:
        result["price_summary"] = price_summary
        result.setdefault("boost_end", None)
        result.setdefault("upcoming_slots", [])
        result.setdefault("opportunistic_active_slot", None)
        result.setdefault("opportunistic_next_slot", None)
        result.setdefault("opportunistic_upcoming_slots", [])
        result.setdefault("opportunistic_hours_remaining", 0.0)
        result["block_count"] = len(result.get("sessions", []))

        # The manual override is the final word on `desired`, applied after
        # everything else — "force off" means off no matter what the schedule
        # or an active boost say, and vice versa for "force on".
        result["charge_override"] = self.charge_override
        if self.charge_override == CHARGE_OVERRIDE_FORCE_ON:
            result["desired"] = True
        elif self.charge_override == CHARGE_OVERRIDE_FORCE_OFF:
            result["desired"] = False
        return result

    def _idle_result(self, now_dt: datetime, price_summary: dict) -> dict:
        return self._with_diagnostics(
            {
                "state": STATE_IDLE,
                "desired": False,
                "sessions": [],
                "active_slot": None,
                "next_slot": None,
                "hours_remaining": 0.0,
                "error_reason": None,
                "calculated_at": now_dt.isoformat(),
            },
            price_summary,
        )

    def _error_result(self, reason: str, now_dt: datetime, price_summary: dict) -> dict:
        # A transient price-data gap (e.g. right after an HA restart, before
        # the price-source integration has finished loading) must not
        # silently drop an already-in-progress session -- see issue #31.
        # Compute active/future from stored state the same way
        # _boosting_result does, instead of unconditionally reporting
        # nothing in progress just because this cycle couldn't compute a
        # fresh schedule.
        active, future = prune_and_classify(self._stored_sessions, now_dt)
        hours_remaining = compute_hours_remaining(future, active, now_dt)
        return self._with_diagnostics(
            {
                "state": STATE_ERROR,
                "desired": active is not None,
                "sessions": self._stored_sessions,
                "active_slot": active,
                "next_slot": future[0] if future else None,
                "upcoming_slots": future,
                "hours_remaining": hours_remaining,
                "error_reason": reason,
                "calculated_at": now_dt.isoformat(),
                **self._opportunistic_diagnostics(self._stored_sessions, now_dt),
            },
            price_summary,
        )

    def _boosting_result(self, now_dt: datetime, price_summary: dict) -> dict:
        active, future = prune_and_classify(self._stored_sessions, now_dt)
        hours_remaining = compute_hours_remaining(future, active, now_dt)
        return self._with_diagnostics(
            {
                "state": STATE_BOOSTING,
                "desired": True,
                "sessions": self._stored_sessions,
                "active_slot": active,
                "next_slot": future[0] if future else None,
                "upcoming_slots": future,
                "hours_remaining": hours_remaining,
                "error_reason": None,
                "calculated_at": now_dt.isoformat(),
                "boost_end": self._boost_end.isoformat(),
                **self._opportunistic_diagnostics(self._stored_sessions, now_dt),
            },
            price_summary,
        )

    def _unschedulable_reason(self, now_dt: datetime, all_prices: list[dict]) -> str:
        """Best-effort diagnosis of *why* no valid schedule was found. Checked in
        order of how likely each is to be the actual cause: ready_by leaving less
        time than required, then not enough price data published yet before
        ready_by, falling back to the tolerances only once both of those are ruled
        out -- see issue #26 (this used to always blame tolerances regardless)."""
        hours_until_ready_by = (
            max(0.0, (to_utc(self.ready_by) - to_utc(now_dt)).total_seconds() / 3600) if self.ready_by else 0.0
        )
        if self.required_hours > hours_until_ready_by:
            ready_by_label = self.ready_by.isoformat() if self.ready_by else "unset"
            return (
                f"ready_by ({ready_by_label}) leaves only {hours_until_ready_by:.1f}h, "
                f"less than the {self.required_hours}h required"
            )

        required_slots = max(1, math.ceil(self.required_hours * 2))
        if self.ready_by:
            deadline = to_utc(self.ready_by)
            eligible_count = sum(1 for p in all_prices if to_utc(p["date_time"]) + timedelta(minutes=30) <= deadline)
        else:
            eligible_count = len(all_prices)
        if eligible_count < required_slots:
            return (
                f"Not enough price data published yet before ready_by: "
                f"{eligible_count} half-hour slots available, {required_slots} needed"
            )

        return (
            f"No slots satisfy constraints: {self.required_hours}h needed, "
            f"max_price={self.max_price}/kWh, min_block_hours={self.min_block_hours}h"
        )

    def _schedule_result(
        self, sessions: list[dict], now_dt: datetime, price_summary: dict, all_prices: list[dict]
    ) -> dict:
        active, future = prune_and_classify(sessions, now_dt)
        hours_remaining = compute_hours_remaining(future, active, now_dt)
        required_slots = max(1, math.ceil(self.required_hours * 2))
        delivered_slots = math.ceil(round(self._delivered_hours * 2, 6))
        already_fulfilled = delivered_slots >= required_slots

        if not sessions and active is None and self.required_hours > 0 and not already_fulfilled:
            reason = self._unschedulable_reason(now_dt, all_prices)
            return self._with_diagnostics(
                {
                    "state": STATE_UNSCHEDULABLE,
                    "desired": False,
                    "sessions": [],
                    "active_slot": None,
                    "next_slot": None,
                    # What is still outstanding -- not the full requirement again when part of
                    # it has already been delivered.
                    "hours_remaining": max(0.0, self.required_hours - self._delivered_hours),
                    "error_reason": reason,
                    "calculated_at": now_dt.isoformat(),
                    **self._opportunistic_diagnostics(sessions, now_dt),
                },
                price_summary,
            )

        sm_state = determine_state(
            required_hours=self.required_hours,
            boost_active=False,
            active_session=active,
            future_sessions=future,
            hours_remaining=hours_remaining,
            data_ok=True,
        )
        return self._with_diagnostics(
            {
                "state": sm_state,
                "desired": sm_state == "charging",
                "sessions": sessions,
                "active_slot": active,
                "next_slot": future[0] if future else None,
                "upcoming_slots": future,
                "hours_remaining": hours_remaining,
                "error_reason": None,
                "calculated_at": now_dt.isoformat(),
                **self._opportunistic_diagnostics(sessions, now_dt),
            },
            price_summary,
        )
