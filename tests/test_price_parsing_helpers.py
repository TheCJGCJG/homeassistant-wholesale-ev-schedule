"""Unit tests for price_parsing.py: dotted-path key resolution and estimate-flag
interpretation. Pure functions, so no hass fixture -- these are the edge cases that
decide whether a real-world source's JSON is read correctly, and the ones where a
silent misread would schedule real charging on the wrong prices.
"""

from types import MappingProxyType

import pytest

from custom_components.wholesale_ev_schedule.price_parsing import PointSpec, is_estimate, resolve_key

# ---------------------------------------------------------------------------
# resolve_key
# ---------------------------------------------------------------------------


def test_flat_key_resolves_as_before():
    assert resolve_key({"value_inc_vat": 0.25}, "value_inc_vat") == 0.25


def test_dotted_path_resolves_through_nested_dicts():
    point = {"agileRate": {"result": {"rate": 40.9, "prediction": False}}}
    assert resolve_key(point, "agileRate.result.rate") == 40.9
    assert resolve_key(point, "agileRate.result.prediction") is False


def test_literal_top_level_key_containing_a_dot_wins_over_the_path_reading():
    """An existing custom config whose flat key happens to contain a dot must keep
    resolving exactly as it did before dotted paths existed."""
    point = {"a.b": "flat", "a": {"b": "nested"}}
    assert resolve_key(point, "a.b") == "flat"


def test_dotted_path_is_used_only_when_no_literal_key_matches():
    assert resolve_key({"a": {"b": "nested"}}, "a.b") == "nested"


@pytest.mark.parametrize(
    "point,key",
    [
        ({}, "missing"),
        ({"a": {}}, "a.b"),
        ({"a": {"b": 1}}, "a.c"),
        ({"a": {"b": 1}}, "x.b"),
        ({"a": {"b": 1}}, "a.b.c"),  # descends into a non-mapping
        ({"a": "text"}, "a.b"),
        ({"a": None}, "a.b"),
        ({"a": [1, 2]}, "a.0"),  # list indexing is deliberately unsupported
        ({"a": {"b": 1}}, "a..b"),  # empty path segment
        ({"a": {"b": 1}}, ".a"),
        ({"a": {"b": 1}}, "a."),
        ({"a": {"b": 1}}, ""),
    ],
)
def test_unresolvable_keys_return_none_rather_than_raising(point, key):
    assert resolve_key(point, key) is None


@pytest.mark.parametrize("point", [None, "a string", 42, 3.5, ["a", "b"], ("a",), True])
def test_non_mapping_points_resolve_to_none_so_the_row_is_skipped(point):
    assert resolve_key(point, "a") is None
    assert resolve_key(point, "a.b") is None


def test_read_only_mappings_work_like_dicts():
    """HA hands back ReadOnlyDict / MappingProxyType-style attribute values."""
    point = MappingProxyType({"agileRate": MappingProxyType({"result": MappingProxyType({"rate": 12.5})})})
    assert resolve_key(point, "agileRate.result.rate") == 12.5


def test_an_explicit_none_value_is_returned_as_none():
    assert resolve_key({"rate": None}, "rate") is None
    assert resolve_key({"a": {"rate": None}}, "a.rate") is None


def test_falsy_values_are_returned_not_confused_with_missing():
    assert resolve_key({"rate": 0}, "rate") == 0
    assert resolve_key({"a": {"flag": False}}, "a.flag") is False
    assert resolve_key({"a": {"s": ""}}, "a.s") == ""


# ---------------------------------------------------------------------------
# is_estimate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flag", [True, 1, 1.0, 2, -1, "true", "True", "TRUE", " true ", "yes", "on", "1", "maybe", "x"]
)
def test_truthy_or_unrecognised_flags_are_estimates(flag):
    assert is_estimate(flag) is True


@pytest.mark.parametrize("flag", [False, 0, 0.0, "false", "False", "FALSE", " false ", "0", "no", "off", "Off"])
def test_explicitly_final_flags_are_not_estimates(flag):
    assert is_estimate(flag) is False


@pytest.mark.parametrize("flag", [None, [], {}, [False], {"a": False}, object(), float("nan"), ""])
def test_missing_or_garbage_flags_are_conservatively_estimates(flag):
    """Wrongly trusting a still-moving price is worse than wrongly distrusting a final
    one, so anything that isn't an explicit 'final' reads as an estimate."""
    assert is_estimate(flag) is True


# ---------------------------------------------------------------------------
# PointSpec
# ---------------------------------------------------------------------------


def test_point_spec_is_immutable_and_flag_key_defaults_to_none():
    spec = PointSpec(attribute="rates", time_key="start", price_key="value", multiplier=100.0)
    assert spec.estimate_flag_key is None
    with pytest.raises(AttributeError):
        spec.multiplier = 1.0  # type: ignore[misc]


def test_point_specs_with_equal_fields_are_equal_and_hashable():
    a = PointSpec("rates", "start", "value", 1.0, "flag")
    b = PointSpec("rates", "start", "value", 1.0, "flag")
    assert a == b
    assert len({a, b}) == 1
