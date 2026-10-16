"""Independent endgame cycles, anchored to the user's confirmed Asia game screen."""

from __future__ import annotations

from datetime import datetime, timedelta

from .models import OfficialSchedule
from .schedule import ASIA_ZONE

CHALLENGES = (
    ("tower", "逆境深塔", 40, datetime(2026, 9, 14, 4, tzinfo=ASIA_ZONE)),
    ("wastes", "冥歌海墟", 21, datetime(2026, 9, 28, 4, tzinfo=ASIA_ZONE)),
)


def challenge_periods(now: datetime) -> tuple[OfficialSchedule, ...]:
    if now.tzinfo is None:
        raise ValueError("aware datetime required")
    periods = []
    for key, title, number, anchor in CHALLENGES:
        offset = (now - anchor) // timedelta(days=28)
        start = anchor + offset * timedelta(days=28)
        end = start + timedelta(days=28) - timedelta(minutes=1)
        periods.append(
            OfficialSchedule(
                key=f"cycle:{key}:{start.date().isoformat()}",
                title=f"{title} · 第 {number + offset} 期",
                kind="event",
                official_url="https://wutheringwaves.kurogames.com/zh-tw/main/news",
                starts_at=start,
                ends_at=end,
                start_label="遊戲內確認" if offset == 0 else "依已確認 28 天週期推算",
                end_label=end.isoformat(),
            )
        )
    return tuple(sorted(periods, key=lambda item: item.ends_at or now))
