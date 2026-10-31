"""The entity layer through real Home Assistant service calls: what a dashboard, automation or voice
assistant actually touches. Numbers, selects, the datetime and buttons are driven with
`<domain>.<service>`; metadata is checked against what is registered; and the sensor attribute
contract is checked in every state the integration can be in."""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.wholesale_ev_schedule import config_flow
from custom_components.wholesale_ev_schedule.const import DOMAIN
from custom_components.wholesale_ev_schedule.scheduler import parse_dt

from .factories import EXPECTED_ENTITY_IDS, OPPORTUNISTIC_OPTIONS, expected_opportunistic_entity_ids
from .sim import UTC, PriceWorld, Sim

COMPONENT_DIR = Path(config_flow.__file__).parent
START = "2026-03-02 16:00:00+00:00"
PREFIX = "wholesale_ev_schedule"
REJECTED = (vol.Invalid, HomeAssistantError)


async def _sim(hass, freezer, **kwargs):
    kwargs.setdefault("start", START)
    kwargs.setdefault("required_hours", 3.0)
    kwargs.setdefault("world", PriceWorld(seed=1))
    return await Sim.create(hass, freezer, **kwargs)


async def _call(hass, domain, service, entity, **data):
    await hass.services.async_call(domain, service, {"entity_id": entity, **data}, blocking=True)
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------

# entity suffix -> (min, max, unit, the coordinator attribute it drives)
NUMBERS = {
    "charging_hours_required": (0.0, 24.0, "h", "required_hours"),
    "gamble_tolerance": (0.0, 100.0, "%", "gamble_tolerance"),
    "min_block_hours": (0.0, 24.0, "h", "min_block_hours"),
    "max_price": (0.0, 200.0, None, "max_price"),
    "assumed_charge_kwh": (0.0, 150.0, "kWh", "assumed_charge_kwh"),
}
OPPORTUNISTIC_NUMBERS = {
    "opportunistic_target_hours": (0.0, 24.0, "h", "opportunistic_target_hours"),
    "opportunistic_max_price": (0.0, 200.0, None, "opportunistic_max_price"),
}


@pytest.mark.parametrize("suffix", list(NUMBERS))
async def test_a_number_accepts_its_boundaries_and_reads_them_back(hass, freezer, suffix):
    low, high, unit, attr = NUMBERS[suffix]
    sim = await _sim(hass, freezer)
    entity = f"number.{PREFIX}_{suffix}"

    for value in (low, high, (low + high) / 2):
        sim.setting_edits = True
        await _call(hass, "number", "set_value", entity, value=value)
        assert getattr(sim.coordinator, attr) == value
        assert float(hass.states.get(entity).state) == value

    state = hass.states.get(entity)
    assert (state.attributes["min"], state.attributes["max"]) == (low, high)
    assert state.attributes.get("unit_of_measurement") == unit


@pytest.mark.parametrize("suffix", list(NUMBERS))
@pytest.mark.parametrize("direction", ["below", "above"])
async def test_a_number_rejects_values_outside_its_range_and_keeps_the_old_one(hass, freezer, suffix, direction):
    low, high, _, attr = NUMBERS[suffix]
    sim = await _sim(hass, freezer)
    before = getattr(sim.coordinator, attr)

    with pytest.raises(REJECTED):
        await _call(
            hass,
            "number",
            "set_value",
            f"number.{PREFIX}_{suffix}",
            value=low - 0.5 if direction == "below" else high + 0.5,
        )

    assert getattr(sim.coordinator, attr) == before


@pytest.mark.parametrize("suffix", list(NUMBERS))
async def test_a_number_survives_a_restart(hass, freezer, suffix):
    low, high, _, attr = NUMBERS[suffix]
    sim = await _sim(hass, freezer)
    value = round((low + high) / 4, 1) or 1.0
    sim.setting_edits = True
    await _call(hass, "number", "set_value", f"number.{PREFIX}_{suffix}", value=value)

    await sim.restart()

    assert getattr(sim.coordinator, attr) == value
    assert float(hass.states.get(f"number.{PREFIX}_{suffix}").state) == value


