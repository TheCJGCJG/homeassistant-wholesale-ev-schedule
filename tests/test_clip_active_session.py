"""clip_active_session: an in-progress block must not outlive what the user still wants.

A session is planned once and then carried forward untouched while it runs, so without clipping,
lowering the requirement, pausing a tier, or pulling the deadline earlier changes nothing until
the block happens to end -- the charger just keeps running."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.wholesale_ev_schedule.scheduler import clip_active_session

UTC = UTC
START = datetime(2026, 3, 3, 0, 0, tzinfo=UTC)


def _session(hours=3.0, start=START, **extra):
    end = start + timedelta(hours=hours)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "duration_hours": hours,
        "avg_price": 9.0,
        "confidence": 100.0,
        "estimated": False,
        "tier": "required",
        **extra,
    }


def _at(minutes):
    return START + timedelta(minutes=minutes)


DEADLINE = START + timedelta(hours=12)


def test_a_session_within_its_allowance_and_deadline_is_untouched():
    session = _session(3.0)

    clipped, credit = clip_active_session(session, _at(40), allowed_slots=6, deadline_dt=DEADLINE)

    assert clipped is session and credit == 0.0


def test_a_larger_allowance_than_the_block_is_untouched():
    session = _session(2.0)
    assert clip_active_session(session, _at(40), allowed_slots=20, deadline_dt=DEADLINE) == (session, 0.0)


def test_lowering_the_allowance_below_the_block_shortens_it_on_the_slot_grid():
    clipped, credit = clip_active_session(_session(3.0), _at(40), allowed_slots=4, deadline_dt=DEADLINE)

    assert credit == 0.0
    assert clipped["start"] == START.isoformat()
    assert datetime.fromisoformat(clipped["end"]) == START + timedelta(hours=2)
    assert clipped["duration_hours"] == 2.0
    assert {k: v for k, v in clipped.items() if k not in ("end", "duration_hours")} == {
        k: v for k, v in _session(3.0).items() if k not in ("end", "duration_hours")
    }


def test_an_allowance_already_used_up_stops_the_session_now_and_credits_the_whole_slots_that_ran():
    clipped, credit = clip_active_session(_session(3.0), _at(70), allowed_slots=2, deadline_dt=DEADLINE)

    assert clipped is None
    assert credit == 1.0  # two whole slots ran; the 10 minutes into the third do not count


@pytest.mark.parametrize("allowed_slots", [0, -3])
def test_no_allowance_at_all_stops_it_immediately(allowed_slots):
    clipped, credit = clip_active_session(_session(3.0), _at(40), allowed_slots=allowed_slots, deadline_dt=DEADLINE)

    assert clipped is None and credit == 0.5


def test_allowance_exactly_equal_to_elapsed_time_stops_now():
    clipped, credit = clip_active_session(_session(3.0), _at(60), allowed_slots=2, deadline_dt=DEADLINE)
    assert clipped is None and credit == 1.0


def test_an_earlier_deadline_clips_the_block_to_it():
    deadline = START + timedelta(hours=2)

    clipped, credit = clip_active_session(_session(3.0), _at(40), allowed_slots=6, deadline_dt=deadline)

    assert datetime.fromisoformat(clipped["end"]) == deadline and credit == 0.0
    assert clipped["duration_hours"] == 2.0


def test_a_deadline_that_has_already_passed_stops_it_now():
    clipped, credit = clip_active_session(_session(3.0), _at(100), allowed_slots=6, deadline_dt=_at(90))
    assert clipped is None and credit == 1.5


def test_the_tighter_of_allowance_and_deadline_wins():
    deadline = START + timedelta(hours=2, minutes=30)
    clipped, _ = clip_active_session(_session(3.0), _at(40), allowed_slots=3, deadline_dt=deadline)
    assert datetime.fromisoformat(clipped["end"]) == START + timedelta(hours=1, minutes=30)


def test_no_deadline_means_only_the_allowance_applies():
    clipped, _ = clip_active_session(_session(3.0), _at(40), allowed_slots=2, deadline_dt=None)
    assert datetime.fromisoformat(clipped["end"]) == START + timedelta(hours=1)


def test_clipping_never_mutates_the_stored_session():
    session = _session(3.0)
    snapshot = dict(session)
    clip_active_session(session, _at(40), allowed_slots=2, deadline_dt=DEADLINE)
    assert session == snapshot


def test_the_clipped_end_keeps_the_sessions_own_utc_offset():
    local = ZoneInfo("Europe/London")
    start = datetime(2026, 7, 1, 1, 0, tzinfo=local)
    session = _session(3.0, start=start)

    clipped, _ = clip_active_session(
        session, start + timedelta(minutes=40), allowed_slots=2, deadline_dt=DEADLINE + timedelta(days=300)
    )

    assert clipped["end"].endswith("+01:00")


def test_clipping_across_the_autumn_clock_change_uses_real_elapsed_time():
    local = ZoneInfo("Europe/London")
    start_utc = datetime(2026, 10, 25, 0, 0, tzinfo=UTC)  # 01:00 BST, an hour before the change
    start = start_utc.astimezone(local)
    session = {**_session(3.0, start=start), "end": (start_utc + timedelta(hours=3)).isoformat()}
    now = (start_utc + timedelta(minutes=90)).astimezone(local)  # real elapsed time, not wall clock

    clipped, credit = clip_active_session(session, now, allowed_slots=2, deadline_dt=DEADLINE + timedelta(days=300))

    assert clipped is None and credit == pytest.approx(1.5)


def test_works_with_the_naive_datetimes_the_older_scheduler_tests_use():
    start = datetime(2024, 1, 15, 10, 0)
    session = {
        "start": start.isoformat(),
        "end": (start + timedelta(hours=3)).isoformat(),
        "duration_hours": 3.0,
    }

    clipped, credit = clip_active_session(session, start + timedelta(minutes=20), allowed_slots=2, deadline_dt=None)

    assert credit == 0.0 and datetime.fromisoformat(clipped["end"]) == start + timedelta(hours=1)


@pytest.mark.parametrize(
    "elapsed_minutes,credited_hours",
    [(1, 0.0), (29, 0.0), (30, 0.5), (31, 0.5), (59, 0.5), (60, 1.0), (89, 1.0), (90, 1.5), (179, 2.5)],
)
def test_only_whole_slots_that_actually_ran_are_credited(elapsed_minutes, credited_hours):
    """Crediting a part-slot would be rounded up to a whole slot by the delivered-hours
    accounting, quietly counting time that never ran and leaving the car short."""
    clipped, credit = clip_active_session(_session(3.0), _at(elapsed_minutes), allowed_slots=0, deadline_dt=None)

    assert clipped is None and credit == credited_hours


# ---------------------------------------------------------------------------
# fit_future_sessions: the same idea for sessions that have not started yet
# ---------------------------------------------------------------------------

from custom_components.wholesale_ev_schedule.scheduler import fit_future_sessions  # noqa: E402


def _block(start_hour, hours=1.0, **extra):
    return _session(hours, start=START + timedelta(hours=start_hour), **extra)


def _spans(sessions):
    return [(datetime.fromisoformat(s["start"]), datetime.fromisoformat(s["end"])) for s in sessions]


def test_sessions_within_the_allowance_and_deadline_are_returned_unchanged():
    sessions = [_block(2), _block(4, 2.0)]

    kept = fit_future_sessions(sessions, allowed_slots=6, deadline_dt=DEADLINE)

    assert kept == sessions and all(a is b for a, b in zip(kept, sessions, strict=True))


def test_the_allowance_is_spent_in_chronological_order_and_the_overflow_is_dropped():
    sessions = [_block(2), _block(4), _block(6)]

    kept = fit_future_sessions(sessions, allowed_slots=4, deadline_dt=DEADLINE)

    assert [s["start"] for s in kept] == [sessions[0]["start"], sessions[1]["start"]]


def test_the_session_that_straddles_the_allowance_is_shortened_not_dropped():
    sessions = [_block(2), _block(4, 2.0)]  # 2 + 4 slots

    kept = fit_future_sessions(sessions, allowed_slots=3, deadline_dt=DEADLINE)

    assert _spans(kept)[1][1] - _spans(kept)[1][0] == timedelta(hours=0.5)
    assert kept[1]["duration_hours"] == 0.5


def test_input_order_does_not_matter():
    sessions = [_block(6), _block(2), _block(4)]
    kept = fit_future_sessions(sessions, allowed_slots=2, deadline_dt=DEADLINE)
    assert [s["start"] for s in kept] == [_block(2)["start"]]


def test_nothing_is_kept_with_no_allowance():
    assert fit_future_sessions([_block(2), _block(4)], allowed_slots=0, deadline_dt=DEADLINE) == []
    assert fit_future_sessions([_block(2)], allowed_slots=-4, deadline_dt=DEADLINE) == []


def test_sessions_after_the_deadline_are_dropped_and_one_crossing_it_ends_on_a_whole_slot():
    deadline = START + timedelta(hours=4, minutes=40)
    sessions = [_block(2), _block(4), _block(6)]

    kept = fit_future_sessions(sessions, allowed_slots=20, deadline_dt=deadline)

    assert len(kept) == 2
    assert _spans(kept)[1][1] == START + timedelta(hours=4.5)


def test_a_session_crossing_the_deadline_keeps_only_the_whole_slots_that_finish_in_time():
    deadline = START + timedelta(hours=4, minutes=40)

    (kept,) = fit_future_sessions([_block(4, 2.0)], allowed_slots=20, deadline_dt=deadline)

    assert datetime.fromisoformat(kept["end"]) == START + timedelta(hours=4.5)


def test_fitting_with_no_deadline_means_only_the_allowance_applies():
    kept = fit_future_sessions([_block(2), _block(4)], allowed_slots=3, deadline_dt=None)
    assert sum(s["duration_hours"] for s in kept) == 1.5


def test_fitting_never_mutates_its_input():
    sessions = [_block(2), _block(4, 2.0)]
    snapshot = [dict(s) for s in sessions]
    fit_future_sessions(sessions, allowed_slots=2, deadline_dt=START + timedelta(hours=5))
    assert sessions == snapshot


def test_an_empty_list_is_fine():
    assert fit_future_sessions([], allowed_slots=5, deadline_dt=DEADLINE) == []
