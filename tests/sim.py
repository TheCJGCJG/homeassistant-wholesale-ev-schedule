"""Deterministic day-in-the-life simulator for end-to-end tests.

Everything here is test infrastructure, not a test module. It models the parts of a real
install that unit tests can't: Home Assistant's timezone, Octopus Agile rates that only
become known at ~16:00 UK for the *next* agile day, a long-range forecast whose error grows
with distance, a day-ahead estimate that firms up from "prediction" to final, feed outages,
restarts that rebuild the coordinator from storage, settings edits, and a notional charger
that simply does whatever `charging_desired` says. After every step it asserts a set of
invariants that must hold in *every* state, and it records a timeline so a scenario can be
compared against a twin run (e.g. "with restarts" vs "without").

Prices are modelled in p/kWh (the integration's working unit). Actual rates are published
the way the Octopus integration exposes them (GBP/kWh, x100 by the integration).
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import homeassistant.util.dt as dt_util

from custom_components.wholesale_ev_schedule.const import (
    CONF_DEFAULT_READY_BY_HOUR,
    CONF_DEFAULT_REQUIRED_HOURS,
    DOMAIN,
)
from custom_components.wholesale_ev_schedule.scheduler import parse_dt

from .factories import (
    CURRENT_RATES_ENTITY,
    FORECAST_ENTITY,
    FULL_OPTIONS,
    NEXT_RATES_ENTITY,
    SHORT_RANGE_ENTITY,
    async_setup_wholesale_entry,
)

UTC = UTC
SLOT = timedelta(minutes=30)
STATE_SENSOR = "sensor.wholesale_ev_schedule_charging_state"
DESIRED_SENSOR = "binary_sensor.wholesale_ev_schedule_charging_desired"
VALID_STATES = {"idle", "scheduled", "charging", "boosting", "complete", "unschedulable", "error"}


# ---------------------------------------------------------------------------
# The price world
# ---------------------------------------------------------------------------


def _slot_index(dt: datetime) -> int:
    return int(dt.astimezone(UTC).timestamp() // 1800)


def floor_slot(dt: datetime) -> datetime:
    dt = dt.astimezone(UTC)
    return dt.replace(minute=0 if dt.minute < 30 else 30, second=0, microsecond=0)


def published_through(now: datetime) -> datetime:
    """When Agile rates run out: the agile day is 23:00Z -> 23:00Z and tomorrow's is published
    at ~15:00Z (16:00 UK). Before 15:00Z rates run to today's 23:00Z; after it, to tomorrow's."""
    now = now.astimezone(UTC)
    today_23 = now.replace(hour=23, minute=0, second=0, microsecond=0)
    return today_23 + timedelta(days=1) if now.hour >= 15 else today_23


@dataclass
class PriceWorld:
    """A seeded, repeatable market. `true_price` is what the slot will actually cost."""

    seed: int = 1
    trough: tuple[float, float] = (0.0, 5.5)  # UTC hours of the cheap window
    cheap: float = 9.0  # overnight trough, p/kWh
    mid: float = 24.0
    peak: float = 55.0
    noise: float = 2.5
    forecast_error_scale: float = 1.0  # 0 = a perfect forecast
    _memo: dict = field(default_factory=dict, repr=False, compare=False)  # deterministic prices, computed once

    def true_price(self, slot: datetime) -> float:
        key = ("true", self.seed, self.trough, self.cheap, self.mid, self.peak, self.noise, _slot_index(slot))
        if key not in self._memo:
            self._memo[key] = self._true_price(slot)
        return self._memo[key]

    def _true_price(self, slot: datetime) -> float:
        hour = slot.astimezone(UTC).hour + slot.astimezone(UTC).minute / 60
        if self.trough[0] <= hour < self.trough[1]:
            base = self.cheap
        elif hour < 16:
            base = self.mid
        elif hour < 19:
            base = self.peak
        else:
            base = self.mid
        jitter = random.Random(self.seed * 1_000_003 + _slot_index(slot)).uniform(-self.noise, self.noise)
        return round(max(0.5, base + jitter), 2)

    def _error(self, slot: datetime, issued: datetime, amplitude: float) -> float:
        rng = random.Random(self.seed * 7_919 + _slot_index(slot) * 31 + _slot_index(issued))
        return rng.uniform(-amplitude, amplitude)

    def forecast_price(self, slot: datetime, now: datetime) -> float:
        issued_idx = int(now.astimezone(UTC).timestamp() // (6 * 3600))
        key = ("fc", self.seed, self.forecast_error_scale, _slot_index(slot), issued_idx, slot < published_through(now))
        if key not in self._memo:
            self._memo[key] = self._forecast_price(slot, now)
        return self._memo[key]

    def _forecast_price(self, slot: datetime, now: datetime) -> float:
        """Long-range forecast: issued every 6h, near-exact for already-published slots,
        drifting by up to ~12p at long range."""
        issued = now.astimezone(UTC).replace(hour=now.astimezone(UTC).hour // 6 * 6, minute=0, second=0, microsecond=0)
        # Relative to the issue time, not "now": a forecast is a fixed document until the next
        # issue, so republishing mid-block (e.g. after a restart) must show identical numbers.
        horizon_h = max(0.0, (slot - issued).total_seconds() / 3600)
        if slot < published_through(now):
            return self.true_price(slot)
        amplitude = min(12.0, 1.0 + horizon_h * 0.12) * self.forecast_error_scale
        return round(max(0.5, self.true_price(slot) + self._error(slot, issued, amplitude)), 2)

    def short_range_price(self, slot: datetime) -> float:
        return round(max(0.5, self.true_price(slot) + self._error(slot, slot, 1.5)), 2)

    # -- what each feed shows at `now` ------------------------------------------------------

    def actual_slots(self, now: datetime) -> list[tuple[datetime, float]]:
        """All published Agile rates from the start of today's agile day to published_through."""
        now = now.astimezone(UTC)
        day_start = now.replace(hour=23, minute=0, second=0, microsecond=0)
        if now.hour < 23:
            day_start -= timedelta(days=1)
        end = published_through(now)
        slots, t = [], day_start
        while t < end:
            slots.append((t, self.true_price(t)))
            t += SLOT
        return slots

    def forecast_slots(self, now: datetime, days: int = 10) -> list[tuple[datetime, float]]:
        slot = floor_slot(now)
        return [(slot + SLOT * i, self.forecast_price(slot + SLOT * i, now)) for i in range(days * 48)]

    def short_range_slots(self, now: datetime) -> tuple[list[tuple[datetime, float]], list[bool]]:
        """From ~10:00Z the next agile day's estimates appear (prediction=True); from ~15:00Z the
        same slots are final (prediction=False). Before 10:00Z there is nothing beyond what is
        already published."""
        now = now.astimezone(UTC)
        if now.hour < 10:
            return [], []
        window_start = now.replace(hour=23, minute=0, second=0, microsecond=0)
        window = [window_start + SLOT * i for i in range(48)]
        final = now.hour >= 15
        prices = [self.true_price(s) if final else self.short_range_price(s) for s in window]
        return list(zip(window, prices, strict=True)), [not final] * len(window)


