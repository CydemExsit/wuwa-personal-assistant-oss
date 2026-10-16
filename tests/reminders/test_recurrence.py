from __future__ import annotations

from datetime import UTC, datetime

import pytest

from wuwa_assistant.reminders import (
    RecurrenceFrequency,
    RecurrenceRule,
    UnknownRecurrenceRule,
    next_occurrence_after,
    parse_recurrence_payload,
    recurrence_payload,
)


def test_recurrence_payload_round_trip_is_versioned() -> None:
    rule = RecurrenceRule.new(RecurrenceFrequency.DAILY, interval=2)

    payload = recurrence_payload(rule)
    parsed = parse_recurrence_payload(payload)

    assert payload["schema_version"] == 1
    assert parsed == rule


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 99, "frequency": "daily", "interval": 1},
        {
            "schema_version": 1,
            "frequency": "monthly",
            "interval": 1,
            "series_id": "series",
            "occurrence_index": 0,
        },
        {
            "schema_version": 1,
            "frequency": "daily",
            "interval": 1,
            "series_id": "series",
            "occurrence_index": 0,
            "future_field": True,
        },
    ],
)
def test_unknown_recurrence_is_preserved_and_never_coerced(
    payload: dict[str, object],
) -> None:
    parsed = parse_recurrence_payload(payload)

    assert isinstance(parsed, UnknownRecurrenceRule)
    assert parsed.to_json_dict() == payload


def test_late_daily_completion_skips_missed_occurrences() -> None:
    scheduled = datetime(2026, 8, 1, 1, tzinfo=UTC)  # 09:00 Taipei
    completed = datetime(2026, 8, 4, 3, tzinfo=UTC)  # 11:00 Taipei
    rule = RecurrenceRule.new("daily", interval=1)

    assert next_occurrence_after(scheduled, completed, rule) == datetime(2026, 8, 5, 1, tzinfo=UTC)


def test_weekly_recurrence_keeps_local_wall_clock() -> None:
    scheduled = datetime(2026, 8, 3, 12, 30, tzinfo=UTC)  # Monday 20:30 Taipei
    completed = datetime(2026, 8, 18, 0, tzinfo=UTC)
    rule = RecurrenceRule.new("weekly", interval=1)

    assert next_occurrence_after(scheduled, completed, rule) == datetime(
        2026, 8, 24, 12, 30, tzinfo=UTC
    )


def test_recurrence_requires_aware_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        next_occurrence_after(
            datetime(2026, 8, 1),
            datetime(2026, 8, 2, tzinfo=UTC),
            RecurrenceRule.new("daily"),
        )
