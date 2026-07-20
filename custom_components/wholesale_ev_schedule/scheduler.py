"""Pure scheduling logic for Wholesale EV Schedule — no Home Assistant dependency.

Ported from https://github.com/TheCJGCJG/homeassistant-pyscripts
(src/ev_charging_state_machine.py), which carries a well-exercised test suite for
this algorithm. Keeping these functions free of HA imports keeps them unit-testable
in isolation, same as the source project.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

_LOGGER = logging.getLogger(__name__)

# Mirrors const.py's OPTIMIZATION_ALGORITHM_* values, duplicated as plain string
# literals rather than imported -- const.py pulls in homeassistant.const, which would
# break this module's deliberate no-HA-dependency contract (see module docstring).
OPTIMIZATION_ALGORITHM_GREEDY = "greedy"
OPTIMIZATION_ALGORITHM_OPTIMAL = "optimal"
OPTIMIZATION_ALGORITHM_HYBRID = "hybrid"

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
    prices, favouring known actual rates; at 100 all prices are taken at face value.

    gamble_tolerance is clamped to [0, 100] -- its only intended domain. Above
    100, eff_cred exceeds 1.0 for predicted tiers, making a predicted price
    *cheaper* than an equally-priced actual one and inverting the documented
    ranking (issue #41). Sufficiently below 0, eff_cred crosses zero and this
    function divides by it (issue #42). The live number entity already
    clamps to [0, 100], but a stored value can bypass that (see coordinator.py).
    """
    gamble_tolerance = max(0.0, min(100.0, gamble_tolerance))
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

    eligible = [s for s in candidate_slots if s["date_time"] + timedelta(minutes=30) <= ready_by_dt]
    eligible.sort(key=lambda s: s["date_time"])

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

    selected_windows, remaining = _greedy_select(strict=True)
    if remaining == required_slots and windows:
        # Strict selection made zero progress -- e.g. required_slots is split across
        # separate runs that are each too short to leave a full-size remainder on
        # their own (see issue #23). Rather than report unschedulable outright, relax
        # the "leave a neat remainder" preference so at least one window gets picked,
        # and let the leftover-fallback below top up whatever's left.
        selected_windows, remaining = _greedy_select(strict=False)

    # Only the (few) selected windows' actual date_times are resolved here --
    # deferring this from window-generation time is exactly what keeps that
    # loop O(1) per window instead of O(size).
    selected_dts: set = set()
    for w in selected_windows:
        run = valid_runs[w["run_id"]]
        for s in run[w["start_idx"] : w["start_idx"] + w["size"]]:
            selected_dts.add(s["date_time"])

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
    unbounded = max_slots_per_block is None
    hi = lo if unbounded else max_slots_per_block

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

    # candidates is never empty: the caller has already checked
    # len(eligible_from_valid) >= required_slots, so j=0 (combined[0]=0.0, no whole
    # blocks) always has at least `required_slots` leftover slots to top up with.
    candidates: list[tuple[set, float]] = []
    for j in range(required_slots + 1):
        if combined[j] == math.inf:
            continue
        whole_block_dts = _backtrack(j) if j > 0 else set()
        remaining = required_slots - j
        if remaining == 0:
            candidates.append((whole_block_dts, combined[j]))
            continue
        leftover = sorted(
            (s for s in eligible_from_valid if s["date_time"] not in whole_block_dts),
            key=lambda s: s["effective_price"],
        )
        topped_up = set(whole_block_dts)
        added_cost = 0.0
        for s in leftover[:remaining]:
            topped_up.add(s["date_time"])
            added_cost += s["effective_price"]
        candidates.append((topped_up, combined[j] + added_cost))

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

    eligible = [s for s in candidate_slots if s["date_time"] + timedelta(minutes=30) <= ready_by_dt]
    eligible.sort(key=lambda s: s["date_time"])

    all_runs = build_contiguous_runs(eligible)
    valid_runs = [r for r in all_runs if len(r) >= min_slots_per_block]
    eligible_from_valid = [s for run in valid_runs for s in run]

    if len(eligible_from_valid) < required_slots:
        return []

    per_run = [
        _min_cost_for_run(run, required_slots, min_slots_per_block, max_slots_per_block, max_price)
        for run in valid_runs
    ]

    return _solve_with_per_run_costs(eligible, valid_runs, eligible_from_valid, per_run, required_slots, max_price)


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

    eligible = [s for s in candidate_slots if s["date_time"] + timedelta(minutes=30) <= ready_by_dt]
    eligible.sort(key=lambda s: s["date_time"])

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

    return _solve_with_per_run_costs(eligible, valid_runs, eligible_from_valid, per_run, required_slots, max_price)


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
        sessions.append(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "duration_hours": len(run) * 0.5,
                "avg_price": round(avg_price, 2),
                "confidence": round(avg_cred * 100, 1),
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
    for s in sessions:
        start = parse_dt(s["start"])
        end = parse_dt(s["end"])
        if start <= now_dt < end:
            if active is not None:
                _LOGGER.warning("Discarding unexpected overlapping active session %r (keeping %r)", s, active)
                continue
            active = s
        elif end > now_dt:
            future.append(s)
    return active, future


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
        end = parse_dt(active_session["end"])
        total += max(0.0, (end - now_dt).total_seconds() / 3600)
    for s in future_sessions:
        start = parse_dt(s["start"])
        end = parse_dt(s["end"])
        total += (end - start).total_seconds() / 3600
    return total


def deduplicate_and_sort_prices(all_prices: list[dict], now_dt: datetime) -> list[dict]:
    """Merge price data from all sources: actual rates win over predicted for the
    same slot. Discards slots that have already ended, sorts the rest chronologically."""
    prices_by_dt: dict = {}
    for p in all_prices:
        dt = p["date_time"]
        is_actual = p["source"] in ("current_actual", "next_actual")
        existing = prices_by_dt.get(dt)
        if existing is None or is_actual and existing["source"] not in ("current_actual", "next_actual"):
            prices_by_dt[dt] = p

    result = []
    for dt in sorted(prices_by_dt.keys()):
        p = prices_by_dt[dt]
        if dt + timedelta(minutes=30) > now_dt:
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
    window_end = now_dt + timedelta(hours=window_hours)
    window_prices = [s["raw_price"] for s in slots if now_dt <= s["date_time"] < window_end]

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
    candidate = now_dt.replace(hour=hour, minute=0, second=0, microsecond=0) + timedelta(days=min_day_offset)
    if candidate.tzinfo is not None:
        # `replace()` does naive wall-clock arithmetic -- on a DST transition day
        # where `hour` falls in the skipped "spring forward" gap (e.g. 1am->2am
        # somewhere becomes 2am->3am), the result may be a local time that never
        # occurred. Round-tripping through UTC snaps it to the real local time
        # at that instant instead of silently keeping an invalid offset (#27).
        candidate = candidate.astimezone(timezone.utc).astimezone(candidate.tzinfo)
    if candidate <= now_dt:
        candidate += timedelta(days=1)
    return candidate
