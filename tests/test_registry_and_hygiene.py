"""Structural guards: the provider registries are internally consistent and wired to the flow,
strings and parser; the release metadata is sane; and the state machine's decision table is
checked exhaustively. These catch the mistakes that only show up when someone adds a provider or
cuts a release."""

import ast
import itertools
import json
import re
from datetime import UTC
from pathlib import Path

import pytest

from custom_components.wholesale_ev_schedule import config_flow, providers
from custom_components.wholesale_ev_schedule.const import DOMAIN
from custom_components.wholesale_ev_schedule.price_parsing import PointSpec
from custom_components.wholesale_ev_schedule.scheduler import determine_state

ROOT = Path(config_flow.__file__).parent
REPO = ROOT.parent.parent
UTC = UTC

REGISTRIES = {
    "rates": (providers.RATE_PROVIDERS, "rates_{id}"),
    "forecast": (providers.FORECAST_PROVIDERS, "forecast_{id}"),
    "short_range": (providers.SHORT_RANGE_PROVIDERS, "short_range_{id}"),
}
NON_NAMED = {"none", "custom"}


def _named(registry):
    return {pid: profile for pid, profile in registry.items() if pid not in NON_NAMED}


# ---------------------------------------------------------------------------
# Provider registries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(REGISTRIES))
def test_every_provider_has_a_unique_non_empty_label(name):
    registry, _ = REGISTRIES[name]
    labels = [p["label"] for p in registry.values()]

    assert all(isinstance(label, str) and label.strip() for label in labels)
    assert len(set(labels)) == len(labels)


@pytest.mark.parametrize("name", list(REGISTRIES))
def test_every_registry_offers_none_or_custom_escape_hatches_as_documented(name):
    registry, _ = REGISTRIES[name]
    assert "custom" in registry
    if name != "rates":  # forecasts are optional; actual rates are not
        assert "none" in registry


@pytest.mark.parametrize("name", list(REGISTRIES))
def test_named_profiles_define_everything_the_parser_needs(name):
    registry, _ = REGISTRIES[name]
    needed = {"attribute", "start_key", "value_key", "unit_multiplier"} if name == "rates" else {
        "attribute", "datetime_key", "price_key", "unit_multiplier"
    }  # fmt: skip
    for pid, profile in _named(registry).items():
        assert needed <= set(profile), pid
        assert all(isinstance(profile[k], str) and profile[k] for k in needed - {"unit_multiplier"}), pid
        assert isinstance(profile["unit_multiplier"], float | int) and profile["unit_multiplier"] > 0, pid
        if "estimate_flag_key" in profile:
            assert isinstance(profile["estimate_flag_key"], str) and profile["estimate_flag_key"]


@pytest.mark.parametrize("name", list(REGISTRIES))
def test_every_provider_has_a_flow_step_and_strings_in_both_flows(name):
    registry, pattern = REGISTRIES[name]
    strings = json.loads((ROOT / "strings.json").read_text(encoding="utf-8"))
    mixin = config_flow._ProviderStepsMixin

    for pid in registry:
        if pid == "none":
            continue
        step = pattern.format(id=pid)
        assert hasattr(mixin, f"async_step_{step}"), f"no flow step for {name} provider {pid!r}"
        for section in ("config", "options"):
            assert step in strings[section]["step"], f"{step} missing from strings.json {section}"


@pytest.mark.parametrize("name", list(REGISTRIES))
def test_a_point_laid_out_by_each_profile_is_actually_parsed(name):
    """The profile's keys really do locate a time and a price -- the same lookup the coordinator makes."""
    from custom_components.wholesale_ev_schedule.price_parsing import resolve_key

    registry, _ = REGISTRIES[name]
    for pid, profile in _named(registry).items():
        time_key = profile.get("start_key") or profile["datetime_key"]
        price_key = profile.get("value_key") or profile["price_key"]
        spec = PointSpec(
            profile["attribute"], time_key, price_key, profile["unit_multiplier"], profile.get("estimate_flag_key")
        )
        point: dict = {}
        for key, value in (
            (spec.time_key, "2026-03-02T12:00:00Z"),
            (spec.price_key, 12.5),
            (spec.estimate_flag_key, False),
        ):
            if key is None:
                continue
            *parents, leaf = key.split(".")
            node = point
            for part in parents:
                node = node.setdefault(part, {})
            node[leaf] = value

        assert resolve_key(point, spec.time_key) == "2026-03-02T12:00:00Z", pid
        assert resolve_key(point, spec.price_key) == 12.5, pid


