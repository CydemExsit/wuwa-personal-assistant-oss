from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from wuwa_assistant.application.tasks_events import TasksEventsService
from wuwa_assistant.public_data.client import official_article_url
from wuwa_assistant.public_data.models import OfficialNotice, OfficialSchedule
from wuwa_assistant.public_data.schedule import ASIA_ZONE
from wuwa_assistant.public_data.schedule_state import ScheduleCompletionService
from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.storage.orm import DataSetRecord
from wuwa_assistant.storage.settings import SettingsService
from wuwa_assistant.ui.controllers.tasks_events import TasksEventsPresenter
from wuwa_assistant.ui.pages.tasks_events import TasksEventsPage
from wuwa_assistant.ui.widgets.official_feed import BannerCountdownWidget, EventCountdownWidget

NOW = datetime(2026, 10, 10, 10, tzinfo=ASIA_ZONE)


def _notice(*items: OfficialSchedule) -> OfficialNotice:
    return OfficialNotice(
        1,
        "官方公告",
        "gacha",
        official_article_url(1),
        None,
        None,
        None,
        "needs_manual_confirmation",
        items,
    )


def _item(key: str, *, kind: str = "gacha") -> OfficialSchedule:
    return OfficialSchedule(
        key,
        key,
        kind,
        official_article_url(1),
        NOW - timedelta(days=1),
        NOW + timedelta(days=8),
        (key,),
    )


def test_banner_cards_keep_simultaneous_and_next_banners_without_guessing(
    qapp: QApplication,
) -> None:
    del qapp
    widget = BannerCountdownWidget()
    widget.timer.stop()
    one, two = _item("角色甲"), _item("角色乙")
    next_one = replace(
        _item("下一期甲"), starts_at=NOW + timedelta(days=9), ends_at=NOW + timedelta(days=20)
    )
    next_two = replace(next_one, key="next2", five_star_characters=("下一期乙",))
    later = replace(
        next_one,
        key="later",
        five_star_characters=("更晚一期",),
        starts_at=NOW + timedelta(days=10),
    )
    expired = replace(
        one, key="expired", five_star_characters=("舊角色",), ends_at=NOW - timedelta(hours=1)
    )
    partial = replace(one, key="unknown", five_star_characters=("開啟未知",), starts_at=None)
    widget.set_notices((_notice(one, two, next_one, next_two, later, expired, partial),))
    widget.refresh(NOW)
    rendered = widget.browser.toPlainText()
    assert all(name in rendered for name in ("角色甲", "角色乙", "下一期甲", "下一期乙"))
    assert "舊角色" not in rendered and "更晚一期" not in rendered
    assert "8 天 00:00:00" in rendered and "9 天 00:00:00" in rendered
    assert "確切開始時間待確認" in rendered
    assert "四星角色待確認" in rendered
    widget.close()


def test_event_completion_and_weekly_reset_do_not_emit_automatic_mutations(
    qapp: QApplication,
) -> None:
    del qapp
    widget = EventCountdownWidget()
    widget.timer.stop()
    item = _item("activity", kind="event")
    expired = replace(item, key="expired", ends_at=NOW - timedelta(hours=1))
    changes: list[tuple[str, bool]] = []
    widget.completion_changed.connect(lambda key, done: changes.append((key, done)))
    widget.set_notices((_notice(item, expired),))
    widget.set_completed(frozenset({"cycle:tower:2026-09-14"}))
    widget.refresh(NOW)
    assert next(row[1] for row in widget._rows if row[0].key.startswith("cycle:tower:")).isChecked()
    active_row = next(row for row in widget._rows if row[0].key == item.key)
    assert "週末提醒" in active_row[2].text()
    assert all(row[0].key != expired.key for row in widget._rows)
    active_row[1].setChecked(True)
    assert changes == [("activity", True)]
    widget.refresh(NOW)
    assert "週末提醒" not in active_row[2].text()
    widget.refresh(datetime(2026, 10, 12, 4, tzinfo=ASIA_ZONE))
    assert not next(
        row[1] for row in widget._rows if row[0].key.startswith("cycle:tower:")
    ).isChecked()
    assert changes == [("activity", True)]
    widget.close()


def test_page_checkbox_persists_completion_for_background_reader(
    qapp: QApplication, qtbot: QtBot, tmp_path: Path
) -> None:
    del qapp
    database = create_database(AppPaths.from_root(tmp_path))
    page = TasksEventsPage()
    page.countdown_feed.timer.stop()
    with database.session_factory.begin() as session:
        data_set = DataSetRecord(name="主要資料", kind="primary", is_active=True)
        session.add(data_set)
        session.flush()
        data_set_id = data_set.id
    state = ScheduleCompletionService(SettingsService(database.session_factory))
    presenter = TasksEventsPresenter(
        service=TasksEventsService(database.session_factory),
        page=page,
        data_set_id=data_set_id,
        completion_service=state,
        now=lambda: NOW,
    )
    try:
        presenter.initialize()
        qtbot.waitUntil(lambda: not page._loading)
        page.set_official_notices((_notice(_item("activity", kind="event")),))
        page.countdown_feed.refresh(NOW)
        next(row[1] for row in page.countdown_feed._rows if row[0].key == "activity").setChecked(
            True
        )
        qtbot.waitUntil(lambda: "activity" in state.completed_keys(data_set_id=data_set_id))
        qtbot.waitUntil(lambda: not page._loading)
        page.countdown_feed.refresh(NOW)
        next(
            row[1] for row in page.countdown_feed._rows if row[0].key.startswith("cycle:tower:")
        ).setChecked(True)
        qtbot.waitUntil(
            lambda: "cycle:tower:2026-09-14" in state.completed_keys(data_set_id=data_set_id)
        )
        qtbot.waitUntil(lambda: not page._loading)
        assert state.completed_keys(data_set_id="another") == frozenset()
    finally:
        presenter.shutdown()
        page.close()
        database.dispose()
