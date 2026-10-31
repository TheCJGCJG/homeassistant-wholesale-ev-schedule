"""Pure scheduling logic for Wholesale EV Schedule — no Home Assistant dependency.

Ported from https://github.com/TheCJGCJG/homeassistant-pyscripts
(src/ev_charging_state_machine.py), which carries a well-exercised test suite for
this algorithm. Keeping these functions free of HA imports keeps them unit-testable
in isolation, same as the source project.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta

_LOGGER = logging.getLogger(__name__)


def to_utc(dt: datetime) -> datetime:
    """`dt` as an absolute UTC instant (naive datetimes are returned unchanged).

    Every comparison, dictionary key and piece of arithmetic on a slot's time goes through
    this. Python compares and subtracts two aware datetimes that share a tzinfo by their
    *wall clock*, ignoring the UTC offset, and treats the repeated autumn hour as one key --
    so local-time slots silently lose or mis-measure time across a DST change. UTC has no
    such thing. Returning the very same object when it is already UTC keeps the common case
    free.
    """
    tzinfo = dt.tzinfo
    # Hot path: the coordinator already hands over UTC, and this runs thousands of times a refresh.
    if tzinfo is UTC or tzinfo is None:
        return dt
    return dt.astimezone(UTC)


# Mirrors const.py's OPTIMIZATION_ALGORITHM_* values, duplicated as plain string
# literals rather than imported -- const.py pulls in homeassistant.const, which would
# break this module's deliberate no-HA-dependency contract (see module docstring).
OPTIMIZATION_ALGORITHM_GREEDY = "greedy"
OPTIMIZATION_ALGORITHM_OPTIMAL = "optimal"
OPTIMIZATION_ALGORITHM_HYBRID = "hybrid"

# Where a price slot came from. coordinator.py tags every slot with one of these;
# nothing here knows or cares which entity or provider produced it.
SOURCE_CURRENT_ACTUAL = "current_actual"
SOURCE_NEXT_ACTUAL = "next_actual"
# A short-range forecast's prices, split by the source's own per-point flag: an
# estimate (still moving, e.g. a day-ahead auction not yet final) or final (the
# source says it will not change -- treated as actual-grade).
SOURCE_SHORT_RANGE = "short_range"
SOURCE_SHORT_RANGE_FINAL = "short_range_final"
SOURCE_PREDICTED = "predicted"

# Precedence when several sources price the same slot: lower rank wins, equal ranks
# keep whichever came first. Real rates beat everything; a source's own final
# prices beat its estimates, which beat the long-range forecast. A source this
# table doesn't know ranks as the forecast, i.e. exactly as every non-actual
# source did before short-range forecasts existed.
_SOURCE_RANK = {
    SOURCE_CURRENT_ACTUAL: 0,
    SOURCE_NEXT_ACTUAL: 0,
    SOURCE_SHORT_RANGE_FINAL: 1,
    SOURCE_SHORT_RANGE: 2,
    SOURCE_PREDICTED: 3,
}
_UNKNOWN_SOURCE_RANK = _SOURCE_RANK[SOURCE_PREDICTED]
_ACTUAL_GRADE_SOURCES = frozenset({SOURCE_CURRENT_ACTUAL, SOURCE_NEXT_ACTUAL, SOURCE_SHORT_RANGE_FINAL})

TIER_ACTUAL = "actual"
TIER_SHORT_RANGE = "short_range"
TIER_PREDICTED_0_24 = "predicted_0_24"
TIER_PREDICTED_24_48 = "predicted_24_48"
TIER_PREDICTED_48_72 = "predicted_48_72"
TIER_PREDICTED_72_PLUS = "predicted_72_plus"

BASE_CREDIBILITY = {
    TIER_ACTUAL: 1.0,
    # Fallback only: coordinator.py always stamps short-range slots with the
    # user-configured confidence (a "base_credibility" field, see
    # slot_base_credibility). Mirrors const.DEFAULT_SHORT_RANGE_CONFIDENCE, which
    # this module can't import (see the module note on OPTIMIZATION_ALGORITHM_*).
    TIER_SHORT_RANGE: 0.95,
    TIER_PREDICTED_0_24: 0.90,
    TIER_PREDICTED_24_48: 0.75,
    TIER_PREDICTED_48_72: 0.60,
    TIER_PREDICTED_72_PLUS: 0.40,
}


def get_source_tier(source: str, slot_dt: datetime, now_dt: datetime) -> str:
    """Return credibility tier string for a price slot based on source and time horizon."""
    if source in _ACTUAL_GRADE_SOURCES:
        return TIER_ACTUAL
    if source == SOURCE_SHORT_RANGE:
        # Deliberately horizon-independent: a short-range source exists because it is
        # trusted over the long-range forecast at the distances it covers.
        return TIER_SHORT_RANGE
    hours_ahead = (to_utc(slot_dt) - to_utc(now_dt)).total_seconds() / 3600
    if hours_ahead <= 24:
        return TIER_PREDICTED_0_24
    if hours_ahead <= 48:
        return TIER_PREDICTED_24_48
    if hours_ahead <= 72:
        return TIER_PREDICTED_48_72
    return TIER_PREDICTED_72_PLUS


# A credibility of 0 would make compute_effective_price divide by zero at a gamble
# tolerance of 0; this floor keeps the ranking finite (just extremely unattractive).
_MIN_CREDIBILITY = 0.01


def _resolve_base_credibility(tier: str, override: float | None) -> float:
    """The tier's base credibility, or a per-slot `override` clamped to a usable
    range. A missing or non-finite override falls back to the tier's own value
    rather than propagating NaN into every price comparison."""
    if override is None or not math.isfinite(override):
        return BASE_CREDIBILITY[tier]
    return max(_MIN_CREDIBILITY, min(1.0, override))


def slot_base_credibility(slot: dict) -> float:
    """A slot's base credibility: its own "base_credibility" override if it has one
    (short-range slots do), otherwise its tier's. Single definition shared by
    price ranking and the session `confidence` figure so the two can't drift."""
    return _resolve_base_credibility(slot.get("tier", TIER_ACTUAL), slot.get("base_credibility"))


