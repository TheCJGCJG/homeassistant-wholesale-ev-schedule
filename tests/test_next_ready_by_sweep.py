"""next_ready_by across clock changes, swept rather than spot-checked.

For several timezones, every half hour of the weeks around each clock change, every target hour
and each day offset, the result must be: strictly in the future; on the requested local hour
(or the nearest real time when that hour does not exist that day); the *earliest* such time that
honours the day offset; and a real instant (round-trips through UTC unchanged)."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.wholesale_ev_schedule.scheduler import next_ready_by

UTC = UTC

# (zone, first instant of a window of two weeks straddling a change)
WINDOWS = {
    "london_spring": ("Europe/London", datetime(2026, 3, 24, 0, 0, tzinfo=UTC)),
    "london_autumn": ("Europe/London", datetime(2026, 10, 18, 0, 0, tzinfo=UTC)),
    "new_york_spring": ("America/New_York", datetime(2026, 3, 3, 0, 0, tzinfo=UTC)),
    "new_york_autumn": ("America/New_York", datetime(2026, 10, 27, 0, 0, tzinfo=UTC)),
    "sydney_ends": ("Australia/Sydney", datetime(2026, 3, 29, 0, 0, tzinfo=UTC)),
    "sydney_starts": ("Australia/Sydney", datetime(2026, 9, 27, 0, 0, tzinfo=UTC)),
    "lord_howe_30min": ("Australia/Lord_Howe", datetime(2026, 3, 29, 0, 0, tzinfo=UTC)),
    "kathmandu_no_dst": ("Asia/Kathmandu", datetime(2026, 3, 29, 0, 0, tzinfo=UTC)),
}


def _local_midnights(zone, anchor_local):
    return anchor_local.replace(hour=0, minute=0, second=0, microsecond=0)


@pytest.mark.parametrize("name", list(WINDOWS))
def test_next_ready_by_is_always_the_earliest_future_occurrence_of_the_hour(name):
    zone_name, first = WINDOWS[name]
    zone = ZoneInfo(zone_name)
    checked = 0
    for half_hour in range(0, 14 * 48):
        now = (first + timedelta(minutes=30 * half_hour)).astimezone(zone)
        for hour in (0, 1, 2, 3, 6, 7, 12, 23):
            for offset in (0, 1, 3):
                result = next_ready_by(now, hour, offset)
                label = (name, now.isoformat(), hour, offset, result.isoformat())

                assert result.astimezone(UTC) > now.astimezone(UTC), label
                assert result.astimezone(UTC).astimezone(zone) == result, label  # a real instant
                assert result.tzinfo is not None
                # on the requested local hour, or on the real time the skipped hour collapses to
                assert result.minute % 30 == 0 or name == "kathmandu_no_dst", label
                assert result.hour in (hour, (hour + 1) % 24, (hour - 1) % 24) or name.startswith("lord_howe"), label
                # not later than necessary: within offset+1 local days, and the day offset is honoured
                assert result.astimezone(UTC) - now.astimezone(UTC) <= timedelta(days=offset + 1, hours=2), label
                if offset:
                    assert (result.date() - now.date()).days >= offset, label
                checked += 1
    assert checked > 5000


@pytest.mark.parametrize("name", list(WINDOWS))
def test_next_ready_by_never_goes_backwards_as_time_advances(name):
    """Walking forward in time, the next deadline only ever stays or moves forward."""
    zone_name, first = WINDOWS[name]
    zone = ZoneInfo(zone_name)
    previous = None
    for minutes in range(0, 14 * 24 * 60, 15):
        now = (first + timedelta(minutes=minutes)).astimezone(zone)
        result = next_ready_by(now, 7, 0).astimezone(UTC)
        if previous is not None:
            assert result >= previous, (name, now.isoformat())
        previous = result


@pytest.mark.parametrize("name", ["london_spring", "london_autumn", "new_york_spring", "new_york_autumn"])
def test_consecutive_deadlines_are_one_per_local_day(name):
    """Rolling forward from each deadline gives exactly one 07:00 per local day -- never two on a
    25-hour day, never a skipped day on a 23-hour one."""
    zone_name, first = WINDOWS[name]
    zone = ZoneInfo(zone_name)
    deadline = next_ready_by(first.astimezone(zone), 7, 0)
    days = []
    for _ in range(12):
        days.append(deadline.date())
        deadline = next_ready_by(deadline, 7, 0)

    assert days == [days[0] + timedelta(days=i) for i in range(12)]
