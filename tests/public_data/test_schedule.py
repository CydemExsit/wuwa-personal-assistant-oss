from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from wuwa_assistant.public_data.cache import PublicDataCache
from wuwa_assistant.public_data.client import MAIN_MENU_URL, official_article_url
from wuwa_assistant.public_data.models import OfficialNotice, OfficialSchedule, PublicDataFeed
from wuwa_assistant.public_data.parser import MenuArticle, parse_article_notice
from wuwa_assistant.public_data.schedule import (
    ASIA_ZONE,
    countdown,
    schedule_items,
    weekend_reminders,
    weekly_key,
    weekly_period,
)
from wuwa_assistant.public_data.schedule_state import ScheduleCompletionService
from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.storage.settings import SettingsService


def _notice(body: str, title: str = "角色活動喚取") -> OfficialNotice:
    return parse_article_notice(
        MenuArticle(5528, title, 90, None),
        {
            "articleId": 5528,
            "articleTitle": title,
            "articleContent": body,
            "startTime": "2026-09-29 11:15:00",
            "endTime": "2099-01-01 00:00:00",
        },
    )


def _banner(title: str, character: str) -> str:
    return (
        f"<h6>[{title}]角色活動喚取</h6>"
        f"<p>活動期間, 5星角色「{character}」, 4星角色「卜靈」、「桃祈」、「釉瑚」"
        "喚取機率限時提升!</p>"
        "<p>3.7版本更新後 ~ 2026年10月22日09:59 (伺服器時間)</p>"
    )


def test_multi_banner_parsing_keeps_all_characters_and_partial_start() -> None:
    notice = _notice(
        "<p>請前往遊戲內查看。"
        + _banner("但願長圓如此夜", "心")
        + "<h6>「玉闕玄華」武器活動喚取</h6><p>5星武器「玉闕玄華」</p>"
        + "<p>3.7版本更新後 ~ 2026年10月22日09:59 (伺服器時間)</p>"
        + _banner("曙暮一線之間", "千咲")
        + _banner("漫於盈缺時軸", "尤諾")
    )
    assert [item.five_star_characters for item in notice.schedules] == [
        ("心",),
        ("千咲",),
        ("尤諾",),
    ]
    for item in notice.schedules:
        assert item.four_star_characters == ("卜靈", "桃祈", "釉瑚")
        assert item.starts_at is None
        assert item.ends_at == datetime(2026, 10, 22, 9, 59, tzinfo=ASIA_ZONE)
        assert item.version == "3.7"


def test_heading_after_inline_official_sentence_is_not_lost() -> None:
    notice = _notice(
        "<p>請以遊戲內內容為準。[但願長圓如此夜]角色活動喚取</p>"
        "<p>5星角色「心」, 4星角色「卜靈」、「桃祈」、「釉瑚」喚取機率限時提升!</p>"
        "<p>3.7版本更新後 ~ 2026年10月22日09:59 (伺服器時間)</p>"
    )
    assert notice.schedules[0].title == "但願長圓如此夜"
    assert notice.schedules[0].five_star_characters == ("心",)


def test_reviewed_image_schedule_fills_next_banner_and_both_bonus_scopes() -> None:
    notice = replace(_notice(""), article_id=5516, schedules=())
    items = schedule_items((notice,))
    banners = [item for item in items if item.kind == "gacha"]
    assert {item.five_star_characters[0] for item in banners} == {"鎖暝", "洛瑟菈", "琳奈"}
    assert all(item.starts_at == datetime(2026, 10, 22, 10, tzinfo=ASIA_ZONE) for item in banners)
    bonuses = [item for item in items if item.bonus_kind]
    assert {scope for item in bonuses for scope in item.bonus_scopes} == {
        "tacet",
        "forgery",
        "simulation",
    }
    assert all(item.daily_bonus_limit is None for item in bonuses)
    corrected = replace(banners[0], five_star_characters=("文字公告優先",))
    newer = replace(notice, article_id=9999, published_at="2026-10-10", schedules=(corrected,))
    assert (
        next(item for item in schedule_items((notice, newer)) if item.key == corrected.key)
        == corrected
    )