@pytest.mark.parametrize("suffix", list(OPPORTUNISTIC_NUMBERS))
async def test_the_opportunistic_numbers_behave_the_same_way(hass, freezer, suffix):
    low, high, _, attr = OPPORTUNISTIC_NUMBERS[suffix]
    sim = await _sim(hass, freezer, options=OPPORTUNISTIC_OPTIONS)
    entity = f"number.{PREFIX}_{suffix}"

    for value in (low, high):
        sim.setting_edits = True
        await _call(hass, "number", "set_value", entity, value=value)
        assert getattr(sim.coordinator, attr) == value
    with pytest.raises(REJECTED):
        await _call(hass, "number", "set_value", entity, value=high + 1)
    await sim.restart()
    assert getattr(sim.coordinator, attr) == high


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("suffix", ["charging_hours_required", "max_price", "gamble_tolerance"])
async def test_non_finite_numbers_never_reach_the_coordinator_or_storage(hass, freezer, suffix, bad):
    sim = await _sim(hass, freezer)
    attr = NUMBERS[suffix][3]
    before = getattr(sim.coordinator, attr)

    with pytest.raises(Exception):  # noqa: B017 - HA, voluptuous or our guard: any refusal will do
        await _call(hass, "number", "set_value", f"number.{PREFIX}_{suffix}", value=bad)

    assert getattr(sim.coordinator, attr) == before
    await sim.restart()
    assert getattr(sim.coordinator, attr) == before


# ---------------------------------------------------------------------------
# Boost slider
# ---------------------------------------------------------------------------


async def test_the_boost_slider_starts_a_boost_and_snaps_back_to_zero(hass, freezer):
    sim = await _sim(hass, freezer)
    entity = f"number.{PREFIX}_boost_duration_hours"
    sim.setting_edits = True

    await _call(hass, "number", "set_value", entity, value=2.0)

    assert sim.data["state"] == "boosting" and sim.data["desired"] is True
    assert float(hass.states.get(entity).state) == 0.0  # so it cannot silently re-trigger later
    assert parse_dt(sim.data["boost_end"]) - sim.now == timedelta(hours=2)


async def test_setting_the_boost_slider_to_zero_does_nothing(hass, freezer):
    sim = await _sim(hass, freezer)
    sim.setting_edits = True
    await _call(hass, "number", "set_value", f"number.{PREFIX}_boost_duration_hours", value=0.0)

    assert sim.data["state"] != "boosting"


async def test_a_second_boost_replaces_the_first_rather_than_stacking(hass, freezer):
    sim = await _sim(hass, freezer)
    entity = f"number.{PREFIX}_boost_duration_hours"
    sim.setting_edits = True
    await _call(hass, "number", "set_value", entity, value=4.0)
    await sim.step(30)

    await _call(hass, "number", "set_value", entity, value=1.0)

    assert parse_dt(sim.data["boost_end"]) - sim.now == timedelta(hours=1)


async def test_the_boost_slider_does_not_exceed_eight_hours(hass, freezer):
    await _sim(hass, freezer)
    with pytest.raises(REJECTED):
        await _call(hass, "number", "set_value", f"number.{PREFIX}_boost_duration_hours", value=8.5)


# ---------------------------------------------------------------------------
# Selects, switch, datetime, buttons
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("option", ["auto", "force_on", "force_off"])
async def test_every_charge_override_option_is_selectable_and_effective(hass, freezer, option):
    sim = await _sim(hass, freezer)
    sim.setting_edits = True

    await _call(hass, "select", "select_option", f"select.{PREFIX}_charge_override", option=option)

    assert sim.coordinator.charge_override == option
    assert hass.states.get(f"select.{PREFIX}_charge_override").state == option
    assert sim.data["desired"] is (option == "force_on")


@pytest.mark.parametrize("option", ["greedy", "optimal", "hybrid"])
async def test_every_algorithm_option_is_selectable_and_keeps_a_valid_plan(hass, freezer, option):
    sim = await _sim(hass, freezer)
    sim.setting_edits = True

    await _call(hass, "select", "select_option", f"select.{PREFIX}_optimization_algorithm", option=option)

    assert sim.coordinator.optimization_algorithm == option
    assert sim.data["state"] == "scheduled"


