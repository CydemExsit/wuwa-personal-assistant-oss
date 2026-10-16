"""Mutually exclusive local-calendar due buckets."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class DueBucket(StrEnum):
    OVERDUE = "overdue"
    TODAY = "today"
    NEXT_THREE_DAYS = "next3days"
    THIS_WEEK = "thisweek"
    UNDATED = "undated"
    LATER = "later"


def effective_due_at(
    due_at: datetime | None,
    snoozed_until: datetime | None,
) -> datetime | None:
    """Use the later due/snooze instant so snoozed work does not remain overdue."""

    safe_due = _optional_aware(due_at, "due_at")
    safe_snooze = _optional_aware(snoozed_until, "snoozed_until")
    if safe_due is None:
        return safe_snooze
    if safe_snooze is None:
        return safe_due
    return max(safe_due, safe_snooze)


def classify_due(
    due_at: datetime | None,
    *,
    now: datetime,
    timezone_name: str = "Asia/Taipei",
    snoozed_until: datetime | None = None,
) -> DueBucket:
    """Classify one task using non-overlapping Asia/Taipei calendar windows."""

    safe_now = _optional_aware(now, "now")
    if safe_now is None:  # pragma: no cover - now is statically non-null
        raise ValueError("now must be a timezone-aware datetime")
    effective = effective_due_at(due_at, snoozed_until)
    if effective is None:
        return DueBucket.UNDATED
    try:
        zone = ZoneInfo(timezone_name)
    except (TypeError, ZoneInfoNotFoundError) as exc:
        raise ValueError("timezone_name must be an installed IANA timezone") from exc
    local_now = safe_now.astimezone(zone)
    local_due = effective.astimezone(zone)
    start_today = datetime.combine(local_now.date(), time.min, tzinfo=zone)
    start_tomorrow = start_today + timedelta(days=1)
    end_next_three_days = start_today + timedelta(days=4)
    start_next_week = start_today + timedelta(days=7 - start_today.weekday())

    if local_due < start_today:
        return DueBucket.OVERDUE
    if local_due < start_tomorrow:
        return DueBucket.TODAY
    if local_due < end_next_three_days:
        return DueBucket.NEXT_THREE_DAYS
    if local_due < start_next_week:
        return DueBucket.THIS_WEEK
    return DueBucket.LATER


def _optional_aware(value: datetime | None, label: str) -> datetime | None:
    if value is not None and (not isinstance(value, datetime) or value.tzinfo is None):
        raise ValueError(f"{label} must be a timezone-aware datetime or null")
    return value
