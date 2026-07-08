"""Data update coordinator for Wholesale EV Schedule."""
from __future__ import annotations

from datetime import datetime, timedelta
import logging
import math

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import slugify
import homeassistant.util.dt as dt_util

from .const import (
    CONF_CHARGER_CONNECTED_STATES,
    CONF_CHARGER_STATE_ENTITY,
    CONF_CURRENT_RATES_ENTITY,
    CONF_FORECAST_ATTRIBUTE,
    CONF_FORECAST_DATETIME_KEY,
    CONF_FORECAST_ENTITY,
    CONF_FORECAST_PRICE_KEY,
    CONF_FORECAST_UNIT_MULTIPLIER,
    CONF_GAMBLE_TOLERANCE,
    CONF_MAX_PRICE,
    CONF_MIN_BLOCK_HOURS,
    CONF_NEXT_RATES_ENTITY,
    CONF_RATE_START_KEY,
    CONF_RATE_UNIT_MULTIPLIER,
    CONF_RATE_VALUE_KEY,
    CONF_RATES_ATTRIBUTE,
    CONF_UPDATE_INTERVAL_MINUTES,
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
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    STATE_BOOSTING,
    STATE_ERROR,
    STATE_IDLE,
    STATE_UNSCHEDULABLE,
    STORAGE_VERSION,
)
from .scheduler import (
    TIER_ACTUAL,
    assign_credibilities,
    compute_hours_remaining,
    deduplicate_and_sort_prices,
    determine_state,
    find_optimal_slots,
    parse_dt,
    prune_and_classify,
    slots_to_sessions,
)

_LOGGER = logging.getLogger(__name__)