@pytest.mark.parametrize(
    "entity,bad",
    [("charge_override", "sometimes"), ("charge_override", ""), ("optimization_algorithm", "magic")],
)
async def test_selects_reject_unknown_options(hass, freezer, entity, bad):
    sim = await _sim(hass, freezer)
    before = (sim.coordinator.charge_override, sim.coordinator.optimization_algorithm)

    with pytest.raises(REJECTED):
        await _call(hass, "select", "select_option", f"select.{PREFIX}_{entity}", option=bad)

    assert (sim.coordinator.charge_override, sim.coordinator.optimization_algorithm) == before


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-03-03T06:30:00+00:00", datetime(2026, 3, 3, 6, 30, tzinfo=UTC)),
        ("2026-03-03T07:30:00+01:00", datetime(2026, 3, 3, 6, 30, tzinfo=UTC)),
    ],
)
async def test_the_ready_by_datetime_is_set_through_the_service_in_any_offset(hass, freezer, raw, expected):
    sim = await _sim(hass, freezer)
    sim.setting_edits = True

    await _call(hass, "datetime", "set_value", f"datetime.{PREFIX}_ready_by", datetime=raw)

    assert sim.coordinator.ready_by == expected
    assert parse_dt(hass.states.get(f"datetime.{PREFIX}_ready_by").state) == expected


async def test_ready_by_survives_a_restart_to_the_minute(hass, freezer):
    sim = await _sim(hass, freezer)
    sim.setting_edits = True
    await _call(hass, "datetime", "set_value", f"datetime.{PREFIX}_ready_by", datetime="2026-03-04T05:17:00+00:00")

    await sim.restart()

    assert sim.coordinator.ready_by == datetime(2026, 3, 4, 5, 17, tzinfo=UTC)


async def test_the_opportunistic_switch_and_datetime_exist_only_when_enabled_and_work(hass, freezer):
    sim = await _sim(hass, freezer, options=OPPORTUNISTIC_OPTIONS)
    sim.setting_edits = True

    await _call(hass, "switch", "turn_on", f"switch.{PREFIX}_opportunistic_charging_enabled")
    assert sim.coordinator.opportunistic_enabled is True
    await _call(hass, "switch", "toggle", f"switch.{PREFIX}_opportunistic_charging_enabled")
    assert sim.coordinator.opportunistic_enabled is False
    await _call(
        hass, "datetime", "set_value", f"datetime.{PREFIX}_opportunistic_ready_by", datetime="2026-03-05T07:00:00+00:00"
    )
    assert sim.coordinator.opportunistic_ready_by == datetime(2026, 3, 5, 7, 0, tzinfo=UTC)


async def test_pressing_stop_through_the_button_goes_idle(hass, freezer):
    sim = await _sim(hass, freezer)
    sim.setting_edits = True
    await _call(hass, "button", "press", f"button.{PREFIX}_stop")
    assert sim.data["state"] == "idle" and sim.coordinator.required_hours == 0.0


async def test_pressing_reset_through_the_button_restores_defaults(hass, freezer):
    sim = await _sim(hass, freezer)
    sim.setting_edits = True
    await _call(hass, "number", "set_value", f"number.{PREFIX}_max_price", value=77.0)
    await _call(hass, "button", "press", f"button.{PREFIX}_reset")
    assert sim.coordinator.max_price == 20.0


async def test_pressing_cancel_boost_ends_a_boost(hass, freezer):
    sim = await _sim(hass, freezer)
    sim.setting_edits = True
    await _call(hass, "number", "set_value", f"number.{PREFIX}_boost_duration_hours", value=3.0)
    assert sim.data["state"] == "boosting"
    await _call(hass, "button", "press", f"button.{PREFIX}_boost_cancel")
    assert sim.data["state"] != "boosting"


async def test_services_are_idempotent_pressing_stop_twice_is_fine(hass, freezer):
    sim = await _sim(hass, freezer)
    sim.setting_edits = True
    for _ in range(3):
        await _call(hass, "button", "press", f"button.{PREFIX}_stop")
    assert sim.data["state"] == "idle"


# ---------------------------------------------------------------------------
# Registry metadata
# ---------------------------------------------------------------------------


def _strings():
    return json.loads((COMPONENT_DIR / "strings.json").read_text(encoding="utf-8"))["entity"]