def compute_effective_price(
    raw_price: float, tier: str, gamble_tolerance: float, base_credibility: float | None = None
) -> float:
    """Risk-adjusted price used for slot ranking. Low gamble_tolerance makes predicted
    prices less attractive (inflating positive ones, shrinking negative ones toward zero),
    favouring known actual rates; at 100 all prices are taken at face value.

    gamble_tolerance is clamped to [0, 100] -- its only intended domain. Above
    100, eff_cred exceeds 1.0 for predicted tiers, making a predicted price
    *cheaper* than an equally-priced actual one and inverting the documented
    ranking (issue #41). Sufficiently below 0, eff_cred crosses zero and this
    function divides by it (issue #42). The live number entity already
    clamps to [0, 100], but a stored value can bypass that (see coordinator.py).

    base_credibility, if given, replaces the tier's own base value (clamped to a
    usable range) -- how a short-range source's configured confidence is applied.
    """
    gamble_tolerance = max(0.0, min(100.0, gamble_tolerance))
    base_cred = _resolve_base_credibility(tier, base_credibility)
    eff_cred = base_cred + (1.0 - base_cred) * (gamble_tolerance / 100.0)
    if raw_price < 0:
        # Negative prices ("plunge pricing") flip the direction of the penalty: dividing -5p by
        # 0.9 gives -5.6p, making a doubtful price look MORE attractive than a real -5p one.
        # Shrinking it toward zero instead keeps "doubtful is never better than real" true for
        # every sign, and meets the positive branch continuously at zero.
        return raw_price * eff_cred
    return raw_price / eff_cred


def assign_credibilities(slots: list[dict], now_dt: datetime, gamble_tolerance: float) -> list[dict]:
    """Add 'tier' and 'effective_price' fields to each slot dict. Returns new list."""
    result = []
    for slot in slots:
        tier = get_source_tier(slot["source"], slot["date_time"], now_dt)
        eff_price = compute_effective_price(slot["raw_price"], tier, gamble_tolerance, slot.get("base_credibility"))
        entry = dict(slot)
        entry["date_time"] = to_utc(slot["date_time"])
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
        expected = to_utc(current_run[-1]["date_time"]) + timedelta(minutes=30)
        if to_utc(slot["date_time"]) == expected:
            current_run.append(slot)
        else:
            runs.append(current_run)
            current_run = [slot]
    runs.append(current_run)
    return runs


def _eligible_slots(candidate_slots: list[dict], ready_by_dt: datetime) -> list[dict]:
    """The candidates that finish by ready_by, sorted by time, with every `date_time` as a UTC
    instant (see to_utc). Shared by all three algorithms so the deadline rule and the
    DST-safe normalisation live in exactly one place. A slot that is already UTC is passed
    through as the same object rather than copied."""
    deadline = to_utc(ready_by_dt)
    eligible = []
    for slot in candidate_slots:
        start = to_utc(slot["date_time"])
        if start + timedelta(minutes=30) <= deadline:
            eligible.append(slot if start is slot["date_time"] else {**slot, "date_time": start})
    eligible.sort(key=lambda s: s["date_time"])
    return eligible


def _best_topup(
    pool: list[dict],
    taken_dts: set,
    size: int,
    min_slots_per_block: int,
    max_price: float | None,
    max_slots_per_block: int | None,
) -> list[dict] | None:
    """Cheapest way to add exactly `size` more slots from `pool` (not already in
    `taken_dts`) without breaking the block rules, or None if no way exists.

    Fills whatever a block-based selection couldn't (the "last block may end up shorter
    than min_block_hours" relaxation). It must NOT pick the cheapest individual slots
    independently: that scattered them into several 1-slot blocks whenever it was
    cheaper than any valid block, silently defeating min_block_hours (a live install
    with min_block_hours=2 and 1.5h required got three isolated 30-minute slots).

    Candidates grow the existing selection outward by e slots (cheapest adjacent first --
    how a pricey slot gets absorbed into a block whose merged average is still fine, e.g.
    [5, 5, 20, 5] under a cap of 10) and put the other size-e slots in one contiguous
    window elsewhere, for every split e. Each candidate is then validated on the blocks it
    actually produces: at most one block may be shorter than min_slots_per_block, and
    every block's raw average must be <= max_price. max_price is a block average, not a
    per-slot limit, so it can't be checked slot by slot here (the same reason
    _min_cost_for_run only checks it where a block's size is decided).
    """
    free = [s for s in pool if s["date_time"] not in taken_dts]
    if size <= 0:
        return []

    def _valid(extra: list[dict]) -> bool:
        by_dt = {s["date_time"]: s for s in extra}
        by_dt.update({s["date_time"]: s for s in pool if s["date_time"] in taken_dts})
        blocks = build_contiguous_runs([by_dt[dt] for dt in sorted(by_dt)])
        if sum(1 for blk in blocks if len(blk) < min_slots_per_block) > 1:
            return False
        if max_price is None:
            return True
        return all(sum(x["raw_price"] for x in blk) / len(blk) <= max_price for blk in blocks)

    step = timedelta(minutes=30)
    free_by_dt = {s["date_time"]: s for s in free}

    # Slots grown outward from the existing selection, cheapest adjacent slot first;
    # grown[:e] is the cheapest way to absorb e slots into already-selected blocks.
    grown: list[dict] = []
    covered = set(taken_dts)
    while len(grown) < size:
        frontier = [
            free_by_dt[dt] for c in covered for dt in (c - step, c + step) if dt in free_by_dt and dt not in covered
        ]
        if not frontier:
            break
        pick = min(frontier, key=lambda s: s["effective_price"])
        grown.append(pick)
        covered.add(pick["date_time"])

    candidates: list[list[dict]] = []
    free_runs = build_contiguous_runs(sorted(free, key=lambda s: s["date_time"]))
    for e in range(min(size, len(grown)) + 1):
        absorbed = grown[:e]
        absorbed_dts = {s["date_time"] for s in absorbed}
        w = size - e
        if w == 0:
            candidates.append(absorbed)
            continue
        if max_slots_per_block is not None and w > max_slots_per_block:
            continue
        # The rest is one contiguous window elsewhere (a single new block, which
        # _valid then requires to be the only undersized one). Only the cheapest few
        # per split are kept -- they're tried cheapest first below.
        windows = []
        for run in free_runs:
            for i in range(len(run) - w + 1):
                window = run[i : i + w]
                if absorbed_dts.isdisjoint(x["date_time"] for x in window):
                    windows.append((sum(x["effective_price"] for x in window), window))
        windows.sort(key=lambda t: t[0])
        candidates.extend(absorbed + window for _, window in windows[:5])

    # Cheapest first, validating lazily: validation rebuilds every block, so only pay
    # for it until the first candidate that passes.
    for cand in sorted(candidates, key=lambda c: sum(s["effective_price"] for s in c)):
        if _valid(cand):
            return cand
    return None


