"""Brute-force reference for slot selection: enumerate every subset, keep the valid ones, take
the cheapest. Exponential, so only for small instances -- which is exactly where it can be
trusted as ground truth for the real algorithms.

A selection is valid when (the documented rules of find_optimal_slots):
- it has exactly `required` slots, all finishing by ready_by;
- every selected slot lies in a contiguous run of eligible slots at least min_block long (a run
  too short to hold a block is never used, even for the one allowed short top-up);
- at most one maximal contiguous group is shorter than min_block (the "last block may be short"
  relaxation);
- every maximal contiguous group's average RAW price is within max_price (a cap on the block's
  average, not on single slots);
- (only for the exact DP algorithms, when max_block_hours is given) no maximal group is longer
  than the block cap.
"""

from __future__ import annotations

import itertools
import math
import random
from datetime import UTC, datetime, timedelta

from custom_components.wholesale_ev_schedule.scheduler import (
    assign_credibilities,
    build_contiguous_runs,
    deduplicate_and_sort_prices,
    to_utc,
)

UTC = UTC
NOW = datetime(2026, 3, 2, 20, 0, tzinfo=UTC)


def min_slots(required: int, min_block_hours: float) -> int:
    return min(max(1, math.ceil(min_block_hours * 2)), required)


def max_slots(required: int, min_block_hours: float, max_block_hours: float | None) -> int | None:
    """The block-length cap in slots (floored up to the minimum block, as the scheduler does)."""
    if not max_block_hours or max_block_hours <= 0:
        return None
    return max(math.ceil(max_block_hours * 2), min_slots(required, min_block_hours))


def eligible(slots, ready_by):
    deadline = to_utc(ready_by)
    return sorted(
        (s for s in slots if to_utc(s["date_time"]) + timedelta(minutes=30) <= deadline), key=lambda s: s["date_time"]
    )


def usable(slots, ready_by, m):
    """Eligible slots that sit in a run long enough to hold a minimum block."""
    out = []
    for run in build_contiguous_runs(eligible(slots, ready_by)):
        if len(run) >= m:
            out.extend(run)
    return out


def is_valid(selection, usable_dts, required, m, cap, max_block=None, max_short=1):
    if len(selection) != required:
        return False
    if any(s["date_time"] not in usable_dts for s in selection):
        return False
    blocks = build_contiguous_runs(sorted(selection, key=lambda s: s["date_time"]))
    if sum(1 for b in blocks if len(b) < m) > max_short:
        return False
    if max_block is not None and any(len(b) > max_block for b in blocks):
        return False
    if cap is not None:
        return all(sum(s["raw_price"] for s in b) / len(b) <= cap + 1e-9 for b in blocks)
    return True


def cost(selection) -> float:
    return sum(s["effective_price"] for s in selection)


def cheapest_valid(slots, ready_by, required, min_block_hours, cap, max_block_hours=None, max_short=1):
    """(best_cost, best_selection) over all valid selections, or (None, None) if there is none."""
    m = min_slots(required, min_block_hours)
    max_block = max_slots(required, min_block_hours, max_block_hours)
    pool = usable(slots, ready_by, m)
    usable_dts = {s["date_time"] for s in pool}
    best, best_sel = None, None
    for combo in itertools.combinations(pool, required):
        if is_valid(combo, usable_dts, required, m, cap, max_block, max_short):
            c = cost(combo)
            if best is None or c < best - 1e-12:
                best, best_sel = c, combo
    return best, best_sel


def random_instance(rng: random.Random):
    """A small random market: a few runs separated by gaps, mixed sources, some negative prices."""
    n = rng.randint(5, 13)
    start = NOW + timedelta(hours=rng.randint(1, 6))
    raw, t = [], start
    for i in range(n):
        if i and rng.random() < 0.18:
            t += timedelta(minutes=30 * rng.randint(1, 3))  # a gap
        source = rng.choice(["current_actual", "current_actual", "predicted", "short_range", "short_range_final"])
        price = rng.choice([rng.randint(-8, 60), rng.randint(5, 30), rng.randint(5, 30), 20])  # ties on purpose
        slot = {"date_time": t, "raw_price": float(price), "source": source}
        if source == "short_range":
            slot["base_credibility"] = rng.choice([0.5, 0.8, 0.95])
        raw.append(slot)
        t += timedelta(minutes=30)
    gamble = rng.choice([0.0, 30.0, 70.0, 100.0])
    slots = assign_credibilities(deduplicate_and_sort_prices(raw, NOW), NOW, gamble)
    required = rng.randint(1, min(8, n))
    min_block_hours = rng.choice([0.0, 0.5, 1.0, 1.5, 2.0, 3.0])
    cap = rng.choice([None, None, 10.0, 20.0, 30.0, 45.0, 0.0])
    ready_by = slots[-1]["date_time"] + timedelta(minutes=30 * rng.randint(0, 2))
    return slots, ready_by, required, min_block_hours, cap
