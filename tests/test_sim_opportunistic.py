"""The opportunistic tier under simulation: required hours stay guaranteed, extra charging only
happens in genuinely cheap slots, the two tiers never collide, and an in-progress opportunistic
block obeys the switch, its target and its deadline."""

from datetime import datetime, timedelta

from custom_components.wholesale_ev_schedule.const import CONF_DEFAULT_OPPORTUNISTIC_OFFSET_DAYS
from custom_components.wholesale_ev_schedule.scheduler import parse_dt

from .factories import OPPORTUNISTIC_OPTIONS
from .sim import UTC, PriceWorld, Sim

START = "2026-03-02 12:00:00+00:00"
# Opportunistic deadline = ready_by + 1 day, so it competes tonight against tomorrow night.
OPTIONS = {**OPPORTUNISTIC_OPTIONS, CONF_DEFAULT_OPPORTUNISTIC_OFFSET_DAYS: 1}


def _at(day, hour, minute=0):
    return datetime(2026, 3, day, hour, minute, tzinfo=UTC)


async def _sim(hass, freezer, *, target=3.0, max_price=14.0, enabled=True, world=None, **kwargs):
    kwargs.setdefault("required_hours", 2.0)
    kwargs.setdefault("min_block_hours", 1.0)
    sim = await Sim.create(
        hass, freezer, start=START, options=kwargs.pop("options", OPTIONS), world=world or PriceWorld(seed=3), **kwargs
    )
    c = sim.coordinator
    await sim.act(c.async_set_opportunistic_max_price(max_price))
    await sim.act(c.async_set_opportunistic_target_hours(target))
    await sim.act(c.async_set_opportunistic_enabled(enabled))
    return sim


def _tier_sessions(sim, tier):
    return [s for s in sim.data["sessions"] if s["tier"] == tier]


async def _run_until_opportunistic_block_is_active(sim, limit_hours=40):
    for _ in range(limit_hours * 12):
        t = await sim.step(5)
        for s in t.sessions:
            if s["tier"] == "opportunistic" and parse_dt(s["start"]) <= t.at < parse_dt(s["end"]):
                return s
    raise AssertionError("no opportunistic block ever became active")


async def test_required_hours_are_always_met_and_extra_charging_stays_within_the_target(hass, freezer):
    sim = await _sim(hass, freezer)
    await sim.run(60 * 24 * 4, step=10)

    cycles = [m / 60 for _, m in sorted(sim.charged_minutes.items())][:-1]
    assert len(cycles) >= 3
    assert all(2.0 <= hours <= 2.0 + 3.0 for hours in cycles), cycles
    assert any(hours > 2.0 for hours in cycles), "opportunistic charging never added anything on cheap nights"


async def test_a_zero_target_means_exactly_the_required_hours(hass, freezer):
    sim = await _sim(hass, freezer, target=0.0)
    await sim.run(60 * 24 * 3, step=10)

    assert {m for _, m in sorted(sim.charged_minutes.items())[:-1]} == {120}


async def test_a_cap_below_every_price_adds_nothing_but_leaves_required_untouched(hass, freezer):
    sim = await _sim(hass, freezer, max_price=1.0)
    await sim.run(60 * 24 * 3, step=10)

    assert {m for _, m in sorted(sim.charged_minutes.items())[:-1]} == {120}
    assert all(s["tier"] == "required" for t in sim.timeline for s in t.sessions)


async def test_the_two_tiers_never_claim_the_same_slot(hass, freezer):
    sim = await _sim(hass, freezer, target=5.0, max_price=30.0)
    await sim.run(60 * 24 * 3, step=15)

    for tick in sim.timeline:
        required = [(parse_dt(s["start"]), parse_dt(s["end"])) for s in tick.sessions if s["tier"] == "required"]
        extra = [(parse_dt(s["start"]), parse_dt(s["end"])) for s in tick.sessions if s["tier"] == "opportunistic"]
        for a_start, a_end in required:
            for b_start, b_end in extra:
                assert a_end <= b_start or b_end <= a_start, (tick.at, required, extra)


async def test_opportunistic_hours_are_a_fresh_allowance_every_day(hass, freezer):
    sim = await _sim(hass, freezer, target=2.0, max_price=14.0)
    await sim.run(60 * 24 * 4, step=10)

    extra_by_cycle = [m / 60 - 2.0 for _, m in sorted(sim.charged_minutes.items())[:-1]]
    assert sum(1 for h in extra_by_cycle if h > 0) >= 2, extra_by_cycle  # not a one-off