def _find_optimal_slots_greedy(
    candidate_slots: list[dict],
    required_slots: int,
    ready_by_dt: datetime,
    min_block_hours: float,
    max_price: float | None = None,
    max_block_hours: float | None = None,
) -> list[dict]:
    """Original find_optimal_slots implementation (the "greedy" algorithm, see
    OPTIMIZATION_ALGORITHM_GREEDY in const.py): picks the single cheapest window first
    and fills any remainder from what's left. Fast, but can lose to a different
    combination -- often one larger window -- whose combined total is cheaper, since it
    only ever compares individual window averages against each other, never total cost
    of one combination against another (issue #55). Kept as the default, unchanged,
    alongside the exact `_find_optimal_slots_optimal` and narrowed-search
    `_find_optimal_slots_hybrid` alternatives -- see `find_optimal_slots`.

    max_block_hours (if given and > 0) caps how long any single contiguous block may
    be — a cheap run longer than this is never offered as one big window, so when the
    cheapest slots are spread across separate price dips they get combined into
    several smaller blocks capped at this length rather than one long one. Note this
    is a cap on window size during selection, not an enforced rest period: if the
    single cheapest option in the market genuinely is one long uninterrupted dip,
    capping the window size still results in back-to-back blocks with no gap between
    them, since there's no cost-based reason to leave a cheaper slot unpicked.
    """
    if not candidate_slots or required_slots <= 0:
        return []

    # A minimum block longer than the whole requirement would make every request for
    # less than that long unschedulable, so relax it to a single contiguous block.
    min_slots_per_block = min(max(1, math.ceil(min_block_hours * 2)), required_slots)
    # Per the docstring, max_block_hours only applies "if given and > 0" -- a
    # zero or negative value means unlimited, same as None (issue #40 found a
    # negative value was instead treated as truthy, producing a negative cap).
    max_slots_per_block = math.ceil(max_block_hours * 2) if max_block_hours and max_block_hours > 0 else None
    if max_slots_per_block is not None:
        # A cap shorter than the minimum block would make every window's size
        # range empty (`range(min_slots_per_block, max_window_size + 1)` below
        # with max_window_size < min_slots_per_block), so no window is ever
        # generated at all -- a silent false unschedulable regardless of the
        # actual price data. Floor it to min_slots_per_block instead, the same
        # way min_slots_per_block itself relaxes when it exceeds the
        # requirement above.
        max_slots_per_block = max(max_slots_per_block, min_slots_per_block)

    eligible = _eligible_slots(candidate_slots, ready_by_dt)

    all_runs = build_contiguous_runs(eligible)
    valid_runs = [r for r in all_runs if len(r) >= min_slots_per_block]
    eligible_from_valid = [s for run in valid_runs for s in run]

    if len(eligible_from_valid) < required_slots:
        return []

    # Prefix sums so any window's effective-price and raw-price sums are O(1)
    # subtractions instead of re-summing the slice from scratch for every
    # (start, size) pair, and windows are identified by (run_id, start_idx)
    # rather than a materialized slots list/dts set -- both were also O(size)
    # to build, same as the sums, and just as dominant a cost. This loop was
    # previously effectively O(len(run) * window_size^2) per run (issue #45).
    # max_price filtering is folded into the same pass rather than a second
    # full pass over every window afterward.
    windows = []
    for run_id, run in enumerate(valid_runs):
        max_window_size = min(required_slots, len(run))
        if max_slots_per_block is not None:
            max_window_size = min(max_window_size, max_slots_per_block)

        eff_prefix = [0.0]
        raw_prefix = [0.0]
        for s in run:
            eff_prefix.append(eff_prefix[-1] + s["effective_price"])
            raw_prefix.append(raw_prefix[-1] + s["raw_price"])

        for size in range(min_slots_per_block, max_window_size + 1):
            for start_idx in range(len(run) - size + 1):
                if max_price is not None:
                    raw_avg = (raw_prefix[start_idx + size] - raw_prefix[start_idx]) / size
                    if raw_avg > max_price:
                        continue
                eff_sum = eff_prefix[start_idx + size] - eff_prefix[start_idx]
                windows.append(
                    {
                        "run_id": run_id,
                        "start_idx": start_idx,
                        "size": size,
                        "avg_eff": eff_sum / size,
                    }
                )

    if max_price is not None and not windows:
        return []

    windows.sort(key=lambda w: w["avg_eff"])

    def _overlaps(a: dict, b: dict) -> bool:
        # Windows only ever come from the same contiguous run or different,
        # disjoint runs (build_contiguous_runs) -- cross-run windows can never
        # share a slot, and same-run windows share one iff their [start_idx,
        # start_idx+size) index ranges intersect, exactly like their
        # underlying date_time ranges would.
        return a["run_id"] == b["run_id"] and not (
            a["start_idx"] + a["size"] <= b["start_idx"] or b["start_idx"] + b["size"] <= a["start_idx"]
        )

    def _greedy_select(strict: bool) -> tuple[list[dict], int]:
        # Cheapest non-overlapping windows summing to required_slots. In strict mode,
        # after each pick the remainder must be 0 or >= min_slots_per_block so it can
        # always be filled with another full-sized block.
        selected: list[dict] = []
        left = required_slots
        for w in windows:
            if w["size"] > left:
                continue
            after = left - w["size"]
            if strict and after > 0 and after < min_slots_per_block:
                continue
            if any(_overlaps(w, sel) for sel in selected):
                continue
            selected.append(w)
            left -= w["size"]
            if left == 0:
                break
        return selected, left

    def _complete(selected_windows: list[dict], remaining: int) -> set | None:
        # Only the (few) selected windows' actual date_times are resolved here --
        # deferring this from window-generation time is exactly what keeps that
        # loop O(1) per window instead of O(size).
        selected_dts: set = set()
        for w in selected_windows:
            run = valid_runs[w["run_id"]]
            for s in run[w["start_idx"] : w["start_idx"] + w["size"]]:
                selected_dts.add(s["date_time"])
        if remaining == 0:
            return selected_dts
        # Relaxed fallback: fill the remainder with the cheapest valid top-up (the last
        # block may end up shorter than min_block_hours) -- see _best_topup.
        if remaining < required_slots:
            window = _best_topup(
                eligible_from_valid, selected_dts, remaining, min_slots_per_block, max_price, max_slots_per_block
            )
            if window is not None:
                selected_dts.update(s["date_time"] for s in window)
                return selected_dts
        return None

    selected_windows, remaining = _greedy_select(strict=True)
    selected_dts = None
    if remaining < required_slots:
        selected_dts = _complete(selected_windows, remaining)
    if selected_dts is None and windows:
        # Strict selection made zero progress, or left a remainder no valid top-up can
        # fill -- e.g. required_slots is split across separate runs that are each too
        # short to leave a full-size remainder on their own (see issue #23), or the
        # neat-remainder rule stopped at one window and the leftover would itself need a
        # block that busts max_price (18 slots at min 8: strict takes 8, leaving 10, but
        # nothing valid is 9-10 long). Rather than report unschedulable outright, relax
        # the "leave a neat remainder" preference so more windows get picked, and let the
        # top-up cover what's left (at most one undersized block).
        selected_windows, remaining = _greedy_select(strict=False)
        selected_dts = _complete(selected_windows, remaining)

    if selected_dts is None:
        return []

    result = [s for s in eligible if s["date_time"] in selected_dts]
    result.sort(key=lambda s: s["date_time"])
    return result


