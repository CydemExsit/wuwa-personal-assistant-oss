from __future__ import annotations

from datetime import UTC, datetime

from wuwa_assistant.reminders import DueBucket, classify_due


def test_taipei_midnight_boundary_uses_local_calendar() -> None:
    now = datetime(2026, 8, 8, 16, 5, tzinfo=UTC)  # 2026-08-09 00:05 Taipei

    assert classify_due(datetime(2026, 8, 8, 15, 59, tzinfo=UTC), now=now) is DueBucket.OVERDUE
    assert classify_due(datetime(2026, 8, 8, 16, 0, tzinfo=UTC), now=now) is DueBucket.TODAY


def test_due_buckets_are_mutually_exclusive() -> None:
    # Monday 2026-08-03 09:00 Taipei.
    now = datetime(2026, 8, 3, 1, tzinfo=UTC)
    cases = {
        DueBucket.OVERDUE: datetime(2026, 8, 2, 15, 59, tzinfo=UTC),
        DueBucket.TODAY: datetime(2026, 8, 3, 14, tzinfo=UTC),
        DueBucket.NEXT_THREE_DAYS: datetime(2026, 8, 5, 1, tzinfo=UTC),
        DueBucket.THIS_WEEK: datetime(2026, 8, 7, 1, tzinfo=UTC),
        DueBucket.LATER: datetime(2026, 8, 10, 1, tzinfo=UTC),
    }

    assert {classify_due(value, now=now) for value in cases.values()} == set(cases)
    assert classify_due(None, now=now) is DueBucket.UNDATED


def test_snooze_moves_an_overdue_task_to_the_snooze_bucket() -> None:
    now = datetime(2026, 8, 3, 1, tzinfo=UTC)
    due = datetime(2026, 8, 1, 1, tzinfo=UTC)
    snoozed = datetime(2026, 8, 4, 1, tzinfo=UTC)

    assert classify_due(due, now=now, snoozed_until=snoozed) is DueBucket.NEXT_THREE_DAYS
