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

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

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
