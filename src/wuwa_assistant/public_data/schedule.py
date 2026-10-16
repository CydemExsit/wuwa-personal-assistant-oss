"""Shared, Qt-free Asia countdown and weekend reminder rules."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .models import OfficialNotice, OfficialSchedule
from .reviewed import reviewed_schedules

ASIA_ZONE = ZoneInfo("Asia/Taipei")


def schedule_items(
    notices: Iterable[OfficialNotice], *, kind: str | None = None
) -> tuple[OfficialSchedule, ...]:
    """Keep the newest article's version of each named period."""
    result: dict[str, OfficialSchedule] = {}
    ordered = sorted(notices, key=lambda item: (item.published_at or "", item.article_id))
    if any(notice.article_id == 5516 for notice in ordered):
        result.update(
            {item.key: item for item in reviewed_schedules() if kind is None or item.kind == kind}
        )
    for notice in ordered:
        for item in notice.schedules:
            if kind is None or item.kind == kind:
                result[item.key] = item
    return tuple(result.values())


def weekly_period(now: datetime) -> tuple[datetime, datetime]:
    """Asia weekly reward period, Monday 04:00 through next Monday 04:00."""
    local = _aware(now).astimezone(ASIA_ZONE)
    start = local.replace(hour=4, minute=0, second=0, microsecond=0) - timedelta(
        days=local.weekday()
    )
    if local < start:
        start -= timedelta(days=7)
    return start, start + timedelta(days=7)


def weekly_key(now: datetime) -> str:
    return "weekly-boss:" + weekly_period(now)[0].date().isoformat()


def is_weekend(now: datetime) -> bool:
    return _aware(now).astimezone(ASIA_ZONE).weekday() in {5, 6}


def weekend_reminders(
    items: Iterable[OfficialSchedule], *, now: datetime, completed_keys: Iterable[str] = ()
) -> tuple[OfficialSchedule, ...]:
    """Unfinished events strictly less than ten days from a confirmed deadline."""
    _aware(now)
    if not is_weekend(now):
        return ()
    completed = frozenset(completed_keys)
    return tuple(
        item
        for item in items
        if item.kind == "event"
        and item.key not in completed
        and item.ends_at is not None
        and timedelta(0) < item.ends_at - now < timedelta(days=10)
        and (item.starts_at is None or item.starts_at <= now)
    )


def countdown(deadline: datetime | None, *, now: datetime) -> str:
    _aware(now)
    if deadline is None:
        return "時間待確認"
    seconds = max(0, int((_aware(deadline) - now).total_seconds()))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{days} 天 {hours:02d}:{minutes:02d}:{seconds:02d}"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return value