@pytest.mark.parametrize("options,extra", [(None, set()), (OPPORTUNISTIC_OPTIONS, expected_opportunistic_entity_ids())])
async def test_exactly_the_documented_entities_are_registered(hass, freezer, options, extra):
    sim = await _sim(hass, freezer, options=options) if options else await _sim(hass, freezer)
    registry = er.async_get(hass)

    ids = {e.entity_id for e in er.async_entries_for_config_entry(registry, sim.entry.entry_id)}

    assert ids == EXPECTED_ENTITY_IDS | extra


async def test_unique_ids_are_unique_stable_and_scoped_to_the_entry(hass, freezer):
    sim = await _sim(hass, freezer, options=OPPORTUNISTIC_OPTIONS)
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, sim.entry.entry_id)

    unique_ids = [e.unique_id for e in entries]
    assert len(set(unique_ids)) == len(unique_ids)
    assert all(u.startswith(f"{sim.entry.entry_id}_") for u in unique_ids)
    await sim.restart()
    assert {e.unique_id for e in er.async_entries_for_config_entry(registry, sim.entry.entry_id)} == set(unique_ids)


async def test_every_entity_belongs_to_one_device_with_the_instance_name(hass, freezer):
    sim = await _sim(hass, freezer, options=OPPORTUNISTIC_OPTIONS)
    registry = er.async_get(hass)
    devices = dr.async_get(hass)

    device_ids = {e.device_id for e in er.async_entries_for_config_entry(registry, sim.entry.entry_id)}

    assert len(device_ids) == 1
    device = devices.async_get(device_ids.pop())
    assert device.name == "Wholesale EV Schedule" and device.manufacturer == "Wholesale EV Schedule"
    assert (DOMAIN, sim.entry.entry_id) in device.identifiers


async def test_every_translation_key_in_use_has_a_name_in_strings_and_no_name_is_orphaned(hass, freezer):
    sim = await _sim(hass, freezer, options=OPPORTUNISTIC_OPTIONS)
    registry = er.async_get(hass)
    strings = _strings()
    used = {(e.domain, e.translation_key) for e in er.async_entries_for_config_entry(registry, sim.entry.entry_id)}

    assert all(key and key in strings.get(domain, {}) for domain, key in used), sorted(
        pair for pair in used if pair[1] not in strings.get(pair[0], {})
    )
    declared = {(domain, key) for domain, keys in strings.items() for key in keys}
    assert declared == used, f"strings not backed by an entity: {sorted(declared - used)}"


async def test_friendly_names_come_from_the_strings_file(hass, freezer):
    sim = await _sim(hass, freezer)
    strings = _strings()
    registry = er.async_get(hass)

    for entry in er.async_entries_for_config_entry(registry, sim.entry.entry_id):
        expected = strings[entry.domain][entry.translation_key]["name"]
        assert hass.states.get(entry.entity_id).name.endswith(expected), (entry.entity_id, expected)


DIAGNOSTICS = {
    "block_count", "upcoming_block_2_start", "upcoming_block_2_end", "upcoming_block_3_start", "upcoming_block_3_end",
    "candidate_price_points", "cheapest_available_price", "most_expensive_available_price", "average_price_next_24h",
    "average_price_all_data", "price_data_sources", "active_providers",
}  # fmt: skip


async def test_exactly_the_diagnostic_sensors_are_hidden_diagnostics(hass, freezer):
    sim = await _sim(hass, freezer)
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, sim.entry.entry_id)

    def suffix(entry):
        return entry.unique_id.removeprefix(f"{sim.entry.entry_id}_")

    assert {suffix(e) for e in entries if e.entity_category is not None} == DIAGNOSTICS
    assert {suffix(e) for e in entries if e.hidden_by == er.RegistryEntryHider.INTEGRATION} == DIAGNOSTICS
    assert all(e.disabled_by is None for e in entries)  # hidden from dashboards, never switched off


# ---------------------------------------------------------------------------
# Sensor attribute contract, in every state
# ---------------------------------------------------------------------------

SCHEDULE = f"sensor.{PREFIX}_charging_schedule"
STATE = f"sensor.{PREFIX}_charging_state"