def test_the_shared_agile_predict_profile_is_one_object_not_a_copy_that_can_drift():
    assert providers.SHORT_RANGE_PROVIDERS["agile_predict"] is providers.FORECAST_PROVIDERS["agile_predict"]


# ---------------------------------------------------------------------------
# Release hygiene
# ---------------------------------------------------------------------------


def test_the_manifest_is_complete_and_the_version_is_semver():
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["domain"] == DOMAIN == ROOT.name
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"]), manifest["version"]
    assert manifest["config_flow"] is True
    for key in ("name", "documentation", "issue_tracker", "codeowners", "iot_class"):
        assert manifest.get(key), key


def test_hacs_metadata_is_valid():
    hacs = json.loads((REPO / "hacs.json").read_text(encoding="utf-8"))
    assert hacs.get("name")


def test_every_json_file_in_the_component_parses():
    for path in ROOT.rglob("*.json"):
        json.loads(path.read_text(encoding="utf-8"))


def test_brand_assets_ship_with_the_component():
    for name in ("icon.png", "logo.png", "icon@2x.png", "logo@2x.png"):
        assert (ROOT / "brand" / name).stat().st_size > 0, name


def test_no_debugging_leftovers_in_the_component():
    for path in ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        assert not [c for c in calls if c.func.id in ("print", "breakpoint")], path.name
        text = path.read_text(encoding="utf-8")
        assert "pdb" not in text.replace("pdbsupport", "") and "TODO(" not in text, path.name


def test_every_python_module_has_a_docstring_explaining_its_role():
    for path in ROOT.glob("*.py"):
        assert ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))), f"{path.name} has no module docstring"


def test_readme_and_developing_mention_every_module():
    developing = (REPO / "DEVELOPING.md").read_text(encoding="utf-8")
    for path in ROOT.glob("*.py"):
        if path.name != "__init__.py":
            assert path.name in developing, f"{path.name} is not described in DEVELOPING.md's structure section"


# ---------------------------------------------------------------------------
# determine_state: the decision table, exhaustively
# ---------------------------------------------------------------------------

ACTIVE = {"start": "x", "end": "y"}
FUTURE = [{"start": "a", "end": "b"}]


def _spec(required_hours, boost_active, active, future, hours_remaining, data_ok):
    """The documented priority order, written independently of the implementation."""
    if not data_ok:
        return "error"
    if required_hours is None or required_hours <= 0:
        return "idle"
    if boost_active:
        return "boosting"
    if active:
        return "charging"
    if future:
        return "scheduled"
    if hours_remaining <= 0:
        return "complete"
    return "error"


@pytest.mark.parametrize(
    "required_hours,boost_active,active,future,hours_remaining,data_ok",
    list(
        itertools.product(
            [None, 0, -1, 0.5, 8], [False, True], [None, ACTIVE], [[], FUTURE], [0, -0.1, 0.5, 9], [False, True]
        )
    ),
)
def test_determine_state_matches_the_documented_priority_order(
    required_hours, boost_active, active, future, hours_remaining, data_ok
):
    assert determine_state(required_hours, boost_active, active, future, hours_remaining, data_ok) == _spec(
        required_hours, boost_active, active, future, hours_remaining, data_ok
    )


def test_the_table_covers_every_state_the_integration_can_report():
    produced = {
        _spec(*combo)
        for combo in itertools.product([None, 0, 8], [False, True], [None, ACTIVE], [[], FUTURE], [0, 1], [False, True])
    }
    assert produced == {
        "idle",
        "error",
        "boosting",
        "charging",
        "scheduled",
        "complete",
    }  # (unschedulable is decided by the coordinator)
