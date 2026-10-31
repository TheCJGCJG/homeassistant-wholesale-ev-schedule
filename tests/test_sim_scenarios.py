"""Day-in-the-life scenarios (see sim.py)."""

from datetime import datetime, timedelta

import pytest

from .sim import PriceWorld, Sim


async def test_a_session_that_ends_exactly_at_ready_by_does_not_use_up_the_next_days_requirement(hass, freezer):
    """The cheapest window runs right up to the 07:00 deadline. At 07:00 ready_by rolls forward and
    resets the delivered-hours counter; the session that has just ended must be credited to the
    cycle it belonged to, not the new one -- otherwise the whole next requirement reads as
    already delivered and the car never charges again."""
    world = PriceWorld(trough=(4.0, 7.0))
    sim = await Sim.create(hass, freezer, world=world, start="2026-03-02 12:00:00+00:00", required_hours=3.0)

    await sim.run(60 * 24 * 3)

    delivered = {key: round(minutes / 60, 2) for key, minutes in sim.charged_minutes.items()}
    assert delivered == {
        "2026-03-03T07:00:00+00:00": 3.0,
        "2026-03-04T07:00:00+00:00": 3.0,
        "2026-03-05T07:00:00+00:00": 3.0,
    }


# ---------------------------------------------------------------------------
# Timezones and clock changes
# ---------------------------------------------------------------------------

# (id, HA timezone, start, slack in hours). Each run spans a clock change where the zone has one.
# `slack` is how much less than the requirement a cycle may deliver: the scheduler works in whole
# half-hour slots on the UTC grid, so where local 07:00 falls mid-slot (UTC+5:45) the first slot of
# each cycle is already 15 minutes underway when the cycle begins.
_ZONES = [
    ("utc", "UTC", "2026-03-02 12:00:00+00:00", 0.0),
    ("london_spring", "Europe/London", "2026-03-27 12:00:00+00:00", 0.0),
    ("london_autumn", "Europe/London", "2026-10-23 12:00:00+00:00", 0.0),
    ("new_york_spring", "America/New_York", "2026-03-06 12:00:00+00:00", 0.0),
    ("new_york_autumn", "America/New_York", "2026-10-30 12:00:00+00:00", 0.0),
    ("sydney_dst_ends", "Australia/Sydney", "2026-04-02 12:00:00+00:00", 0.0),
    ("sydney_dst_starts", "Australia/Sydney", "2026-10-02 12:00:00+00:00", 0.0),
    ("adelaide_half_hour_offset", "Australia/Adelaide", "2026-04-02 12:00:00+00:00", 0.0),
    ("kolkata_half_hour_no_dst", "Asia/Kolkata", "2026-03-02 12:00:00+00:00", 0.0),
    ("kathmandu_45_minute_offset", "Asia/Kathmandu", "2026-03-02 12:00:00+00:00", 0.5),
    ("lord_howe_30_minute_dst", "Australia/Lord_Howe", "2026-04-02 12:00:00+00:00", 0.0),
]


@pytest.mark.parametrize("tz,start,slack", [z[1:] for z in _ZONES], ids=[z[0] for z in _ZONES])
async def test_every_daily_cycle_delivers_the_required_hours_in_any_timezone(hass, freezer, tz, start, slack):
    """4 days of 'charge 3h by 07:00 local' with a deadline that moves in UTC across clock changes."""
    sim = await Sim.create(hass, freezer, start=start, tz=tz, world=PriceWorld(seed=3), required_hours=3.0)

    await sim.run(60 * 24 * 4, step=10)

    ready_bys = sorted(sim.charged_minutes)
    full_cycles = ready_bys[:-1]
    assert len(full_cycles) >= 3
    for key in full_cycles:
        hours = sim.charged_minutes[key] / 60
        assert 3.0 - slack - 1e-9 <= hours <= 3.0 + 1e-9, (key, sim.charged_minutes)
    assert sim.coordinator.ready_by.hour == 7


# ---------------------------------------------------------------------------
# A deadline hour that does not exist, or happens twice, on a clock-change day
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ready_hour", [0, 1, 2, 3, 6, 23])
@pytest.mark.parametrize(
    "tz,start",
    [
        ("Europe/London", "2026-03-27 12:00:00+00:00"),
        ("Europe/London", "2026-10-23 12:00:00+00:00"),
        ("America/New_York", "2026-03-06 12:00:00+00:00"),
    ],
    ids=["london_spring", "london_autumn", "new_york_spring"],
)
async def test_any_deadline_hour_gives_exactly_one_delivered_cycle_per_day_across_a_clock_change(
    hass, freezer, tz, start, ready_hour
):
    sim = await Sim.create(
        hass,
        freezer,
        start=start,
        tz=tz,
        world=PriceWorld(seed=6),
        required_hours=2.0,
        ready_hour=ready_hour,
        step_minutes=10,
    )
    await sim.run(60 * 24 * 5, step=10)

    deadlines = sorted(sim.charged_minutes)
    full = deadlines[:-1]
    assert len(full) >= 4
    for key in full:
        assert sim.charged_minutes[key] == 120, (key, sim.charged_minutes)
    # one deadline per local calendar day, in order, none skipped or repeated
    instants = [datetime.fromisoformat(k) for k in deadlines]
    gaps = [(b - a) for a, b in zip(instants, instants[1:], strict=False)]
    assert all(timedelta(hours=22) <= g <= timedelta(hours=26) for g in gaps), gaps