async def test_pausing_opportunistic_charging_stops_a_block_that_is_running(hass, freezer):
    sim = await _sim(hass, freezer, target=3.0, max_price=30.0)
    block = await _run_until_opportunistic_block_is_active(sim)
    assert sim.data["desired"] is True

    await sim.act(sim.coordinator.async_set_opportunistic_enabled(False))

    assert sim.data["desired"] is False or sim.data["state"] == "charging"  # required may be running too
    assert all(s["tier"] == "required" for s in sim.data["sessions"])
    await sim.step(5)
    assert not any(s["tier"] == "opportunistic" for s in sim.data["sessions"])
    assert parse_dt(block["end"]) > sim.now  # it really was cut short


async def test_resuming_after_a_pause_plans_opportunistic_charging_again(hass, freezer):
    sim = await _sim(hass, freezer, target=3.0, max_price=30.0)
    await _run_until_opportunistic_block_is_active(sim)
    await sim.act(sim.coordinator.async_set_opportunistic_enabled(False))
    await sim.step(5)

    await sim.act(sim.coordinator.async_set_opportunistic_enabled(True))
    await sim.step(5)

    assert (
        any(s["tier"] == "opportunistic" for s in sim.data["sessions"])
        or sim.coordinator._delivered_opportunistic_hours > 0
    )


async def test_lowering_the_target_below_what_has_run_stops_the_running_opportunistic_block(hass, freezer):
    sim = await _sim(hass, freezer, target=4.0, max_price=30.0)
    block = await _run_until_opportunistic_block_is_active(sim)
    await sim.run(35, step=5)  # let some of it run

    await sim.act(sim.coordinator.async_set_opportunistic_target_hours(0.5))
    await sim.step(5)

    assert not any(
        s["tier"] == "opportunistic" and parse_dt(s["start"]) <= sim.now < parse_dt(s["end"])
        for s in sim.data["sessions"]
    )
    assert parse_dt(block["end"]) > sim.now


async def test_pulling_the_opportunistic_deadline_earlier_cuts_the_running_block(hass, freezer):
    sim = await _sim(hass, freezer, target=4.0, max_price=30.0)
    block = await _run_until_opportunistic_block_is_active(sim)
    cut = sim.now + timedelta(minutes=30)

    await sim.act(sim.coordinator.async_set_opportunistic_ready_by(cut))
    await sim.step(5)

    for s in sim.data["sessions"]:
        if s["tier"] == "opportunistic":
            assert parse_dt(s["end"]) <= cut
    assert parse_dt(block["end"]) > cut


async def test_the_opportunistic_cap_is_independent_of_the_required_cap(hass, freezer):
    """Required may use any price up to its own (generous) cap while opportunistic, with a cap
    below every price, adds nothing."""
    sim = await _sim(hass, freezer, max_price=1.0)
    await sim.act(sim.coordinator.async_set_max_price(100.0))
    await sim.run(60 * 24, step=10)

    assert {s["tier"] for t in sim.timeline for s in t.sessions} == {"required"}


async def test_toggling_the_switch_through_the_entity_works_end_to_end(hass, freezer):
    sim = await _sim(hass, freezer, enabled=False, target=3.0, max_price=30.0)
    assert not any(s["tier"] == "opportunistic" for s in sim.data["sessions"])

    sim.setting_edits = True
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.wholesale_ev_schedule_opportunistic_charging_enabled"}, blocking=True
    )
    await sim.step(5)

    assert sim.coordinator.opportunistic_enabled is True
    assert hass.states.get("switch.wholesale_ev_schedule_opportunistic_charging_enabled").state == "on"
    assert any(s["tier"] == "opportunistic" for s in sim.data["sessions"])


async def test_the_next_slot_sensor_names_the_earliest_session_across_both_tiers(hass, freezer):
    sim = await _sim(hass, freezer, target=3.0, max_price=30.0)
    sim.setting_edits = True

    for _ in range(40):
        await sim.step(30)
        upcoming = [parse_dt(s["start"]) for s in sim.data["sessions"] if parse_dt(s["start"]) > sim.now]
        if upcoming:
            next_slot = sim.data["next_slot"]
            assert parse_dt(next_slot["start"]) == min(upcoming), (sim.now, sim.data["sessions"])
            sensor = hass.states.get("sensor.wholesale_ev_schedule_next_slot_start")
            assert parse_dt(sensor.state) == min(upcoming)