def _min_cost_for_run(
    run: list[dict],
    k_cap: int,
    min_slots_per_block: int,
    max_slots_per_block: int | None,
    max_price: float | None,
) -> tuple[list[float], list, list[int]]:
    """DP over a single contiguous run: for every k in 0..k_cap, the minimum total
    effective-price cost of selecting exactly k of the run's slots as a union of
    disjoint contiguous blocks, each sized min_slots_per_block..max_slots_per_block
    (unbounded above if max_slots_per_block is None) and (if max_price given) with raw
    average <= max_price.

    Returns (cost_by_k, history, end_state_by_k): cost_by_k[k] is math.inf if k is
    unreachable; history[t] holds the backpointer choice made at run-index t for every
    (k, state) cell reached there; end_state_by_k[k] is the state cost_by_k[k] was read
    from. _reconstruct_run_selection replays these to recover which slots were picked.

    The state machine tracks, per position, how many slots have been consumed in the
    currently-open block (0 = not in a block). Once a block has consumed
    min_slots_per_block slots it becomes "free" and may end at any point or keep
    extending; states 1..min_slots_per_block-1 are "still mandatory" and must extend.
    This keeps the per-position state count at max_slots_per_block+1 (or
    min_slots_per_block+1 when unbounded, since every "free" length collapses to one
    absorbing state) instead of re-deriving eligible block sizes from scratch at every
    (position, k) cell, which would cost an extra O(block-size-range) factor -- this is
    what makes an exact search over ALL combinations tractable (O(run_length * k_cap)
    per run), unlike a naive DP that scans every block size at every cell.

    max_price is checked only where a block's size is actually decided -- the "end"
    transition (it stops growing) and the final cost_by_k scan (it's still open when the
    run runs out) -- never while a block is still growing. An earlier version rejected
    growth as soon as any intermediate prefix's average exceeded max_price, which
    permanently closed off longer versions of the same block whose eventual average
    would have been fine once diluted by more/cheaper slots later on -- e.g. a block
    starting [30, 1] (avg 15.5, over a cap of 12) was rejected outright, even though
    growing it to [30, 1, 1, 1, 1] (avg 6.8) is a valid, cheap block under the same cap.
    """
    n = len(run)
    k_cap = min(n, k_cap)
    lo = min_slots_per_block
    # With no length cap, every "free" length >= lo can collapse into one absorbing state
    # -- but only if nothing needs the block's real length. max_price does: a block's
    # average is over ALL its slots, and the collapsed state only knows the last `lo`
    # (so [40, 1, 1] passed a cap of 10 by checking just its tail). So with a cap, track
    # real lengths, bounded by k_cap since no block can exceed the slots being asked for.
    unbounded = max_slots_per_block is None and max_price is None
    if unbounded:
        hi = lo
    elif max_slots_per_block is None:
        hi = max(lo, k_cap)
    else:
        hi = max_slots_per_block

    raw_prefix = [0.0]
    for s in run:
        raw_prefix.append(raw_prefix[-1] + s["raw_price"])

    OUT = 0
    num_states = hi + 1  # OUT, plus "consumed c slots" for c in 1..hi

    NEG = math.inf
    dp = [[NEG] * num_states for _ in range(k_cap + 1)]
    dp[0][OUT] = 0.0
    history: list[list[list[tuple | None]]] = []

    for t in range(n):
        ndp = [[NEG] * num_states for _ in range(k_cap + 1)]
        nchoice: list[list[tuple | None]] = [[None] * num_states for _ in range(k_cap + 1)]
        price = run[t]["effective_price"]
        for k in range(k_cap + 1):
            row = dp[k]
            for s in range(num_states):
                val = row[s]
                if val == NEG:
                    continue
                if s == OUT:
                    if val < ndp[k][OUT]:
                        ndp[k][OUT] = val
                        nchoice[k][OUT] = ("skip", k, s)
                    if k + 1 <= k_cap:
                        # Growing a block is never rejected mid-flight -- max_price
                        # constrains a block's FINAL average once it stops growing, not
                        # every intermediate prefix along the way (a block that looks
                        # expensive at its minimum size can still average under the cap
                        # once diluted by more, cheaper slots later in the same block).
                        # The check happens once, at the "end" transition below and in
                        # the final cost_by_k scan, for whatever size the block actually
                        # settles at -- not here.
                        nxt = 1 if hi >= 1 else OUT
                        cand = val + price
                        if cand < ndp[k + 1][nxt]:
                            ndp[k + 1][nxt] = cand
                            nchoice[k + 1][nxt] = ("start", k, s)
                    continue
                c = s
                is_free = c == lo if unbounded else c >= lo
                if not is_free:
                    # Still short of the minimum block size -- must extend, no choice to
                    # end yet (see the OUT branch above for why max_price isn't checked
                    # here either).
                    if k + 1 <= k_cap:
                        cand = val + price
                        if cand < ndp[k + 1][c + 1]:
                            ndp[k + 1][c + 1] = cand
                            nchoice[k + 1][c + 1] = ("mand", k, s)
                    continue
                # Free: may end the block here (this position starts fresh as OUT) --
                # this is the one point a block's size is actually decided, so it's the
                # one point its average is checked against max_price.
                block_ok = True
                if max_price is not None:
                    block_start = t - c
                    raw_avg = (raw_prefix[t] - raw_prefix[block_start]) / c
                    block_ok = raw_avg <= max_price
                if block_ok and val < ndp[k][OUT]:
                    ndp[k][OUT] = val
                    nchoice[k][OUT] = ("end", k, s)
                # ...or extend it, if a longer block is still allowed (again, not
                # rejected here even if the average-so-far is over max_price -- only
                # ending the block validates it).
                can_extend = unbounded or c < hi
                if can_extend and k + 1 <= k_cap:
                    nxt_c = c if unbounded else c + 1
                    cand = val + price
                    if cand < ndp[k + 1][nxt_c]:
                        ndp[k + 1][nxt_c] = cand
                        nchoice[k + 1][nxt_c] = ("extend", k, s)
        dp = ndp
        history.append(nchoice)

    cost_by_k = [NEG] * (k_cap + 1)
    end_state_by_k: list[int] = [OUT] * (k_cap + 1)
    for k in range(k_cap + 1):
        best = dp[k][OUT]
        best_state = OUT
        for c in range(1, num_states):
            is_free = c == lo if unbounded else c >= lo
            if not is_free or dp[k][c] >= best:
                continue
            # A block still open (free, never explicitly ended) when the run runs out
            # of slots is implicitly "ended" here, at the run boundary -- the same
            # max_price check the "end" transition applies during the main loop, since
            # this is the other (only) place a block's size is finally decided.
            if max_price is not None:
                block_start = n - c
                raw_avg = (raw_prefix[n] - raw_prefix[block_start]) / c
                if raw_avg > max_price:
                    continue
            best = dp[k][c]
            best_state = c
        cost_by_k[k] = best
        end_state_by_k[k] = best_state

    return cost_by_k, history, end_state_by_k


