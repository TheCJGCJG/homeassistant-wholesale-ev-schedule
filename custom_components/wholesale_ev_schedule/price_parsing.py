"""Pure helpers for reading price points out of an entity's attributes -- no Home
Assistant dependency, so they're unit-tested directly (tests/test_price_parsing_helpers.py)
and the coordinator only has to wire them to real entity state.

Every price source this integration reads (actual rates, long-range forecast,
short-range forecast) has the same shape: an attribute holding a list of points,
each carrying a time and a price. `PointSpec` captures where those live so one
parser serves all three (see coordinator.py's `_parse_points`) instead of a copy
per source.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .scheduler import to_utc

# Strings a source might use for "this is not an estimate". Anything else, or
# anything missing, is read as an estimate -- see is_estimate.
_FINAL_STRINGS = frozenset({"false", "0", "no", "off"})


@dataclass(frozen=True)
class PointSpec:
    """Where a source keeps its price points: `attribute` is the entity attribute
    holding the list; `time_key`/`price_key` locate each point's start time and
    price (dotted paths allowed, see resolve_key); `multiplier` converts the
    source's unit to the integration's (p/kWh). `estimate_flag_key` is optional:
    when set, it locates a per-point flag saying whether the price is still an
    estimate (see is_estimate)."""

    attribute: str
    time_key: str
    price_key: str
    multiplier: float
    estimate_flag_key: str | None = None


def resolve_key(point: Any, key: str) -> Any:
    """The value of `key` in `point`, or None if it isn't there.

    `key` may be a dotted path into nested dicts ("agileRate.result.rate"), which
    some sources need. A literal top-level key always wins over the path reading,
    so an existing config whose flat key happens to contain a dot ("a.b") keeps
    resolving exactly as it did before dotted paths existed. A non-mapping point
    (a malformed row) resolves to None so the caller skips it.
    """
    if not isinstance(point, Mapping):
        return None
    if key in point:
        return point[key]
    if "." not in key:
        return None
    node: Any = point
    for part in key.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def is_estimate(flag: Any) -> bool:
    """Whether a source's per-point estimate flag marks the price as an estimate.

    False only when the source explicitly says the price is final (False, 0, or a
    string like "false"). A missing flag or anything unrecognised reads as an
    estimate: wrongly trusting a still-moving price is worse than wrongly
    distrusting a final one, since a final price will simply be superseded by the
    actual rates, while an estimate treated as final would be scheduled at full
    confidence.
    """
    if flag is None:
        return True
    if isinstance(flag, bool):
        return flag
    if isinstance(flag, int | float):
        return flag != 0
    if isinstance(flag, str):
        return flag.strip().lower() not in _FINAL_STRINGS
    return True


# -- Putting a source on the scheduler's 30-minute grid ------------------------------------------

_HALF_HOUR = timedelta(minutes=30)
# Regular feeds finer than half-hourly that divide a half hour evenly, and the one coarser grid
# (hourly) that is common in day-ahead markets. Anything else is left exactly as it came.
_FINE_GRID_MINUTES = frozenset({1, 2, 3, 5, 6, 10, 15})
_COARSE_GRID_MINUTES = 60
_MIN_POINTS_TO_JUDGE = 4
_MIN_REGULARITY = 0.8  # share of consecutive gaps that must equal the feed's usual gap


def normalize_to_half_hours(entries: list[tuple[datetime, float, Any]]) -> list[tuple[datetime, float, Any]]:
    """Put a regular non-half-hourly feed on the 30-minute grid the scheduler works in.

    `entries` are (start, price, originating_point). The scheduler assumes every slot is a
    30-minute slot on the UTC half-hour grid; fed anything else, a 15-minute feed produces
    overlapping sessions and an hourly one prices only half of each hour. So a feed whose gaps
    are regularly 15 (or 10, 5, ...) minutes has its points averaged into the half hour they
    fall in, and one that is regularly hourly has each point cover two slots.

    Deliberately conservative: it acts only when the feed is long enough to judge (4+ distinct
    starts) and at least 80% of consecutive gaps equal its usual gap, and never on a grid it does
    not recognise. A half-hourly feed (the common case) and anything irregular is returned
    unchanged -- guessing the grid of a patchy feed would invent prices. When it does act,
    results are UTC instants in time order, the first occurrence of a duplicated start wins, and
    a genuine point is never replaced by a synthesised one.
    """
    if len(entries) < _MIN_POINTS_TO_JUDGE:
        return entries
    by_start: dict[datetime, tuple[float, Any]] = {}
    for start, price, point in entries:
        by_start.setdefault(to_utc(start), (price, point))
    starts = sorted(by_start)
    if len(starts) < _MIN_POINTS_TO_JUDGE:
        return entries

    gap_seconds = [(b - a).total_seconds() for a, b in zip(starts, starts[1:], strict=False)]
    usual_gap, occurrences = Counter(gap_seconds).most_common(1)[0]
    if occurrences / len(gap_seconds) < _MIN_REGULARITY or usual_gap % 60:
        return entries
    minutes = int(usual_gap // 60)

    if minutes in _FINE_GRID_MINUTES:
        buckets: dict[datetime, list[tuple[float, Any]]] = {}
        for start in starts:
            bucket = start.replace(minute=0 if start.minute < 30 else 30, second=0, microsecond=0)
            buckets.setdefault(bucket, []).append(by_start[start])
        return [(bucket, sum(p for p, _ in group) / len(group), group[0][1]) for bucket, group in buckets.items()]

    if minutes == _COARSE_GRID_MINUTES:
        expanded: dict[datetime, tuple[float, Any]] = dict(by_start)
        for start in starts:
            expanded.setdefault(start + _HALF_HOUR, by_start[start])
        return [(start, price, point) for start, (price, point) in sorted(expanded.items())]

    return entries
