"""Data update coordinator for Wholesale EV Schedule."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta

import homeassistant.util.dt as dt_util
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import slugify

from .const import (
    CHARGE_OVERRIDE_FORCE_OFF,
    CHARGE_OVERRIDE_FORCE_ON,
    CONF_CURRENT_RATES_ENTITY,
    CONF_DEFAULT_GAMBLE_TOLERANCE,
    CONF_DEFAULT_MAX_PRICE,
    CONF_DEFAULT_MIN_BLOCK_HOURS,
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
    DEFAULT_RATE_START_KEY,
    DEFAULT_RATE_UNIT_MULTIPLIER,
    DEFAULT_RATE_VALUE_KEY,
    DEFAULT_RATES_ATTRIBUTE,
    DEFAULT_READY_BY_DAY_OFFSET,
    DEFAULT_READY_BY_HOUR,
    DEFAULT_REQUIRED_HOURS,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    STATE_BOOSTING,
    STATE_ERROR,
    STATE_IDLE,
    STATE_UNSCHEDULABLE,
    STORAGE_VERSION,
)
from .providers import FORECAST_PROVIDER_AGILE_PREDICT, FORECAST_PROVIDERS, RATE_PROVIDER_OCTOPUS_ENERGY, RATE_PROVIDERS
from .scheduler import (
    TIER_ACTUAL,
    assign_credibilities,
    compute_hours_remaining,
    deduplicate_and_sort_prices,
    determine_state,
    find_optimal_slots,
    next_ready_by,
    parse_dt,
    prune_and_classify,
    slots_to_sessions,
    summarize_prices,
)

_LOGGER = logging.getLogger(__name__)


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

    The six self._default_* attributes below are read once at construction
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

        self.ready_by: datetime | None = None
        self.required_hours: float = self._default_required_hours
        self.gamble_tolerance: float = self._default_gamble_tolerance
        self.min_block_hours: float = self._default_min_block_hours
        self.max_price: float = self._default_max_price
        self.charge_override: str = DEFAULT_CHARGE_OVERRIDE
        self.assumed_charge_kwh: float = DEFAULT_ASSUMED_CHARGE_KWH

        self._stored_sessions: list[dict] = []
        self._boost_end: datetime | None = None

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
        self.required_hours = data.get("required_hours", self._default_required_hours)
        self.gamble_tolerance = data.get("gamble_tolerance", self._default_gamble_tolerance)
        self.min_block_hours = data.get("min_block_hours", self._default_min_block_hours)
        self.max_price = data.get("max_price", self._default_max_price)
        self.charge_override = data.get("charge_override", DEFAULT_CHARGE_OVERRIDE)
        self.assumed_charge_kwh = data.get("assumed_charge_kwh", DEFAULT_ASSUMED_CHARGE_KWH)
        self._stored_sessions = data.get("sessions", [])
        self._boost_end = self._parse_stored_dt(data.get("boost_end"), "boost_end")
        await self._async_save_stored_state()

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

    async def _async_save_stored_state(self) -> None:
        await self._store.async_save(
            {
                "ready_by": self.ready_by.isoformat() if self.ready_by else None,
                "required_hours": self.required_hours,
                "gamble_tolerance": self.gamble_tolerance,
                "min_block_hours": self.min_block_hours,
                "max_price": self.max_price,
                "charge_override": self.charge_override,
                "assumed_charge_kwh": self.assumed_charge_kwh,
                "sessions": self._stored_sessions,
                "boost_end": self._boost_end.isoformat() if self._boost_end else None,
            }
        )

    async def async_set_ready_by(self, value: datetime) -> None:
        self.ready_by = dt_util.as_local(value)
        await self._async_save_stored_state()
        await self.async_refresh()

    async def async_set_required_hours(self, value: float) -> None:
        self.required_hours = value
        await self._async_save_stored_state()
        await self.async_refresh()

    async def async_set_gamble_tolerance(self, value: float) -> None:
        self.gamble_tolerance = value
        await self._async_save_stored_state()
        await self.async_refresh()

    async def async_set_min_block_hours(self, value: float) -> None:
        self.min_block_hours = value
        await self._async_save_stored_state()
        await self.async_refresh()

    async def async_set_max_price(self, value: float) -> None:
        self.max_price = value
        await self._async_save_stored_state()
        await self.async_refresh()

    async def async_set_assumed_charge_kwh(self, value: float) -> None:
        self.assumed_charge_kwh = value
        await self._async_save_stored_state()
        await self.async_refresh()

    async def async_set_charge_override(self, value: str) -> None:
        self.charge_override = value
        await self._async_save_stored_state()
        await self.async_refresh()

    async def async_start_boost(self, duration_hours: float) -> None:
        self._boost_end = dt_util.now() + timedelta(hours=duration_hours)
        await self._async_save_stored_state()
        await self.async_refresh()

    async def async_cancel_boost(self) -> None:
        self._boost_end = None
        await self._async_save_stored_state()
        await self.async_refresh()

    async def async_stop(self) -> None:
        """Kill/end the current session: cancel today's schedule and any
        boost, and drop required_hours to 0 (idle) — but keep ready_by and
        every tuning preference untouched. Useful when you've decided not to
        charge today but the same deadline and preferences still apply
        tomorrow."""
        self._stored_sessions = []
        self._boost_end = None
        self.required_hours = 0.0
        await self._async_save_stored_state()
        await self.async_refresh()

    async def async_reset(self) -> None:
        """Put everything back to the configured setup-time defaults (see
        class docstring): ready_by (next self._default_ready_by_hour, at
        least self._default_ready_by_day_offset days out), required_hours,
        gamble tolerance, min block hours, max price, assumed charge kWh, and
        the charge override, on top of clearing the schedule and any boost
        like async_stop does. Intended to be triggered by an automation on
        charger-unplugged, so the next plug-in starts from a completely clean
        slate rather than carrying over yesterday's tweaks."""
        self.ready_by = next_ready_by(dt_util.now(), self._default_ready_by_hour, self._default_ready_by_day_offset)
        self.required_hours = self._default_required_hours
        self.gamble_tolerance = self._default_gamble_tolerance
        self.min_block_hours = self._default_min_block_hours
        self.max_price = self._default_max_price
        self.assumed_charge_kwh = DEFAULT_ASSUMED_CHARGE_KWH
        self.charge_override = DEFAULT_CHARGE_OVERRIDE
        self._stored_sessions = []
        self._boost_end = None
        await self._async_save_stored_state()
        await self.async_refresh()

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

    def _parse_rate_entity(self, entity_id: str | None, source_label: str) -> list[dict]:
        entity = self._entity_state(entity_id)
        if not entity:
            return []
        options = self._options
        attribute = options.get(CONF_RATES_ATTRIBUTE, DEFAULT_RATES_ATTRIBUTE)
        start_key = options.get(CONF_RATE_START_KEY, DEFAULT_RATE_START_KEY)
        value_key = options.get(CONF_RATE_VALUE_KEY, DEFAULT_RATE_VALUE_KEY)
        multiplier = float(options.get(CONF_RATE_UNIT_MULTIPLIER, DEFAULT_RATE_UNIT_MULTIPLIER))

        slots = []
        # `.get(attribute, [])`'s default only applies when the key is absent -- an
        # explicit `None` (a real pattern for "not yet populated" attributes) would
        # otherwise reach `for rate in None:` and crash the whole coordinator update.
        for rate in entity.attributes.get(attribute) or []:
            try:
                dt_val = rate.get(start_key)
                price_val = rate.get(value_key)
                if dt_val is None or price_val is None:
                    continue
                slot_dt = dt_util.as_local(parse_dt(dt_val))
                slots.append(
                    {
                        "date_time": slot_dt,
                        "raw_price": round(float(price_val) * multiplier, 4),
                        "source": source_label,
                    }
                )
            except (TypeError, ValueError) as err:
                _LOGGER.debug("Skipping %s rate: %s", source_label, err)
        return slots

    def _parse_forecast_entity(self, entity_id: str | None) -> list[dict]:
        entity = self._entity_state(entity_id)
        if not entity:
            return []
        options = self._options
        attribute = options.get(CONF_FORECAST_ATTRIBUTE, DEFAULT_FORECAST_ATTRIBUTE)
        datetime_key = options.get(CONF_FORECAST_DATETIME_KEY, DEFAULT_FORECAST_DATETIME_KEY)
        price_key = options.get(CONF_FORECAST_PRICE_KEY, DEFAULT_FORECAST_PRICE_KEY)
        multiplier = float(options.get(CONF_FORECAST_UNIT_MULTIPLIER, DEFAULT_FORECAST_UNIT_MULTIPLIER))

        slots = []
        # See _parse_rate_entity -- `.get(attribute, [])`'s default only applies when
        # the key is absent, not when it's explicitly `None`.
        for point in entity.attributes.get(attribute) or []:
            try:
                dt_str = point.get(datetime_key)
                price_val = point.get(price_key)
                if dt_str is None or price_val is None:
                    continue
                slot_dt = dt_util.as_local(parse_dt(dt_str))
                slots.append(
                    {
                        "date_time": slot_dt,
                        "raw_price": round(float(price_val) * multiplier, 4),
                        "source": "predicted",
                    }
                )
            except (TypeError, ValueError) as err:
                _LOGGER.debug("Skipping predicted price: %s", err)
        return slots

    def _collect_prices(self) -> list[dict]:
        options = self._options
        return (
            self._parse_rate_entity(options.get(CONF_CURRENT_RATES_ENTITY), "current_actual")
            + self._parse_rate_entity(options.get(CONF_NEXT_RATES_ENTITY), "next_actual")
            + self._parse_forecast_entity(options.get(CONF_FORECAST_ENTITY))
        )

    def _compute_sessions(self, all_prices: list[dict], now_dt: datetime) -> list[dict]:
        active_session, _ = prune_and_classify(self._stored_sessions, now_dt)
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

        required_slots = max(1, math.ceil(self.required_hours * 2))
        slots_still_needed = required_slots
        if active_session:
            duration_h = active_session.get("duration_hours")
            if duration_h is None:
                end = parse_dt(active_session["end"])
                start = parse_dt(active_session["start"])
                duration_h = (end - start).total_seconds() / 3600
            slots_still_needed = max(0, required_slots - math.ceil(duration_h * 2))

        future_sessions = []
        if slots_still_needed > 0:
            future_slots = find_optimal_slots(
                candidate_slots,
                slots_still_needed,
                self.ready_by,
                self.min_block_hours,
                max_price=self.max_price,
            )
            future_sessions = slots_to_sessions(future_slots)

        return ([active_session] if active_session else []) + future_sessions

    async def _async_update_data(self) -> dict:
        now_dt = dt_util.now()
        # Collected unconditionally (even when idle/erroring on the schedule
        # itself) so the market-data diagnostics stay populated regardless of
        # whether a charge is actually being planned right now.
        all_prices = self._collect_prices()
        price_summary = summarize_prices(all_prices, now_dt)

        if self._boost_end and self._boost_end > now_dt:
            return self._boosting_result(now_dt, price_summary)
        if self._boost_end:
            self._boost_end = None

        # ready_by never just expires — once reached (or if somehow unset) it
        # rolls forward to the next self._default_ready_by_hour automatically,
        # so "charge N hours by 7am" is a standing target rather than
        # something that needs resetting by hand every day.
        if self.ready_by is None or self.ready_by <= now_dt:
            self.ready_by = next_ready_by(now_dt, self._default_ready_by_hour, self._default_ready_by_day_offset)
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

    def _with_diagnostics(self, result: dict, price_summary: dict) -> dict:
        result["price_summary"] = price_summary
        result.setdefault("boost_end", None)
        result.setdefault("upcoming_slots", [])
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
        return self._with_diagnostics(
            {
                "state": STATE_ERROR,
                "desired": False,
                "sessions": self._stored_sessions,
                "active_slot": None,
                "next_slot": None,
                "hours_remaining": 0.0,
                "error_reason": reason,
                "calculated_at": now_dt.isoformat(),
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
            },
            price_summary,
        )

    def _unschedulable_reason(self, now_dt: datetime, all_prices: list[dict]) -> str:
        """Best-effort diagnosis of *why* no valid schedule was found. Checked in
        order of how likely each is to be the actual cause: ready_by leaving less
        time than required, then not enough price data published yet before
        ready_by, falling back to the tolerances only once both of those are ruled
        out -- see issue #26 (this used to always blame tolerances regardless)."""
        hours_until_ready_by = max(0.0, (self.ready_by - now_dt).total_seconds() / 3600) if self.ready_by else 0.0
        if self.required_hours > hours_until_ready_by:
            ready_by_label = self.ready_by.isoformat() if self.ready_by else "unset"
            return (
                f"ready_by ({ready_by_label}) leaves only {hours_until_ready_by:.1f}h, "
                f"less than the {self.required_hours}h required"
            )

        required_slots = max(1, math.ceil(self.required_hours * 2))
        if self.ready_by:
            eligible_count = sum(1 for p in all_prices if p["date_time"] + timedelta(minutes=30) <= self.ready_by)
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

        if not sessions and active is None and self.required_hours > 0:
            reason = self._unschedulable_reason(now_dt, all_prices)
            return self._with_diagnostics(
                {
                    "state": STATE_UNSCHEDULABLE,
                    "desired": False,
                    "sessions": [],
                    "active_slot": None,
                    "next_slot": None,
                    "hours_remaining": self.required_hours,
                    "error_reason": reason,
                    "calculated_at": now_dt.isoformat(),
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
            },
            price_summary,
        )