def _reconstruct_run_selection(run: list[dict], history: list, k: int, end_state_by_k: list[int]) -> set:
    """Recover which of a run's date_times were selected to achieve _min_cost_for_run's
    cost_by_k[k], by replaying the DP's backpointers from the best final state at k."""
    selected: set = set()
    cur_k, cur_s = k, end_state_by_k[k]
    for t in range(len(run) - 1, -1, -1):
        kind, prev_k, prev_s = history[t][cur_k][cur_s]
        if kind in ("start", "mand", "extend"):
            selected.add(run[t]["date_time"])
        cur_k, cur_s = prev_k, prev_s
    return selected


def _solve_with_per_run_costs(
    eligible: list[dict],
    valid_runs: list[list[dict]],
    eligible_from_valid: list[dict],
    per_run: list[tuple[list[float], list, list[int]]],
    required_slots: int,
    max_price: float | None,
    min_slots_per_block: int,
    max_slots_per_block: int | None = None,
) -> list[dict]:
    """Shared final stage for both DP-based algorithms (_find_optimal_slots_optimal,
    _find_optimal_slots_hybrid): given each valid run's _min_cost_for_run cost curve,
    find the overall cheapest required_slots-sized selection.

    If max_price is set and no run has a valid block of any size, there is nothing to
    build a schedule from at all -- same as OPTIMIZATION_ALGORITHM_GREEDY's own early
    "no priced windows survived the cap" guard, which this mirrors. Without it, the
    relaxed leftover top-up below (which -- also matching greedy -- doesn't itself
    filter candidate slots by max_price, since a single top-up slot isn't a priced
    block average) would otherwise be the only candidate and could return a schedule
    entirely made of slots over the cap.

    A small knapsack (combined[j] = min cost to select exactly j slots using whole
    valid blocks from the runs considered so far) finds, for every partial total j up
    to required_slots, the cheapest way to reach it using only fully min_block_hours-
    respecting blocks. j == required_slots itself is one candidate answer; every
    smaller j is also tried, topped up to required_slots with the cheapest leftover
    individual slots (the same "last block may end up shorter than min_block_hours"
    relaxation every algorithm falls back to) -- and the overall cheapest candidate
    wins, whether or not the strict j == required_slots combination succeeded.

    Trying every partial total (not just falling back to it when the strict one fails)
    matters because a combination that bends the block-size rule for one cheap leftover
    slot can beat a stricter one that doesn't -- matching OPTIMIZATION_ALGORITHM_GREEDY's
    own willingness to use that fallback opportunistically rather than only as an
    unschedulable-otherwise last resort (confirmed during issue #55 verification: an
    "optimal" that always preferred the strict combination could come out pricier than
    greedy on exactly this kind of case, which an "optimal" algorithm must never do).
    """
    if max_price is not None and all(all(c == math.inf for c in cost_by_k[1:]) for cost_by_k, _, _ in per_run):
        return []

    combined = [math.inf] * (required_slots + 1)
    combined[0] = 0.0
    choices: list[list[int | None]] = []
    for cost_by_k, _, _ in per_run:
        new_combined = list(combined)
        choice_row: list[int | None] = [None] * (required_slots + 1)
        max_k = len(cost_by_k) - 1
        for j in range(required_slots + 1):
            if combined[j] == math.inf:
                continue
            upper = min(max_k, required_slots - j)
            for k in range(1, upper + 1):
                cost_k = cost_by_k[k]
                if cost_k == math.inf:
                    continue
                cand = combined[j] + cost_k
                if cand < new_combined[j + k]:
                    new_combined[j + k] = cand
                    choice_row[j + k] = k
        combined = new_combined
        choices.append(choice_row)

    def _backtrack(target_j: int) -> set:
        selected_dts: set = set()
        j = target_j
        for run_idx in range(len(per_run) - 1, -1, -1):
            k = choices[run_idx][j]
            if not k:
                continue
            _, history, end_state_by_k = per_run[run_idx]
            selected_dts |= _reconstruct_run_selection(valid_runs[run_idx], history, k, end_state_by_k)
            j -= k
        return selected_dts

    # May be empty: a top-up must be one contiguous, under-cap window (see
    # _best_topup), which can legitimately not exist.
    # Branch and bound over the partial totals j: any top-up of `remaining` slots costs
    # at least the `remaining` cheapest free prices overall, so visit j in order of that
    # lower bound and stop once it can't beat the best complete answer found.
    cheapest_prefix = [0.0]
    for price in sorted(s["effective_price"] for s in eligible_from_valid):
        cheapest_prefix.append(cheapest_prefix[-1] + price)
    order = sorted(
        (j for j in range(required_slots + 1) if combined[j] != math.inf),
        key=lambda j: combined[j] + cheapest_prefix[required_slots - j],
    )
    candidates: list[tuple[set, float]] = []
    best_cost = math.inf
    for j in order:
        remaining = required_slots - j
        if combined[j] + cheapest_prefix[remaining] >= best_cost:
            break
        whole_block_dts = _backtrack(j) if j > 0 else set()
        if remaining == 0:
            candidates.append((whole_block_dts, combined[j]))
            best_cost = min(best_cost, combined[j])
            continue
        window = _best_topup(
            eligible_from_valid, whole_block_dts, remaining, min_slots_per_block, max_price, max_slots_per_block
        )
        if window is None:
            continue
        cost = combined[j] + sum(s["effective_price"] for s in window)
        candidates.append((whole_block_dts | {s["date_time"] for s in window}, cost))
        best_cost = min(best_cost, cost)

    if not candidates:
        return []

    selected_dts, _ = min(candidates, key=lambda c: c[1])
    result = [s for s in eligible if s["date_time"] in selected_dts]
    result.sort(key=lambda s: s["date_time"])
    return result