# ---------------------------------------------------------------------------
# How a feed's points are laid out in the entity (so any provider combination can be fed)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Shape:
    """Where a source keeps its points: `attribute` holds the list; each point has the slot's start
    under `time_key` and its price (p/kWh x `scale`) under `price_key`. Keys may be dotted paths into
    nested data. `flag_key`, if set, carries the day-ahead "still a prediction" boolean."""

    attribute: str
    time_key: str
    price_key: str
    scale: float = 1.0
    flag_key: str | None = None
    z_suffix: bool = False  # render UTC as "...Z" instead of "...+00:00"
    entity_state: str = "populated"


OCTOPUS_SHAPE = Shape("rates", "start", "value_inc_vat", scale=0.01)  # GBP/kWh, as the Octopus integration reports
AGILE_PREDICT_SHAPE = Shape("prices", "date_time", "agile_pred")
AGILE_RATES_SHAPE = Shape(
    "rates",
    "deliveryStart",
    "agileRate.result.rate",
    flag_key="agileRate.result.prediction",
    z_suffix=True,
    entity_state="ok",
)


def _set_path(target: dict, path: str, value) -> None:
    *parents, leaf = path.split(".")
    for part in parents:
        target = target.setdefault(part, {})
    target[leaf] = value


def points_for(shape: Shape, slots: list[tuple[datetime, float]], flags: list[bool] | None = None) -> list[dict]:
    """The attribute list a source with this shape would expose for these (time, p/kWh) slots."""
    points = []
    for i, (when, price) in enumerate(slots):
        utc = when.astimezone(UTC)
        stamp = utc.strftime("%Y-%m-%dT%H:%M:%SZ") if shape.z_suffix else utc.isoformat()
        point: dict = {}
        _set_path(point, shape.time_key, stamp)
        _set_path(point, shape.price_key, round(price * shape.scale, 6))
        if shape.flag_key is not None and flags is not None:
            _set_path(point, shape.flag_key, flags[i])
        points.append(point)
    return points