async def _into_state(hass, freezer, name):
    if name == "idle":
        return await _sim(hass, freezer, required_hours=0.0)
    if name == "scheduled":
        return await _sim(hass, freezer)
    if name == "charging":
        sim = await _sim(hass, freezer, min_block_hours=3.0)
        await sim.run_to(datetime(2026, 3, 3, 1, 0, tzinfo=UTC))
        return sim
    if name == "complete":
        sim = await _sim(hass, freezer)
        await sim.run_to(datetime(2026, 3, 3, 6, 0, tzinfo=UTC))
        return sim
    if name == "unschedulable":
        return await _sim(hass, freezer, required_hours=24.0)
    if name == "error":
        return await _sim(hass, freezer, feeds=())
    if name == "boosting":
        sim = await _sim(hass, freezer)
        await sim.act(sim.coordinator.async_start_boost(1.0))
        return sim
    raise AssertionError(name)


@pytest.mark.parametrize("name", ["idle", "scheduled", "charging", "complete", "unschedulable", "error", "boosting"])
async def test_the_schedule_and_state_sensors_expose_a_stable_attribute_contract(hass, freezer, name):
    sim = await _into_state(hass, freezer, name)

    assert hass.states.get(STATE).state == name == hass.states.get(SCHEDULE).state
    schedule = hass.states.get(SCHEDULE).attributes
    assert {
        "slots",
        "future_slots",
        "active_slot",
        "next_slot",
        "block_count",
        "total_committed_hours",
        "hours_remaining",
        "calculated_at",
    } <= set(schedule)
    assert isinstance(schedule["slots"], list) and isinstance(schedule["future_slots"], list)
    assert schedule["block_count"] == len(schedule["slots"])
    assert schedule["total_committed_hours"] == pytest.approx(sum(s["duration_hours"] for s in schedule["slots"]))
    assert isinstance(schedule["hours_remaining"], float | int)
    assert parse_dt(schedule["calculated_at"]) == sim.now
    json.dumps(dict(schedule), default=str)  # everything HA stores must be serialisable
    for slot in schedule["slots"]:
        assert {"start", "end", "duration_hours", "avg_price", "confidence", "estimated", "tier"} <= set(slot)
        assert parse_dt(slot["start"]) < parse_dt(slot["end"])
        assert slot["tier"] in ("required", "opportunistic") and isinstance(slot["estimated"], bool)
    state_attrs = hass.states.get(STATE).attributes
    assert set(state_attrs) >= {"error_reason", "boost_ends_at"}
    assert (state_attrs["error_reason"] is not None) == (name in ("error", "unschedulable"))
    assert (state_attrs["boost_ends_at"] is not None) == (name == "boosting")


@pytest.mark.parametrize("name", ["idle", "scheduled", "charging", "complete", "unschedulable", "error", "boosting"])
async def test_the_next_slot_and_remaining_sensors_agree_with_the_schedule(hass, freezer, name):
    sim = await _into_state(hass, freezer, name)
    data = sim.data
    start = hass.states.get(f"sensor.{PREFIX}_next_slot_start")
    end = hass.states.get(f"sensor.{PREFIX}_next_slot_end")
    price = hass.states.get(f"sensor.{PREFIX}_next_slot_average_price")
    cost = hass.states.get(f"sensor.{PREFIX}_next_slot_estimated_cost")
    hours = hass.states.get(f"sensor.{PREFIX}_hours_remaining")
    minutes = hass.states.get(f"sensor.{PREFIX}_time_remaining")
    boost = hass.states.get(f"sensor.{PREFIX}_boost_ends_at")

    if data["next_slot"]:
        assert parse_dt(start.state) == parse_dt(data["next_slot"]["start"])
        assert parse_dt(end.state) == parse_dt(data["next_slot"]["end"])
        assert float(price.state) == pytest.approx(data["next_slot"]["avg_price"])
        assert float(cost.state) == pytest.approx(
            round(data["next_slot"]["avg_price"] * sim.coordinator.assumed_charge_kwh, 2)
        )
    else:
        assert {start.state, end.state, price.state, cost.state} == {"unknown"}
    assert float(hours.state) == pytest.approx(round(data["hours_remaining"], 2))
    assert float(minutes.state) == round(data["hours_remaining"] * 60)
    assert (boost.state != "unknown") == (name == "boosting")


@pytest.mark.parametrize("name", ["scheduled", "error", "idle"])
async def test_the_charging_desired_binary_sensor_matches_the_computed_output(hass, freezer, name):
    sim = await _into_state(hass, freezer, name)
    assert hass.states.get(f"binary_sensor.{PREFIX}_charging_desired").state == ("on" if sim.data["desired"] else "off")
