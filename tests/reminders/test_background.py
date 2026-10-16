from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from wuwa_assistant.application.echo_history import EchoHistoryService
from wuwa_assistant.public_data.models import OfficialNotice, OfficialSchedule, PublicDataFeed
from wuwa_assistant.public_data.schedule import ASIA_ZONE
from wuwa_assistant.public_data.schedule_state import ScheduleCompletionService
from wuwa_assistant.reminders.background import (
    AUTO_UPDATE_KEY,
    ENABLED_KEY,
    HOUR_KEY,
    MINUTE_KEY,
    pending_reminders,
    run_background_reminders,
    unsent_reminders,
)
from wuwa_assistant.reminders.windows_task import (
    TASK_NAMESPACE,
    ReminderCommand,
    eligible_weekend,
    task_name,
    task_xml,
)
from wuwa_assistant.storage import AppPaths, create_database

SATURDAY = datetime(2026, 10, 10, 10, tzinfo=ASIA_ZONE)


def _feed(now: datetime) -> PublicDataFeed:
    schedules = tuple(
        OfficialSchedule(
            key=key,
            title=key,
            kind="event",
            official_url="https://wutheringwaves.kurogames.com/zh-tw/main/news/detail/1",
            starts_at=now - timedelta(days=1),
            ends_at=now + timedelta(days=days),
        )
        for key, days in (("expired", -0.5), ("soon", 9.99), ("exact10", 10), ("far", 11))
    )
    notice = OfficialNotice(
        1,
        "活動",
        "event",
        schedules[0].official_url,
        None,
        None,
        None,
        "needs_manual_confirmation",
        schedules,
    )
    return PublicDataFeed(
        1, "https://example.invalid", now.isoformat(), "a" * 64, None, None, (notice,)
    )


def test_weekend_deadlines_and_completion_are_exact() -> None:
    feed = _feed(SATURDAY)
    pending = pending_reminders(feed, now=SATURDAY)
    assert [item.key for item in pending] == ["soon", "cycle:tower:2026-09-14"]
    assert (
        pending_reminders(feed, now=SATURDAY, completed_keys=["soon", "cycle:tower:2026-09-14"])
        == ()
    )
    assert pending_reminders(feed, now=SATURDAY - timedelta(days=1)) == ()


def test_daily_suppression_does_not_hide_sunday_followup_or_new_items() -> None:
    pending = pending_reminders(_feed(SATURDAY), now=SATURDAY)
    ledger = {"date": "2026-10-10", "keys": ["soon"]}
    assert [x.key for x in unsent_reminders(pending, ledger, now=SATURDAY)] == [
        "cycle:tower:2026-09-14"
    ]
    assert unsent_reminders(pending, ledger, now=SATURDAY + timedelta(days=1)) == pending


def test_weekend_uses_asia_time_and_rejects_naive_times() -> None:
    assert eligible_weekend(SATURDAY, 10)
    assert not eligible_weekend(SATURDAY - timedelta(seconds=1), 10)
    assert not eligible_weekend(SATURDAY + timedelta(days=2), 10)
    with pytest.raises(ValueError):
        eligible_weekend(datetime(2026, 10, 10), 10)


def test_task_is_per_user_hidden_zoned_and_quotes_arguments(tmp_path: Path) -> None:
    command = ReminderCommand(
        tmp_path / "a & b.exe",
        ("--data-root", str(tmp_path / "data & docs"), "--check-reminders"),
        tmp_path,
    )
    xml = task_xml(command, hour=10, user="MACHINE\\user")
    root = ET.fromstring(xml)
    ns = {"t": TASK_NAMESPACE}
    assert root.findtext(".//t:StartBoundary", namespaces=ns).endswith("10:00:00+08:00")
    assert root.findtext(".//t:LogonType", namespaces=ns) == "InteractiveToken"
    assert root.findtext(".//t:RunLevel", namespaces=ns) == "LeastPrivilege"
    assert root.findtext(".//t:Hidden", namespaces=ns) == "true"
    assert root.findtext(".//t:Exec/t:Command", namespaces=ns) == str(command.executable)
    assert '"' in root.findtext(".//t:Arguments", namespaces=ns)
    assert task_name(tmp_path) != task_name(tmp_path / "another")