def _min_max_slots_per_block(
    required_slots: int, min_block_hours: float, max_block_hours: float | None
) -> tuple[int, int | None]:
    """Shared min/max-slots-per-block derivation for the DP-based algorithms (see
    _find_optimal_slots_greedy's matching logic, which this mirrors): a minimum
    block longer than the whole requirement relaxes to a single contiguous block,
    and a max_block_hours cap shorter than the minimum floors up to it instead of
    leaving the DP with no valid block size at all (issue #40)."""
    min_slots_per_block = min(max(1, math.ceil(min_block_hours * 2)), required_slots)
    max_slots_per_block = math.ceil(max_block_hours * 2) if max_block_hours and max_block_hours > 0 else None
    if max_slots_per_block is not None:
        max_slots_per_block = max(max_slots_per_block, min_slots_per_block)
    return min_slots_per_block, max_slots_per_block


def _find_optimal_slots_optimal(
    candidate_slots: list[dict],
    required_slots: int,
    ready_by_dt: datetime,
    min_block_hours: float,
    max_price: float | None = None,
    max_block_hours: float | None = None,
) -> list[dict]:
    """Exact version of find_optimal_slots (OPTIMIZATION_ALGORITHM_OPTIMAL): every
    contiguous run of eligible slots is solved exactly via _min_cost_for_run's DP
    (minimum cost to pick exactly k of that run's slots as valid blocks, for every
    reachable k), then _solve_with_per_run_costs combines the per-run cost curves
    across runs to hit required_slots at minimum total cost. This finds the true
    optimum instead of the greedy "pick the single cheapest window, then fill the
    remainder from whatever's left" approach, which can lose to a combination -- often
    one larger contiguous window -- with a cheaper combined total (issue #55).

    Costs O(run_length * required_slots) per run, which is fast at realistic scale
    (a week of 30-min slots resolves in well under 100ms) but can be slower than the
    other two algorithms on a very long price horizon combined with a large or
    unbounded max_block_hours -- see OPTIMIZATION_ALGORITHM_HYBRID for a faster,
    heuristic middle ground.
    """
    if not candidate_slots or required_slots <= 0:
        return []

    min_slots_per_block, max_slots_per_block = _min_max_slots_per_block(
        required_slots, min_block_hours, max_block_hours
    )

    eligible = _eligible_slots(candidate_slots, ready_by_dt)

    all_runs = build_contiguous_runs(eligible)
    valid_runs = [r for r in all_runs if len(r) >= min_slots_per_block]
    eligible_from_valid = [s for run in valid_runs for s in run]

    if len(eligible_from_valid) < required_slots:
        return []

    per_run = [
        _min_cost_for_run(run, required_slots, min_slots_per_block, max_slots_per_block, max_price)
        for run in valid_runs
    ]

    return _solve_with_per_run_costs(
        eligible,
        valid_runs,
        eligible_from_valid,
        per_run,
        required_slots,
        max_price,
        min_slots_per_block,
        max_slots_per_block,
    )


def _find_optimal_slots_hybrid(
    candidate_slots: list[dict],
    required_slots: int,
    ready_by_dt: datetime,
    min_block_hours: float,
    max_price: float | None = None,
    max_block_hours: float | None = None,
) -> list[dict]:
    """Narrowed-search version of find_optimal_slots (OPTIMIZATION_ALGORITHM_HYBRID):
    a fast pre-pass restricts each run's DP to a handful of representative block sizes
    -- min_slots_per_block, the run's max usable size, and a few sizes in between --
    always including whichever size exactly covers what's still needed (the "one
    window spans the whole requirement" case that fixes issue #55's reported example),
    then _solve_with_per_run_costs searches exactly over just those sizes.

    This is a heuristic, not an exact search like OPTIMIZATION_ALGORITHM_OPTIMAL: sizes
    other than the representative ones are never considered, so a combination that
    only works at an untried size could in principle be missed. In exchange it stays
    close to the greedy algorithm's speed (each representative size costs the same
    single DP pass regardless of how many other sizes exist), rather than scaling with
    every achievable k the way the fully exact algorithm does.
    """
    if not candidate_slots or required_slots <= 0:
        return []

    min_slots_per_block, max_slots_per_block = _min_max_slots_per_block(
        required_slots, min_block_hours, max_block_hours
    )

    eligible = _eligible_slots(candidate_slots, ready_by_dt)

    all_runs = build_contiguous_runs(eligible)
    valid_runs = [r for r in all_runs if len(r) >= min_slots_per_block]
    eligible_from_valid = [s for run in valid_runs for s in run]

    if len(eligible_from_valid) < required_slots:
        return []

    per_run = []
    for run in valid_runs:
        run_max = min(len(run), required_slots)
        if max_slots_per_block is not None:
            run_max = min(run_max, max_slots_per_block)
        # Representative sizes: the two ends of the run's usable range, the size that
        # would exactly cover the remaining requirement in one block, and a few
        # intermediate fractions of it -- narrowing the search from "every achievable
        # k" down to a small, fixed-size set of candidate block lengths per run.
        candidate_sizes = {min_slots_per_block, run_max}
        for divisor in (2, 3, 4):
            size = required_slots // divisor
            if min_slots_per_block <= size <= run_max:
                candidate_sizes.add(size)
        # _min_cost_for_run computes the whole 0..k_cap cost curve in one pass, so
        # rather than call it once per representative size (which would only look at
        # single-block windows), call it once with k_cap capped at the largest
        # representative size -- this still explores every k up to that cap using
        # whatever mix of blocks is cheapest, it just never explores k's above the
        # largest representative size, keeping the DP's cost bounded independent of
        # required_slots when the run is much longer than any representative size.
        k_cap = max(candidate_sizes) if candidate_sizes else min_slots_per_block
        per_run.append(_min_cost_for_run(run, k_cap, min_slots_per_block, max_slots_per_block, max_price))

    return _solve_with_per_run_costs(
        eligible,
        valid_runs,
        eligible_from_valid,
        per_run,
        required_slots,
        max_price,
        min_slots_per_block,
        max_slots_per_block,
    )