def test_version_article_extracts_each_event_with_its_own_range() -> None:
    notice = _notice(
        "<p>維護: 2026年9月30日04:00 ~ 2026年9月30日11:00 (UTC+8)</p>"
        "<h3>[天工尋物]限時戰鬥活動</h3>"
        "<p>2026年10月8日04:00 ~ 2026年10月26日03:59 (伺服器時間)</p>"
        "<h3>[無音消除]休閒活動</h3>"
        "<p>2026年10月22日10:00 ~ 2026年11月9日03:59 (伺服器時間)</p>",
        "3.7版本內容說明",
    )
    assert [item.title for item in notice.schedules] == ["天工尋物", "無音消除"]
    assert notice.schedules[1].starts_at == datetime(2026, 10, 22, 10, tzinfo=ASIA_ZONE)
    assert notice.starts_on is None  # Multiple article periods remain ambiguous at article level.


@pytest.mark.parametrize(
    "period",
    [
        "2026年10月1日 ~ 2026年10月22日 (伺服器時間)",
        "2026年10月1日10:00 ~ 2026年10月22日09:59",
        "2026年10月1日10:00 ~ 2099年1月1日09:59 (伺服器時間)",
    ],
)
def test_missing_precision_or_sentinel_never_becomes_exact_deadline(period: str) -> None:
    notice = _notice(f"<p>{period}</p>")
    assert not notice.schedules or notice.schedules[0].ends_at is None


def test_ambiguous_section_does_not_guess_which_range_is_the_banner() -> None:
    notice = _notice(
        "<p>2026年10月1日10:00 ~ 2026年10月22日09:59 (UTC+8)</p>"
        "<p>2026年10月2日10:00 ~ 2026年10月23日09:59 (UTC+8)</p>"
    )
    assert notice.schedules == ()


def test_explicit_timezone_and_unknown_roles_are_preserved() -> None:
    item = _notice("<p>2026年10月1日10:00 ~ 2026年10月22日09:59 (UTC-5)</p>").schedules[0]
    assert item.starts_at == datetime(2026, 10, 1, 15, tzinfo=UTC)
    assert item.five_star_characters == ()


def test_bonus_metadata_requires_a_single_named_activity_and_explicit_bonus() -> None:
    item = _notice(
        "<p>無音區獎勵雙倍, 每日可獲得3次雙倍獎勵。</p>"
        "<p>2026年10月1日04:00 ~ 2026年10月8日03:59 (伺服器時間)</p>",
        "雙倍活動",
    ).schedules[0]
    assert item.bonus_kind == "tacet"
    assert item.daily_bonus_limit == 3


def test_official_tacet_challenge_name_and_weapon_only_notice() -> None:
    period = "2026年11月4日04:00 ~ 2026年11月11日03:59 (伺服器時間)"
    item = _notice(f"<p>完成無音清剿挑戰獲得雙倍獎勵。</p><p>{period}</p>", "聲弦滌蕩活動")
    assert item.schedules[0].bonus_kind == "tacet"
    assert not _notice(f"<p>{period}</p>", "「存幀」武器活動喚取").schedules


def test_mixed_double_rewards_preserve_both_scopes_without_guessing_daily_count() -> None:
    item = _notice(
        "<p>模擬領域與凝素領域獎勵雙倍。</p>"
        "<p>2026年10月1日04:00 ~ 2026年10月8日03:59 (伺服器時間)</p>",
        "材料活動",
    ).schedules[0]
    assert item.bonus_kind == "mixed"
    assert set(item.bonus_scopes) == {"simulation", "forgery"}
    assert item.daily_bonus_limit is None