# ---------------------------------------------------------------------------
# One observation of the system
# ---------------------------------------------------------------------------


@dataclass
class Tick:
    at: datetime  # UTC
    state: str
    desired: bool
    hours_remaining: float
    sessions: list[dict]
    delivered_hours: float
    ready_by: datetime
    required_hours: float
    charge_override: str


@dataclass
class Sim:
    hass: object
    freezer: object
    world: PriceWorld
    entry: object
    tz: str
    feeds: set = field(default_factory=lambda: {"actual", "forecast"})
    shapes: dict = field(
        default_factory=lambda: {
            "actual": OCTOPUS_SHAPE,
            "forecast": AGILE_PREDICT_SHAPE,
            "short_range": AGILE_RATES_SHAPE,
        }
    )
    outages: set = field(default_factory=set)
    timeline: list[Tick] = field(default_factory=list)
    charged_minutes: dict = field(default_factory=dict)  # ready_by iso -> minutes the charger was on
    cost: float = 0.0  # p, using true prices
    step_minutes: int = 5
    _published_key: tuple = None
    setting_edits: bool = False  # set by scenarios right before a deliberate edit

    # -- construction -----------------------------------------------------------------------

    @classmethod
    async def create(
        cls,
        hass,
        freezer,
        *,
        start="2026-03-02 12:00:00+00:00",
        tz="UTC",
        options=None,
        world=None,
        feeds=("actual", "forecast"),
        required_hours=2.0,
        min_block_hours=0.5,
        max_price=100.0,
        ready_hour=7,
        step_minutes=5,
        configure=True,
        shapes=None,
    ):
        await hass.config.async_set_time_zone(tz)
        freezer.move_to(start)
        sim = cls(hass, freezer, world or PriceWorld(), None, tz, feeds=set(feeds), step_minutes=step_minutes)
        if shapes:
            sim.shapes = {**sim.shapes, **shapes}
        sim.publish(force=True)
        opts = {**(options if options is not None else FULL_OPTIONS)}
        opts[CONF_DEFAULT_READY_BY_HOUR] = ready_hour
        opts[CONF_DEFAULT_REQUIRED_HOURS] = 0.0 if configure else required_hours
        sim.entry = await async_setup_wholesale_entry(hass, options=opts)
        if configure:
            c = sim.coordinator
            await c.async_set_min_block_hours(min_block_hours)
            await c.async_set_max_price(max_price)
            await c.async_set_required_hours(required_hours)
        # The state right after setup is the first observation, so the charger being on already
        # when the simulation begins is accounted for from the first step.
        sim.timeline.append(sim._observe())
        check_invariants(sim, sim.timeline[-1])
        return sim

    # -- accessors --------------------------------------------------------------------------

    @property
    def coordinator(self):
        return self.hass.data[DOMAIN][self.entry.entry_id]

    @property
    def now(self) -> datetime:
        return dt_util.utcnow()

    @property
    def data(self) -> dict:
        return self.coordinator.data

    # -- feeds ------------------------------------------------------------------------------

    def publish(self, force: bool = False) -> None:
        """Push the world's current view into the HA entities the integration reads. Only
        rewrites an entity when what it should show changed, so a long run stays fast."""
        now = self.now
        key = (
            published_through(now),
            now.hour // 6,
            now.date(),
            now.hour >= 10,
            now.hour >= 15,
            floor_slot(now),
            frozenset(self.feeds),
            frozenset(self.outages),
        )
        if not force and key == self._published_key:
            return
        self._published_key = key

        hass = self.hass
        if "actual" in self.feeds:
            if "actual" in self.outages:
                hass.states.async_set(CURRENT_RATES_ENTITY, "unavailable")
                hass.states.async_set(NEXT_RATES_ENTITY, "unavailable")
            else:
                shape = self.shapes["actual"]
                slots = self.world.actual_slots(now)
                boundary = now.replace(hour=23, minute=0, second=0, microsecond=0)
                if now.hour >= 23:
                    boundary += timedelta(days=1)
                current = [(t, p) for t, p in slots if t < boundary]
                following = [(t, p) for t, p in slots if t >= boundary]
                for entity, part in ((CURRENT_RATES_ENTITY, current), (NEXT_RATES_ENTITY, following)):
                    hass.states.async_set(entity, shape.entity_state, {shape.attribute: points_for(shape, part)})
        if "forecast" in self.feeds:
            if "forecast" in self.outages:
                hass.states.async_set(FORECAST_ENTITY, "unavailable")
            else:
                shape = self.shapes["forecast"]
                hass.states.async_set(
                    FORECAST_ENTITY,
                    shape.entity_state,
                    {shape.attribute: points_for(shape, self.world.forecast_slots(now))},
                )
        if "short_range" in self.feeds:
            if "short_range" in self.outages:
                hass.states.async_set(SHORT_RANGE_ENTITY, "unavailable")
            else:
                shape = self.shapes["short_range"]
                slots, flags = self.world.short_range_slots(now)
                hass.states.async_set(
                    SHORT_RANGE_ENTITY, shape.entity_state, {shape.attribute: points_for(shape, slots, flags)}
                )

    # -- time -------------------------------------------------------------------------------

    async def step(self, minutes: int | None = None, *, check: bool = True) -> Tick:
        minutes = self.step_minutes if minutes is None else minutes
        was_on = self.timeline[-1].desired if self.timeline else False
        before = self.now
        self.freezer.move_to(before + timedelta(minutes=minutes))
        self.publish()
        await self.coordinator.async_refresh()
        await self.hass.async_block_till_done()
        tick = self._observe()
        # The charger is on for the interval just elapsed if it was on at its start (a controller
        # reacts to the state it last saw), which is how a real switch behaves.
        if was_on:
            # Belongs to the cycle that was running when the interval began, not the one
            # that may have just rolled over at its end.
            key = (self.timeline[-1].ready_by if self.timeline else tick.ready_by).isoformat()
            self.charged_minutes[key] = self.charged_minutes.get(key, 0) + minutes
            self.cost += self.world.true_price(floor_slot(before)) * minutes / 60.0 * 1.0
        self.timeline.append(tick)
        if check:
            check_invariants(self, tick)
        self.setting_edits = False
        return tick

    async def act(self, coro) -> Tick:
        """Await a deliberate user action (a setter, a button) and record the state right after
        it, at the same instant. Relaxes the "plan is current" invariants for this tick and the
        next, since an edit can legitimately leave an in-progress session untouched."""
        self.setting_edits = True
        await coro
        await self.hass.async_block_till_done()
        tick = self._observe()
        self.timeline.append(tick)
        check_invariants(self, tick)
        return tick

    async def run(self, minutes: int, *, step: int | None = None, until=None) -> None:
        step = step or self.step_minutes
        elapsed = 0
        while elapsed < minutes:
            await self.step(step)
            elapsed += step
            if until and until(self):
                return

    async def run_to(self, target: datetime, *, step: int | None = None) -> None:
        target = target.astimezone(UTC)
        while self.now < target:
            gap = int((target - self.now).total_seconds() // 60)
            await self.step(min(step or self.step_minutes, gap))

    # -- control ----------------------------------------------------------------------------

    async def restart(self) -> None:
        """A genuine restart: tear the entry down and set it up again (a brand-new coordinator)."""
        assert await self.hass.config_entries.async_unload(self.entry.entry_id)
        await self.hass.async_block_till_done()
        self.publish(force=True)
        assert await self.hass.config_entries.async_setup(self.entry.entry_id)
        await self.hass.async_block_till_done()

    def outage(self, feed: str, on: bool = True) -> None:
        (self.outages.add if on else self.outages.discard)(feed)
        self.publish(force=True)

    # -- observation ------------------------------------------------------------------------

    def _observe(self) -> Tick:
        c = self.coordinator
        data = c.data
        return Tick(
            at=self.now,
            state=data["state"],
            desired=bool(data["desired"]),
            hours_remaining=float(data["hours_remaining"]),
            sessions=json.loads(json.dumps(data["sessions"])),
            delivered_hours=c._delivered_hours,
            ready_by=c.ready_by,
            required_hours=c.required_hours,
            charge_override=c.charge_override,
        )

    def snapshot(self) -> dict:
        """Everything a user can observe or a restart must preserve, as plain comparable data."""
        c = self.coordinator
        return {
            "state": c.data["state"],
            "desired": c.data["desired"],
            "sessions": json.loads(json.dumps(c.data["sessions"])),
            "hours_remaining": c.data["hours_remaining"],
            "error_reason": c.data["error_reason"],
            "boost_end": c.data["boost_end"],
            "ready_by": c.ready_by.isoformat(),
            "required_hours": c.required_hours,
            "gamble_tolerance": c.gamble_tolerance,
            "min_block_hours": c.min_block_hours,
            "max_price": c.max_price,
            "assumed_charge_kwh": c.assumed_charge_kwh,
            "charge_override": c.charge_override,
            "optimization_algorithm": c.optimization_algorithm,
            "delivered_hours": c._delivered_hours,
            "opportunistic_enabled": c.opportunistic_enabled,
            "opportunistic_target_hours": c.opportunistic_target_hours,
            "opportunistic_max_price": c.opportunistic_max_price,
            "opportunistic_ready_by": c.opportunistic_ready_by.isoformat() if c.opportunistic_ready_by else None,
            "delivered_opportunistic_hours": c._delivered_opportunistic_hours,
        }

    # -- outcome helpers --------------------------------------------------------------------

    def charged_hours(self, ready_by: datetime) -> float:
        return self.charged_minutes.get(ready_by.isoformat(), 0) / 60.0

    def on_minutes(self, start: datetime, end: datetime) -> int:
        """Minutes the charger was on within [start, end], measured over the recorded ticks."""
        total = 0
        for prev, cur in zip(self.timeline, self.timeline[1:], strict=False):
            if prev.desired and prev.at >= start and cur.at <= end:
                total += int((cur.at - prev.at).total_seconds() // 60)
        return total

    def desired_series(self) -> list[tuple[datetime, bool]]:
        return [(t.at, t.desired) for t in self.timeline]


# ---------------------------------------------------------------------------
# Invariants: must hold at every step, in every state
# ---------------------------------------------------------------------------


def _intervals(sessions):
    return [(parse_dt(s["start"]), parse_dt(s["end"]), s) for s in sessions]


def check_invariants(sim: Sim, tick: Tick) -> None:
    c = sim.coordinator
    now = tick.at
    ctx = f"[{now.isoformat()} state={tick.state} desired={tick.desired}]"

    assert c.last_update_success, f"{ctx} coordinator update failed"
    assert tick.state in VALID_STATES, f"{ctx} unknown state"

    # What HA shows is what the coordinator computed.
    ha_state = sim.hass.states.get(STATE_SENSOR)
    ha_desired = sim.hass.states.get(DESIRED_SENSOR)
    assert ha_state is not None and ha_state.state == tick.state, f"{ctx} state sensor {ha_state}"
    assert ha_desired is not None and ha_desired.state == ("on" if tick.desired else "off"), f"{ctx} desired sensor"

    intervals = _intervals(tick.sessions)
    for start, end, s in intervals:
        assert end > start, f"{ctx} empty/negative session {s}"
        hours = (end - start).total_seconds() / 3600
        assert abs(hours - s["duration_hours"]) < 1e-6, f"{ctx} duration_hours mismatch {s}"
        assert hours * 2 == int(hours * 2), f"{ctx} session not a whole number of half hours {s}"
        assert start.astimezone(UTC).minute % 30 == 0, f"{ctx} session not on the half-hour grid {s}"
    for (_, end_a, _), (start_b, _, _) in zip(intervals, intervals[1:], strict=False):
        assert end_a <= start_b, f"{ctx} overlapping/unsorted sessions {tick.sessions}"

    active = [iv for iv in intervals if iv[0] <= now < iv[1]]
    assert len(active) <= 1, f"{ctx} more than one active session"

    # desired follows the state (or the override, which has the final word)
    if tick.charge_override == "force_on":
        assert tick.desired is True, f"{ctx} force_on but not desired"
    elif tick.charge_override == "force_off":
        assert tick.desired is False, f"{ctx} force_off but desired"
    else:
        if tick.state in ("charging", "boosting"):
            assert tick.desired is True, f"{ctx} {tick.state} but not desired"
        elif tick.state == "error":
            assert tick.desired == bool(active), f"{ctx} error state desired must mirror an in-progress session"
        else:
            assert tick.desired is False, f"{ctx} {tick.state} but desired"

    # the state machine agrees with the clock
    if tick.state == "charging":
        assert active, f"{ctx} charging with no active session"
    if tick.state == "scheduled":
        assert not active and any(s >= now for s, _, _ in intervals), f"{ctx} scheduled but nothing upcoming"
    if tick.state in ("idle", "complete", "unschedulable"):
        assert not active, f"{ctx} {tick.state} but a session is in progress"

    # time left matches the sessions that are left
    if tick.state in ("scheduled", "charging", "boosting", "error"):
        expected = sum(max(0.0, (e - max(s, now)).total_seconds()) for s, e, _ in intervals) / 3600
        assert abs(tick.hours_remaining - expected) < 1e-6, (
            f"{ctx} hours_remaining {tick.hours_remaining} != {expected}"
        )

    # never commit to more than was asked for
    required_hours_scheduled = sum(
        max(0.0, (e - max(s, now)).total_seconds()) / 3600
        for s, e, sess in intervals
        if sess.get("tier", "required") == "required"
    )
    if tick.required_hours > 0 and tick.state not in ("boosting",):
        # (If the user lowered the target below what was already delivered, nothing more may be
        # scheduled: the budget is what is left, never negative.)
        budget = max(0.0, tick.required_hours - tick.delivered_hours)
        assert required_hours_scheduled <= budget + 0.5 + 1e-6, (
            f"{ctx} over-committed: {required_hours_scheduled}h scheduled + {tick.delivered_hours}h delivered"
            f" > {tick.required_hours}h required"
        )

    if tick.state == "idle":
        assert tick.required_hours <= 0 or c.required_hours <= 0, f"{ctx} idle with hours required"

    # future required sessions respect the deadline and the price cap -- only in states that
    # recompute the plan every cycle (boosting and error replay the stored plan unchanged)
    if not sim.setting_edits and tick.state in ("scheduled", "charging", "complete"):
        for start, end, sess in intervals:
            if sess.get("tier", "required") != "required" or start <= now:
                continue
            assert end <= tick.ready_by, f"{ctx} required session ends after ready_by: {sess} ready_by={tick.ready_by}"
            assert sess["avg_price"] <= c.max_price + 1e-6, f"{ctx} session over max_price {c.max_price}: {sess}"

    # what is reported as "next"/"active" is what the session list says
    data = sim.data
    upcoming = [iv for iv in intervals if iv[0] > now]
    if tick.state in ("scheduled", "charging", "boosting", "error"):
        if upcoming:
            assert data["next_slot"] is not None, f"{ctx} next_slot missing"
            assert parse_dt(data["next_slot"]["start"]) == min(s for s, _, _ in upcoming), f"{ctx} wrong next_slot"
        else:
            assert data["next_slot"] is None, f"{ctx} next_slot reported with nothing upcoming"
        if active:
            assert data["active_slot"] is not None and parse_dt(data["active_slot"]["start"]) == active[0][0], (
                f"{ctx} active_slot does not match the running session"
            )
        else:
            assert data["active_slot"] is None, f"{ctx} active_slot reported with nothing running"
    assert data["block_count"] == len(data["sessions"]), f"{ctx} block_count out of step with sessions"
    ordered = [parse_dt(s["start"]) for s in data["upcoming_slots"]]
    assert ordered == sorted(ordered), f"{ctx} upcoming_slots out of order"

    # A freshly computed plan keeps full blocks together: at most one stretch of consecutive
    # required sessions is shorter than the minimum block (capped at what was needed in total).
    if tick.state == "scheduled" and not sim.setting_edits and not active:
        groups: list[list] = []
        for start, end, _ in sorted(
            (iv for iv in intervals if iv[2].get("tier", "required") == "required"), key=lambda iv: iv[0]
        ):
            if groups and groups[-1][1] >= start:
                groups[-1][1] = max(groups[-1][1], end)
            else:
                groups.append([start, end])
        total_slots = sum((end - start).total_seconds() for start, end in groups) / 1800
        floor_slots = min(max(1, math.ceil(c.min_block_hours * 2)), total_slots)
        short = [g for g in groups if (g[1] - g[0]).total_seconds() / 1800 < floor_slots]
        assert len(short) <= 1, f"{ctx} several blocks shorter than the minimum: {groups}"

    # stored state must always be serialisable
    json.dumps(c._stored_sessions)

    # delivered hours only move forward within a ready_by cycle
    if len(sim.timeline) >= 1 and not sim.setting_edits:
        prev = sim.timeline[-1]
        if prev.ready_by == tick.ready_by and prev.required_hours == tick.required_hours:
            assert tick.delivered_hours >= prev.delivered_hours - 1e-9, (
                f"{ctx} delivered hours went backwards {prev.delivered_hours} -> {tick.delivered_hours}"
            )