def find_optimal_slots(
    candidate_slots: list[dict],
    required_slots: int,
    ready_by_dt: datetime,
    min_block_hours: float,
    max_price: float | None = None,
    max_block_hours: float | None = None,
    algorithm: str = OPTIMIZATION_ALGORITHM_GREEDY,
) -> list[dict]:
    """Select the cheapest combination of slots totalling required_slots, where every
    contiguous block is >= min_block_hours and (if max_price given) each block's average
    raw price <= max_price. Returns a flat list of selected slot dicts, or [] if none.

    algorithm picks which of three implementations does the selection (see
    OPTIMIZATION_ALGORITHM_* in const.py for the tradeoffs):
    - "greedy" (default): _find_optimal_slots_greedy, the original fast-but-occasionally-
      suboptimal algorithm (issue #55) -- unchanged, kept as the default.
    - "optimal": _find_optimal_slots_optimal, an exact search that's always at least as
      cheap as greedy, at the cost of more compute on very long price horizons.
    - "hybrid": _find_optimal_slots_hybrid, a narrowed exact search over a handful of
      representative block sizes per run -- close to greedy's speed, much less likely
      to miss a cheaper single window, but not guaranteed globally optimal.

    max_block_hours (if given and > 0) caps how long any single contiguous block may
    be — a cheap run longer than this is never offered as one big window, so when the
    cheapest slots are spread across separate price dips they get combined into
    several smaller blocks capped at this length rather than one long one. Note this
    is a cap on window size during selection, not an enforced rest period: if the
    single cheapest option in the market genuinely is one long uninterrupted dip,
    capping the window size still results in back-to-back blocks with no gap between
    them, since there's no cost-based reason to leave a cheaper slot unpicked.
    """
    impl = _ALGORITHM_IMPLEMENTATIONS.get(algorithm, _find_optimal_slots_greedy)
    return impl(candidate_slots, required_slots, ready_by_dt, min_block_hours, max_price, max_block_hours)


_ALGORITHM_IMPLEMENTATIONS = {
    OPTIMIZATION_ALGORITHM_GREEDY: _find_optimal_slots_greedy,
    OPTIMIZATION_ALGORITHM_OPTIMAL: _find_optimal_slots_optimal,
    OPTIMIZATION_ALGORITHM_HYBRID: _find_optimal_slots_hybrid,
}


def slots_to_sessions(selected_slots: list[dict]) -> list[dict]:
    """Convert a flat list of slot dicts into session dicts grouped by contiguous run.

    confidence (0-100) is the average base credibility of the slots in the session:
    100 = all actual rates, down to 40 for predictions more than 72h out.

    estimated is True if any slot in the session is not an actual-grade price, i.e.
    the block could still move when the real rates are published -- what to check
    before acting on a block ("charge now or wait for tomorrow's rates").
    """
    if not selected_slots:
        return []
    sorted_slots = sorted(selected_slots, key=lambda s: to_utc(s["date_time"]))
    runs = build_contiguous_runs(sorted_slots)
    sessions = []
    for run in runs:
        start = to_utc(run[0]["date_time"])
        end = to_utc(run[-1]["date_time"]) + timedelta(minutes=30)
        avg_price = sum(s["raw_price"] for s in run) / len(run)
        avg_cred = sum(slot_base_credibility(s) for s in run) / len(run)
        sessions.append(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "duration_hours": len(run) * 0.5,
                "avg_price": round(avg_price, 2),
                "confidence": round(avg_cred * 100, 1),
                "estimated": any(s.get("tier", TIER_ACTUAL) != TIER_ACTUAL for s in run),
            }
        )
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


def _session_span(session: dict) -> tuple[datetime, datetime]:
    """A stored session's (start, end) as UTC instants, however they were persisted."""
    return to_utc(parse_dt(session["start"])), to_utc(parse_dt(session["end"]))


def prune_and_classify(sessions: list[dict], now_dt: datetime) -> tuple[dict | None, list[dict]]:
    """Split sessions into (active_session | None, [future_sessions]). Expired
    sessions (end <= now_dt) are silently dropped.

    Sessions shouldn't normally overlap (the scheduler never generates
    overlapping ones in a single computation), but sessions is long-lived
    persisted state read back across update cycles -- if more than one
    matches "active" (start <= now_dt < end), the first encountered wins
    deterministically and the rest are logged and discarded, rather than the
    previous behavior of silently overwriting `active` with no trace of the
    discarded one, which undercounted compute_hours_remaining (issue #43).
    """
    active = None
    future = []
    now_dt = to_utc(now_dt)
    for s in sessions:
        start, end = _session_span(s)
        if start <= now_dt < end:
            if active is not None:
                _LOGGER.warning("Discarding unexpected overlapping active session %r (keeping %r)", s, active)
                continue
            active = s
        elif end > now_dt:
            future.append(s)
    return active, future


def clip_active_session(
    session: dict, now_dt: datetime, allowed_slots: int, deadline_dt: datetime | None
) -> tuple[dict | None, float]:
    """Cut an in-progress session down to what its tier still allows: at most `allowed_slots`
    half-hours in total and nothing past `deadline_dt`. Returns (session_to_keep, hours_to_credit).

    A session is planned once and then carried forward untouched while it runs, so without this
    a block outlives anything the user changes after it started -- lowering the requirement,
    pausing a tier, pulling the deadline earlier -- and the charger just keeps going until the
    block happens to end.

    If the limit is still ahead the session is shortened (staying on the half-hour grid when the
    allowance is what limits it). If it has been reached already the session stops *now*: it is
    dropped and the whole slots that actually ran are returned for the caller to credit as
    delivered, which an ended session would otherwise have contributed on its own. The input is
    never mutated.
    """
    start, end = _session_span(session)
    now = to_utc(now_dt)
    limit = min(end, start + timedelta(minutes=30 * max(0, allowed_slots)))
    if deadline_dt is not None:
        limit = min(limit, to_utc(deadline_dt))
    if limit >= end:
        return session, 0.0
    if limit <= now:
        # Whole slots only: the delivered-hours accounting rounds a part-slot UP to a whole one,
        # so crediting the exact elapsed time would count time that never ran. Under-crediting
        # at worst re-plans one extra slot; over-crediting leaves the car short.
        whole_slots = math.floor(round((now - start).total_seconds() / 1800, 6))
        return None, max(0, whole_slots) / 2

    clipped = dict(session)
    tzinfo = parse_dt(session["end"]).tzinfo
    clipped["end"] = (limit.astimezone(tzinfo) if tzinfo is not None else limit).isoformat()
    clipped["duration_hours"] = (limit - start).total_seconds() / 3600
    return clipped, 0.0