def test_weekly_period_changes_only_at_asia_monday_four() -> None:
    before = datetime(2026, 10, 12, 3, 59, tzinfo=ASIA_ZONE)
    after = before + timedelta(minutes=1)
    assert weekly_key(before) == "weekly-boss:2026-10-05"
    assert weekly_key(after) == "weekly-boss:2026-10-12"
    assert weekly_period(before)[1] == after
    assert weekly_key(after.astimezone(UTC)) == weekly_key(after)


def test_weekend_reminders_boundaries_completion_and_future_start() -> None:
    now = datetime(2026, 10, 10, 10, tzinfo=ASIA_ZONE)
    item = OfficialSchedule(
        "event", "活動", "event", official_article_url(1), ends_at=now + timedelta(days=9)
    )
    assert weekend_reminders((item,), now=now) == (item,)
    assert not weekend_reminders((item,), now=now, completed_keys={"event"})
    assert not weekend_reminders((replace(item, ends_at=now + timedelta(days=10)),), now=now)
    assert not weekend_reminders((replace(item, ends_at=now),), now=now)
    assert not weekend_reminders((replace(item, starts_at=now + timedelta(days=1)),), now=now)
    assert not weekend_reminders((item,), now=now + timedelta(days=2))
    assert not weekend_reminders((replace(item, ends_at=None),), now=now)
    assert countdown(now + timedelta(seconds=90061), now=now) == "1 天 01:01:01"


def test_newest_duplicate_schedule_wins_and_cache_roundtrips(tmp_path: Path) -> None:
    notice = _notice(_banner("卡池", "心"))
    updated = replace(
        notice,
        article_id=6000,
        schedules=(
            replace(
                notice.schedules[0],
                official_url=official_article_url(6000),
                five_star_characters=("更正角色",),
            ),
        ),
    )
    assert schedule_items((updated, notice))[0].five_star_characters == ("更正角色",)
    feed = PublicDataFeed(
        1, MAIN_MENU_URL, "2026-10-08T00:00:00+00:00", "a" * 64, None, None, (notice,), 1
    )
    cache = PublicDataCache(tmp_path)
    cache.save(feed)
    assert cache.load() == feed


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("official_url", "https://example.com/untrusted"),
        ("ends_at", "2026-10-22T09:59:00"),
        ("bonus_kind", "arbitrary"),
        ("bonus_scopes", ["unconfirmed"]),
        ("daily_bonus_limit", True),
    ],
)
def test_tampered_schedule_cache_fails_closed(tmp_path: Path, field: str, value: object) -> None:
    notice = _notice(_banner("卡池", "心"))
    feed = PublicDataFeed(
        1, MAIN_MENU_URL, "2026-10-08T00:00:00+00:00", "a" * 64, None, None, (notice,), 1
    )
    cache = PublicDataCache(tmp_path)
    cache.save(feed)
    payload = json.loads(cache.path.read_text(encoding="utf-8"))
    payload["notices"][0]["schedules"][0][field] = value
    cache.path.write_text(json.dumps(payload), encoding="utf-8")
    assert cache.load() is None


def test_completion_is_dataset_scoped_and_persists_across_database_reopen(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path)
    database = create_database(paths)
    state = ScheduleCompletionService(SettingsService(database.session_factory))
    state.set_completed(data_set_id="primary", key="event", completed=True)
    state.set_completed(data_set_id="primary", key="weekly-boss:2026-10-05", completed=True)
    assert state.completed_keys(data_set_id="other") == frozenset()
    database.dispose()
    reopened = create_database(paths)
    try:
        state = ScheduleCompletionService(SettingsService(reopened.session_factory))
        assert state.completed_keys(data_set_id="primary") == {"event", "weekly-boss:2026-10-05"}
        state.set_completed(data_set_id="primary", key="event", completed=False)
        assert state.completed_keys(data_set_id="primary") == {"weekly-boss:2026-10-05"}
    finally:
        reopened.dispose()