class WholesaleEvScheduleCoordinator(DataUpdateCoordinator[dict]):
    """Reads wholesale price entities and computes the EV charging schedule.

    Ready-by time and hours-required are live, user-adjustable values (exposed as
    `datetime`/`number` entities below) rather than config — they typically change
    once a day. Entity wiring and scheduling tolerances live in config-entry options.
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

        self.ready_by: datetime | None = None
        self.required_hours: float = 0.0

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

    async def async_load_stored_state(self) -> None:
        """Restore live inputs and the in-progress schedule after a restart."""
        data = await self._store.async_load() or {}
        self.ready_by = parse_dt(data["ready_by"]) if data.get("ready_by") else None
        self.required_hours = data.get("required_hours", 0.0)
        self._stored_sessions = data.get("sessions", [])
        self._boost_end = parse_dt(data["boost_end"]) if data.get("boost_end") else None

    async def _async_save_stored_state(self) -> None:
        await self._store.async_save({
            "ready_by": self.ready_by.isoformat() if self.ready_by else None,
            "required_hours": self.required_hours,
            "sessions": self._stored_sessions,
            "boost_end": self._boost_end.isoformat() if self._boost_end else None,
        })

    async def async_set_ready_by(self, value: datetime) -> None:
        self.ready_by = dt_util.as_local(value)
        await self._async_save_stored_state()
        await self.async_refresh()

    async def async_set_required_hours(self, value: float) -> None:
        self.required_hours = value
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
        self._stored_sessions = []
        self._boost_end = None
        self.required_hours = 0.0
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
        for rate in entity.attributes.get(attribute, []):
            try:
                dt_val = rate.get(start_key)
                price_val = rate.get(value_key)
                if dt_val is None or price_val is None:
                    continue
                slot_dt = dt_util.as_local(parse_dt(dt_val))
                slots.append({
                    "date_time": slot_dt,
                    "raw_price": round(float(price_val) * multiplier, 4),
                    "source": source_label,
                })
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
        for point in entity.attributes.get(attribute, []):
            try:
                dt_str = point.get(datetime_key)
                price_val = point.get(price_key)
                if dt_str is None or price_val is None:
                    continue
                slot_dt = dt_util.as_local(parse_dt(dt_str))
                slots.append({
                    "date_time": slot_dt,
                    "raw_price": round(float(price_val) * multiplier, 4),
                    "source": "predicted",
                })
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

    def _charger_connected(self) -> bool:
        options = self._options
        entity = self._entity_state(options.get(CONF_CHARGER_STATE_ENTITY))
        if not entity:
            return False
        connected_states = {
            s.strip() for s in options.get(CONF_CHARGER_CONNECTED_STATES, "").split(",") if s.strip()
        }
        return entity.state in connected_states

    def _compute_sessions(self, all_prices: list[dict], now_dt: datetime) -> list[dict]:
        options = self._options
        gamble_tolerance = float(options.get(CONF_GAMBLE_TOLERANCE, DEFAULT_GAMBLE_TOLERANCE))
        min_block_hours = float(options.get(CONF_MIN_BLOCK_HOURS, DEFAULT_MIN_BLOCK_HOURS))
        max_price = float(options.get(CONF_MAX_PRICE, DEFAULT_MAX_PRICE))

        active_session, _ = prune_and_classify(self._stored_sessions, now_dt)
        now_prices = deduplicate_and_sort_prices(all_prices, now_dt)
        if not now_prices:
            return [active_session] if active_session else []

        adjusted = assign_credibilities(now_prices, now_dt, gamble_tolerance)
        if gamble_tolerance <= 0:
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
                duration_h = (parse_dt(active_session["end"]) - parse_dt(active_session["start"])).total_seconds() / 3600
            slots_still_needed = max(0, required_slots - math.ceil(duration_h * 2))

        future_sessions = []
        if slots_still_needed > 0:
            future_slots = find_optimal_slots(
                candidate_slots, slots_still_needed, self.ready_by, min_block_hours, max_price=max_price
            )
            future_sessions = slots_to_sessions(future_slots)

        return ([active_session] if active_session else []) + future_sessions

    async def _async_update_data(self) -> dict:
        now_dt = dt_util.now()

        if self._boost_end and self._boost_end > now_dt:
            return self._boosting_result(now_dt)
        if self._boost_end:
            self._boost_end = None

        if self.ready_by is None or self.required_hours <= 0:
            self._stored_sessions = []
            await self._async_save_stored_state()
            return self._idle_result(now_dt)

        if self.ready_by <= now_dt:
            return self._error_result("Ready-by time is in the past", now_dt)

        all_prices = self._collect_prices()
        if not all_prices:
            return self._error_result("No price data available from configured entities", now_dt)

        sessions = self._compute_sessions(all_prices, now_dt)
        self._stored_sessions = sessions
        await self._async_save_stored_state()
        return self._schedule_result(sessions, now_dt)

    def _idle_result(self, now_dt: datetime) -> dict:
        return {
            "state": STATE_IDLE, "desired": False, "sessions": [], "active_slot": None,
            "next_slot": None, "hours_remaining": 0.0, "error_reason": None,
            "calculated_at": now_dt.isoformat(),
        }

    def _error_result(self, reason: str, now_dt: datetime) -> dict:
        return {
            "state": STATE_ERROR, "desired": False, "sessions": self._stored_sessions,
            "active_slot": None, "next_slot": None, "hours_remaining": 0.0,
            "error_reason": reason, "calculated_at": now_dt.isoformat(),
        }

    def _boosting_result(self, now_dt: datetime) -> dict:
        active, future = prune_and_classify(self._stored_sessions, now_dt)
        hours_remaining = compute_hours_remaining(future, active, now_dt)
        return {
            "state": STATE_BOOSTING, "desired": True, "sessions": self._stored_sessions,
            "active_slot": active, "next_slot": future[0] if future else None,
            "hours_remaining": hours_remaining, "error_reason": None,
            "calculated_at": now_dt.isoformat(),
        }

    def _schedule_result(self, sessions: list[dict], now_dt: datetime) -> dict:
        active, future = prune_and_classify(sessions, now_dt)
        hours_remaining = compute_hours_remaining(future, active, now_dt)
        options = self._options

        if not sessions and active is None and self.required_hours > 0:
            reason = (
                f"No slots satisfy constraints: {self.required_hours}h needed, "
                f"max_price={options.get(CONF_MAX_PRICE, DEFAULT_MAX_PRICE)}/kWh, "
                f"min_block_hours={options.get(CONF_MIN_BLOCK_HOURS, DEFAULT_MIN_BLOCK_HOURS)}h"
            )
            return {
                "state": STATE_UNSCHEDULABLE, "desired": False, "sessions": [],
                "active_slot": None, "next_slot": None, "hours_remaining": self.required_hours,
                "error_reason": reason, "calculated_at": now_dt.isoformat(),
            }

        sm_state = determine_state(
            required_hours=self.required_hours, boost_active=False, active_session=active,
            future_sessions=future, hours_remaining=hours_remaining, data_ok=True,
        )
        return {
            "state": sm_state,
            "desired": self._charger_connected() and sm_state == "charging",
            "sessions": sessions, "active_slot": active,
            "next_slot": future[0] if future else None,
            "hours_remaining": hours_remaining, "error_reason": None,
            "calculated_at": now_dt.isoformat(),
        }