def compute_hours_remaining(future_sessions: list[dict], active_session: dict | None, now_dt: datetime) -> float:
    """Total uncommenced committed charging time: remaining portion of the active
    session plus all future sessions in full.

    Both branches derive their duration from start/end, not a stored
    duration_hours field -- the future-session branch used to trust
    duration_hours verbatim, which could silently drift out of sync with the
    session's own start/end (a partial write, a manual edit, a future schema
    change) and corrupt this total with no crash to surface it (issue #44).
    """
    total = 0.0
    if active_session:
        _, end = _session_span(active_session)
        total += max(0.0, (end - to_utc(now_dt)).total_seconds() / 3600)
    for s in future_sessions:
        start, end = _session_span(s)
        total += (end - start).total_seconds() / 3600
    return total


def _source_rank(source: str) -> int:
    return _SOURCE_RANK.get(source, _UNKNOWN_SOURCE_RANK)


def deduplicate_and_sort_prices(all_prices: list[dict], now_dt: datetime) -> list[dict]:
    """Merge price data from all sources: when several price the same slot the
    best-ranked source wins (see _SOURCE_RANK -- actual rates, then a short-range
    forecast's final prices, then its estimates, then the long-range forecast), and
    equal ranks keep whichever came first. Discards slots that have already ended,
    sorts the rest chronologically."""
    prices_by_dt: dict = {}
    for p in all_prices:
        dt = to_utc(p["date_time"])
        existing = prices_by_dt.get(dt)
        if existing is None or _source_rank(p["source"]) < _source_rank(existing["source"]):
            prices_by_dt[dt] = p if dt is p["date_time"] else {**p, "date_time": dt}

    result = []
    for dt in sorted(prices_by_dt.keys()):
        p = prices_by_dt[dt]
        if dt + timedelta(minutes=30) > to_utc(now_dt):
            result.append(p)
    return result


def summarize_prices(slots: list[dict], now_dt: datetime, window_hours: float = 24.0) -> dict:
    """Diagnostic summary of a candidate price dataset for the current cycle:
    how many data points, their price range/average, an average restricted to
    the next `window_hours`, and a count per source label — "how much data did
    we have, and what did it look like" independent of whatever got scheduled.
    """
    if not slots:
        return {
            "count": 0,
            "cheapest_price": None,
            "most_expensive_price": None,
            "average_price": None,
            "average_price_next_window": None,
            "source_counts": {},
        }

    prices = [s["raw_price"] for s in slots]
    window_start = to_utc(now_dt)
    window_end = window_start + timedelta(hours=window_hours)
    window_prices = [s["raw_price"] for s in slots if window_start <= to_utc(s["date_time"]) < window_end]

    source_counts: dict[str, int] = {}
    for s in slots:
        source_counts[s["source"]] = source_counts.get(s["source"], 0) + 1

    return {
        "count": len(slots),
        "cheapest_price": round(min(prices), 4),
        "most_expensive_price": round(max(prices), 4),
        "average_price": round(sum(prices) / len(prices), 4),
        "average_price_next_window": round(sum(window_prices) / len(window_prices), 4) if window_prices else None,
        "source_counts": source_counts,
    }


def determine_state(
    required_hours: float,
    boost_active: bool,
    active_session: dict | None,
    future_sessions: list[dict],
    hours_remaining: float,
    data_ok: bool,
) -> str:
    """Return the state name for the current conditions. This reflects what is
    scheduled, independent of the manual charge_override the coordinator applies
    separately to `desired` — the schedule itself stays visible on the dashboard
    regardless of any override in effect."""
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


def next_ready_by(now_dt: datetime, hour: int = 7, min_day_offset: int = 0) -> datetime:
    """The next occurrence of `hour`:00:00 strictly after now_dt, same tzinfo.

    Used both as the default ready_by on first setup and to roll ready_by
    forward automatically once it's reached, so "charge N hours by 7am" is a
    standing daily target rather than something that needs resetting by hand
    every day. If now_dt is already before `hour` today, that's the result
    (e.g. 2am -> 7am *today*, a few hours away); otherwise it's tomorrow.

    min_day_offset forces the result at least that many days ahead of today
    (0 = as soon as possible — today if `hour` hasn't passed yet, otherwise
    tomorrow; 1 = at least tomorrow; 2 = at least the day after tomorrow;
    etc.), matching the "Next day / Next day+1/+2/+3" options configurable
    at setup time.
    """
    candidate = _snap_to_real_time(
        now_dt.replace(hour=hour, minute=0, second=0, microsecond=0) + timedelta(days=min_day_offset)
    )
    if to_utc(candidate) <= to_utc(now_dt):
        candidate = _snap_to_real_time(candidate + timedelta(days=1))
    return candidate


def _snap_to_real_time(dt: datetime) -> datetime:
    """`dt` as a local time that really occurred, still in its own timezone.

    Wall-clock arithmetic (`replace()`, adding days) can land on a local time that never
    happened -- on a "spring forward" day where `hour` falls in the skipped gap (e.g.
    1am->2am somewhere becomes 2am->3am). Round-tripping through UTC snaps it to the real
    local time at that instant instead of silently keeping an invalid offset (#27).
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(UTC).astimezone(dt.tzinfo)


def add_local_days(dt: datetime, days: int) -> datetime:
    """`dt` moved `days` calendar days on the wall clock, so 07:00 stays 07:00 across a DST
    change (rather than drifting by an hour, as adding 24h*days would), snapped to a real
    local time if that wall time doesn't exist."""
    return _snap_to_real_time(dt + timedelta(days=days))