def test_selected_minutes_reach_scheduler_and_runner(tmp_path: Path) -> None:
    command = ReminderCommand(tmp_path / "app.exe", ("--check-reminders",), tmp_path)
    root = ET.fromstring(task_xml(command, hour=10, minute=45, user="MACHINE\\user"))
    assert root.findtext(".//t:StartBoundary", namespaces={"t": TASK_NAMESPACE}).endswith(
        "10:45:00+08:00"
    )
    paths, _ = _ready_database(tmp_path)
    with create_database(paths) as database:
        database.settings.set(MINUTE_KEY, 45)
    calls: list[str] = []

    def notify(title: str, _body: str) -> bool:
        calls.append(title)
        return True

    assert run_background_reminders(paths, now=SATURDAY + timedelta(minutes=44), notify=notify) == 0
    assert calls == []
    assert run_background_reminders(paths, now=SATURDAY + timedelta(minutes=45), notify=notify) == 0
    assert len(calls) == 1


def test_endgame_deadlines_and_separate_cycles() -> None:
    from wuwa_assistant.public_data.recurring import challenge_periods

    tower, wastes = challenge_periods(SATURDAY)
    assert tower.title == "逆境深塔 · 第 40 期"
    assert tower.starts_at == datetime(2026, 9, 14, 4, tzinfo=ASIA_ZONE)
    assert tower.ends_at == datetime(2026, 10, 12, 3, 59, tzinfo=ASIA_ZONE)
    assert wastes.title == "冥歌海墟 · 第 21 期"
    assert wastes.starts_at == datetime(2026, 9, 28, 4, tzinfo=ASIA_ZONE)
    assert wastes.ends_at == datetime(2026, 10, 26, 3, 59, tzinfo=ASIA_ZONE)
    after = challenge_periods(datetime(2026, 10, 12, 4, tzinfo=ASIA_ZONE))
    assert after[0] == wastes
    assert after[1].title == "逆境深塔 · 第 41 期"
    assert "推算" in after[1].start_label
    assert [
        item.key
        for item in pending_reminders(
            None, now=SATURDAY + timedelta(days=7), completed_keys=[tower.key]
        )
    ] == [wastes.key]


def _ready_database(tmp_path: Path) -> tuple[AppPaths, str]:
    paths = AppPaths.from_root(tmp_path)
    with create_database(paths) as database:
        dataset = EchoHistoryService(database.session_factory).ensure_active_primary_dataset()
        database.settings.set(ENABLED_KEY, True)
        database.settings.set(HOUR_KEY, 10)
    return paths, dataset


def test_runner_persists_suppression_reopens_and_respects_completion(tmp_path: Path) -> None:
    paths, dataset = _ready_database(tmp_path)
    calls: list[str] = []

    def notify(title: str, _body: str) -> bool:
        calls.append(title)
        return True

    assert run_background_reminders(paths, now=SATURDAY, notify=notify) == 0
    assert run_background_reminders(paths, now=SATURDAY, notify=notify) == 0
    assert len(calls) == 1
    with create_database(paths) as database:
        ScheduleCompletionService(database.settings).set_completed(
            data_set_id=dataset, key="cycle:tower:2026-09-14", completed=True
        )
    assert run_background_reminders(paths, now=SATURDAY + timedelta(days=1), notify=notify) == 0
    assert len(calls) == 1
    assert run_background_reminders(paths, now=SATURDAY + timedelta(days=7), notify=notify) == 0
    assert len(calls) == 2


def test_dry_run_unavailable_notifications_and_weekdays_do_not_mark_sent(tmp_path: Path) -> None:
    paths, _dataset = _ready_database(tmp_path)
    assert (
        run_background_reminders(
            paths,
            now=SATURDAY,
            dry_run=True,
            notify=lambda *_: pytest.fail("dry run sent notification"),
        )
        == 0
    )
    assert run_background_reminders(paths, now=SATURDAY, notify=lambda *_: False) == 3
    report = json.loads((paths.logs_dir / "reminder-last-run.json").read_text(encoding="utf-8"))
    assert report["status"] == "notification_unavailable"
    assert (
        run_background_reminders(
            paths,
            now=SATURDAY - timedelta(days=1),
            notify=lambda *_: pytest.fail("weekday sent notification"),
        )
        == 0
    )
    with create_database(paths) as database:
        assert not any(
            item.key.startswith("reminders/submitted/") for item in database.settings.list_all()
        )


def test_first_offline_sync_still_reminds_near_expiring_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wuwa_assistant.public_data.client import PublicDataError
    from wuwa_assistant.public_data.service import OfficialPublicDataService

    paths, _dataset = _ready_database(tmp_path)
    with create_database(paths) as database:
        database.settings.set(AUTO_UPDATE_KEY, True)

    def fail(_self: object) -> None:
        raise PublicDataError("offline")

    monkeypatch.setattr(OfficialPublicDataService, "synchronize", fail)
    calls: list[str] = []
    assert (
        run_background_reminders(
            paths, now=SATURDAY, notify=lambda title, _: calls.append(title) is None
        )
        == 0
    )
    assert len(calls) == 1
