"""Wall-clock recurrence calculation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import RecurrenceFrequency, RecurrenceRule


def next_occurrence_after(
    scheduled_at: datetime,
    completed_at: datetime,
    rule: RecurrenceRule,
    *,
    timezone_name: str = "Asia/Taipei",
) -> datetime:
    """Return the first scheduled occurrence strictly after completion.

    Missed occurrences are skipped rather than backfilled.  Arithmetic happens
    in the task's local zone and the result is returned in UTC for persistence.
    """

    safe_scheduled = _aware(scheduled_at, "scheduled_at")
    safe_completed = _aware(completed_at, "completed_at")
    if not isinstance(rule, RecurrenceRule):
        raise ValueError("rule must be RecurrenceRule")
    try:
        zone = ZoneInfo(timezone_name)
    except (TypeError, ZoneInfoNotFoundError) as exc:
        raise ValueError("timezone_name must be an installed IANA timezone") from exc
    candidate = safe_scheduled.astimezone(zone)
    completed_local = safe_completed.astimezone(zone)
    days = rule.interval if rule.frequency is RecurrenceFrequency.DAILY else rule.interval * 7
    step = timedelta(days=days)
    candidate += step
    while candidate <= completed_local:
        candidate += step
    return candidate.astimezone(UTC)


def _aware(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{label} must be a timezone-aware datetime")
    return value
