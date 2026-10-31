"""Sessions are reported in the order they will happen, whichever tier planned them.

`next_slot` (and the next-slot sensors built on it) is simply "the first upcoming session", so if
the list is grouped by tier rather than by time, a later required block can be reported as the
"next" one ahead of an earlier opportunistic block."""

from datetime import UTC, datetime, timedelta

from custom_components.wholesale_ev_schedule.scheduler import compute_hours_remaining, prune_and_classify

UTC = UTC
NOW = datetime(2026, 3, 3, 0, 0, tzinfo=UTC)


def _session(start_hour, hours=1.0, tier="required"):
    start = NOW + timedelta(hours=start_hour)
    return {
        "start": start.isoformat(),
        "end": (start + timedelta(hours=hours)).isoformat(),
        "duration_hours": hours,
        "tier": tier,
    }


def test_upcoming_sessions_come_back_in_chronological_order_whatever_order_they_were_stored_in():
    stored = [_session(5), _session(2, tier="opportunistic"), _session(9), _session(3.5)]

    active, future = prune_and_classify(stored, NOW)

    assert active is None
    assert [s["start"] for s in future] == sorted(s["start"] for s in stored)


def test_the_next_session_is_the_earliest_not_the_first_listed():
    required_late = _session(3.5, hours=2.0)
    opportunistic_early = _session(2.0, tier="opportunistic")

    _, future = prune_and_classify([required_late, opportunistic_early], NOW)

    assert future[0] is opportunistic_early


def test_an_active_session_is_unaffected_by_ordering():
    running = _session(-0.5, hours=1.0)
    _, future = prune_and_classify([_session(4), running, _session(2)], NOW)
    active, _ = prune_and_classify([_session(4), running, _session(2)], NOW)

    assert active is running
    assert [s["start"] for s in future] == sorted(s["start"] for s in future)


def test_hours_remaining_does_not_depend_on_order():
    sessions = [_session(5), _session(2), _session(3.5, hours=2.0)]
    active, future = prune_and_classify(sessions, NOW)
    reversed_active, reversed_future = prune_and_classify(list(reversed(sessions)), NOW)

    assert compute_hours_remaining(future, active, NOW) == compute_hours_remaining(
        reversed_future, reversed_active, NOW
    )


def test_ordering_is_by_instant_not_by_the_text_of_the_timestamp_across_a_clock_change():
    """01:30 BST (00:30Z) sorts after 01:00 GMT (01:00Z) as plain text but happens before it."""
    before_change = {"start": "2026-10-25T01:30:00+01:00", "end": "2026-10-25T02:00:00+01:00", "duration_hours": 0.5}
    after_change = {"start": "2026-10-25T01:00:00+00:00", "end": "2026-10-25T01:30:00+00:00", "duration_hours": 0.5}
    now = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)

    _, future = prune_and_classify([after_change, before_change], now)

    assert future == [before_change, after_change]
