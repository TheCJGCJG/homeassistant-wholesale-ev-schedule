"""Pure scheduling logic for Wholesale EV Schedule — no Home Assistant dependency.

Ported from https://github.com/TheCJGCJG/homeassistant-pyscripts
(src/ev_charging_state_machine.py), which carries a well-exercised test suite for
this algorithm. Keeping these functions free of HA imports keeps them unit-testable
in isolation, same as the source project.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import logging
import math

_LOGGER = logging.getLogger(__name__)

TIER_ACTUAL = "actual"
TIER_PREDICTED_0_24 = "predicted_0_24"
TIER_PREDICTED_24_48 = "predicted_24_48"
TIER_PREDICTED_48_72 = "predicted_48_72"
TIER_PREDICTED_72_PLUS = "predicted_72_plus"

BASE_CREDIBILITY = {
    TIER_ACTUAL: 1.0,
    TIER_PREDICTED_0_24: 0.90,
    TIER_PREDICTED_24_48: 0.75,
    TIER_PREDICTED_48_72: 0.60,
    TIER_PREDICTED_72_PLUS: 0.40,
}


def get_source_tier(source: str, slot_dt: datetime, now_dt: datetime) -> str:
    """Return credibility tier string for a price slot based on source and time horizon."""
    if source in ("current_actual", "next_actual"):
        return TIER_ACTUAL
    hours_ahead = (slot_dt - now_dt).total_seconds() / 3600
    if hours_ahead <= 24:
        return TIER_PREDICTED_0_24
    if hours_ahead <= 48:
        return TIER_PREDICTED_24_48
    if hours_ahead <= 72:
        return TIER_PREDICTED_48_72
    return TIER_PREDICTED_72_PLUS


def compute_effective_price(raw_price: float, tier: str, gamble_tolerance: float) -> float:
    """Risk-adjusted price used for slot ranking. Low gamble_tolerance inflates predicted
    prices, favouring known actual rates; at 100 all prices are taken at face value."""
    base_cred = BASE_CREDIBILITY[tier]
    eff_cred = base_cred + (1.0 - base_cred) * (gamble_tolerance / 100.0)
    return raw_price / eff_cred


def assign_credibilities(slots: list[dict], now_dt: datetime, gamble_tolerance: float) -> list[dict]:
    """Add 'tier' and 'effective_price' fields to each slot dict. Returns new list."""
    result = []
    for slot in slots:
        tier = get_source_tier(slot["source"], slot["date_time"], now_dt)
        eff_price = compute_effective_price(slot["raw_price"], tier, gamble_tolerance)
        entry = dict(slot)
        entry["tier"] = tier
        entry["effective_price"] = eff_price
        result.append(entry)
    return result


def build_contiguous_runs(slots: list[dict]) -> list[list[dict]]:
    """Group a sorted list of 30-min price slots into runs of consecutive slots."""
    if not slots:
        return []
    runs = []
    current_run = [slots[0]]
    for slot in slots[1:]:
        expected = current_run[-1]["date_time"] + timedelta(minutes=30)
        if slot["date_time"] == expected:
            current_run.append(slot)
        else:
            runs.append(current_run)
            current_run = [slot]
    runs.append(current_run)
    return runs


def find_optimal_slots(
    candidate_slots: list[dict],
    required_slots: int,
    ready_by_dt: datetime,
    min_block_hours: float,
    max_price: float | None = None,
) -> list[dict]:
    """Select the cheapest combination of slots totalling required_slots, where every
    contiguous block is >= min_block_hours and (if max_price given) each block's average
    raw price <= max_price. Returns a flat list of selected slot dicts, or [] if none.
    """
    if not candidate_slots or required_slots <= 0:
        return []

    # A minimum block longer than the whole requirement would make every request for
    # less than that long unschedulable, so relax it to a single contiguous block.
    min_slots_per_block = min(max(1, math.ceil(min_block_hours * 2)), required_slots)

    eligible = [s for s in candidate_slots if s["date_time"] + timedelta(minutes=30) <= ready_by_dt]
    eligible.sort(key=lambda s: s["date_time"])

    all_runs = build_contiguous_runs(eligible)
    valid_runs = [r for r in all_runs if len(r) >= min_slots_per_block]
    eligible_from_valid = [s for run in valid_runs for s in run]

    if len(eligible_from_valid) < required_slots:
        return []

    windows = []
    for run in valid_runs:
        max_window_size = min(required_slots, len(run))
        for size in range(min_slots_per_block, max_window_size + 1):
            for start_idx in range(len(run) - size + 1):
                w_slots = run[start_idx:start_idx + size]
                avg_eff = sum(s["effective_price"] for s in w_slots) / size
                windows.append({
                    "slots": w_slots,
                    "dts": {s["date_time"] for s in w_slots},
                    "size": size,
                    "avg_eff": avg_eff,
                })

    if max_price is not None:
        windows = [
            w for w in windows
            if sum(s["raw_price"] for s in w["slots"]) / w["size"] <= max_price
        ]
        if not windows:
            return []

    windows.sort(key=lambda w: w["avg_eff"])

    # Greedy selection: cheapest non-overlapping windows summing to required_slots.
    # After each pick the remainder must be 0 or >= min_slots_per_block so it can
    # always be filled with another full-sized block.
    selected_dts: set = set()
    remaining = required_slots
    for w in windows:
        if w["size"] > remaining:
            continue
        after = remaining - w["size"]
        if after > 0 and after < min_slots_per_block:
            continue
        if w["dts"] & selected_dts:
            continue
        selected_dts |= w["dts"]
        remaining -= w["size"]
        if remaining == 0:
            break

    # Relaxed fallback: top up any small remainder with the cheapest leftover slots
    # from valid runs (the last block may end up shorter than min_block_hours).
    if 0 < remaining < required_slots:
        leftover = sorted(
            (s for s in eligible_from_valid if s["date_time"] not in selected_dts),
            key=lambda s: s["effective_price"],
        )
        for s in leftover:
            selected_dts.add(s["date_time"])
            remaining -= 1
            if remaining == 0:
                break

    if remaining > 0:
        return []

    result = [s for s in eligible if s["date_time"] in selected_dts]
    result.sort(key=lambda s: s["date_time"])
    return result


def slots_to_sessions(selected_slots: list[dict]) -> list[dict]:
    """Convert a flat list of slot dicts into session dicts grouped by contiguous run.

    confidence (0-100) is the average base credibility of the slots in the session:
    100 = all actual rates, down to 40 for predictions more than 72h out.
    """
    if not selected_slots:
        return []
    sorted_slots = sorted(selected_slots, key=lambda s: s["date_time"])
    runs = build_contiguous_runs(sorted_slots)
    sessions = []
    for run in runs:
        start = run[0]["date_time"]
        end = run[-1]["date_time"] + timedelta(minutes=30)
        avg_price = sum(s["raw_price"] for s in run) / len(run)
        avg_cred = sum(BASE_CREDIBILITY.get(s.get("tier", TIER_ACTUAL), 1.0) for s in run) / len(run)
        sessions.append({
            "start": start.isoformat(),
            "end": end.isoformat(),
            "duration_hours": len(run) * 0.5,
            "avg_price": round(avg_price, 2),
            "confidence": round(avg_cred * 100, 1),
        })
    return sessions


def parse_iso_str(value: str) -> datetime:
    """Parse an ISO 8601 datetime string, normalising a 'Z' UTC suffix to '+00:00'."""
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def parse_dt(value) -> datetime:
    """Parse a datetime value from a session dict or stored attribute, which may
    already be a datetime object when read back from HA storage."""
    if isinstance(value, datetime):
        return value
    return parse_iso_str(str(value))


def prune_and_classify(sessions: list[dict], now_dt: datetime) -> tuple[dict | None, list[dict]]:
    """Split sessions into (active_session | None, [future_sessions]). Expired
    sessions (end <= now_dt) are silently dropped."""
    active = None
    future = []
    for s in sessions:
        start = parse_dt(s["start"])
        end = parse_dt(s["end"])
        if start <= now_dt < end:
            active = s
        elif end > now_dt:
            future.append(s)
    return active, future


def compute_hours_remaining(future_sessions: list[dict], active_session: dict | None, now_dt: datetime) -> float:
    """Total uncommenced committed charging time: remaining portion of the active
    session plus all future sessions in full."""
    total = 0.0
    if active_session:
        end = parse_dt(active_session["end"])
        total += max(0.0, (end - now_dt).total_seconds() / 3600)
    for s in future_sessions:
        total += s.get("duration_hours", 0.0)
    return total


def deduplicate_and_sort_prices(all_prices: list[dict], now_dt: datetime) -> list[dict]:
    """Merge price data from all sources: actual rates win over predicted for the
    same slot. Discards slots that have already ended, sorts the rest chronologically."""
    prices_by_dt: dict = {}
    for p in all_prices:
        dt = p["date_time"]
        is_actual = p["source"] in ("current_actual", "next_actual")
        existing = prices_by_dt.get(dt)
        if existing is None:
            prices_by_dt[dt] = p
        elif is_actual and existing["source"] not in ("current_actual", "next_actual"):
            prices_by_dt[dt] = p

    result = []
    for dt in sorted(prices_by_dt.keys()):
        p = prices_by_dt[dt]
        if dt + timedelta(minutes=30) > now_dt:
            result.append(p)
    return result


def determine_state(
    required_hours: float,
    boost_active: bool,
    active_session: dict | None,
    future_sessions: list[dict],
    hours_remaining: float,
    data_ok: bool,
) -> str:
    """Return the state name for the current conditions. charger_connected is
    intentionally not a parameter — the state reflects what is scheduled regardless
    of connection; `desired` is gated on connection separately by the caller so the
    schedule stays visible on the dashboard even when the car is unplugged."""
    if not data_ok:
        return "error"
    if required_hours is None or required_hours <= 0:
        return "idle"
    if boost_active:
        return "boosting"
    if active_session:
        return "charging"
    if future_sessions:
        return "scheduled"
    if hours_remaining <= 0:
        return "complete"
    return "error"
