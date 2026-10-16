from __future__ import annotations

from datetime import UTC, datetime, timedelta

from PySide6.QtCore import Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from tests.gui.helpers import MemorySettings
from wuwa_assistant.public_data.models import (
    OfficialNotice,
    OfficialSchedule,
    PublicDataFeed,
    PublicDataSyncResult,
)
from wuwa_assistant.ui.controllers.public_data import (
    OfficialPublicDataPresenter,
    farming_activity_notices,
)
from wuwa_assistant.ui.pages.gacha import GachaPage
from wuwa_assistant.ui.pages.settings import SettingsPage
from wuwa_assistant.ui.pages.tasks_events import TasksEventsPage


def _result(message: str) -> PublicDataSyncResult:
    notices = (
        OfficialNotice(
            10,
            "版本活動公告",
            "event",
            "https://wutheringwaves.kurogames.com/zh-tw/main/news/detail/10",
            None,
            None,
            None,
            "needs_manual_confirmation",
        ),
        OfficialNotice(
            11,
            "角色活動喚取",
            "gacha",
            "https://wutheringwaves.kurogames.com/zh-tw/main/news/detail/11",
            None,
            "2026-08-01",
            "2026-08-20",
            "confirmed_from_article_body",
        ),
    )
    feed = PublicDataFeed(
        1,
        "https://hw-media-cdn-mingchao.kurogame.com/MainMenu.json",
        datetime(2026, 8, 8, tzinfo=UTC).isoformat(),
        "a" * 64,
        None,
        None,
        notices,
    )
    return PublicDataSyncResult(feed, False, False, message)


class _Service:
    def __init__(self) -> None:
        self.cache_calls = 0
        self.sync_calls = 0

    def load_cached(self) -> PublicDataSyncResult:
        self.cache_calls += 1
        return _result("已載入快取。")

    def synchronize(self) -> PublicDataSyncResult:
        self.sync_calls += 1
        return _result("已更新。")


def test_settings_exposes_explicit_update_action(qapp: QApplication) -> None:
    page = SettingsPage(MemorySettings())
    spy = QSignalSpy(page.public_data_update_requested)
    page.show()
    qapp.processEvents()
    QTest.mouseClick(page.public_data_update_button, Qt.MouseButton.LeftButton)
    assert spy.count() == 1
    assert page.public_data_update_button.text() == "更新官方公開資料"
    page.close()


def test_presenter_loads_cache_without_network_and_click_updates_pages(
    qapp: QApplication,
    qtbot: QtBot,
) -> None:
    service = _Service()
    settings = SettingsPage(MemorySettings())
    tasks = TasksEventsPage()
    gacha = GachaPage()
    presenter = OfficialPublicDataPresenter(
        service=service,  # type: ignore[arg-type]
        settings_page=settings,
        tasks_page=tasks,
        gacha_page=gacha,
    )
    presenter.initialize()
    qtbot.waitUntil(lambda: service.cache_calls == 1)
    qtbot.waitUntil(lambda: "已載入快取" in settings.public_data_status_label.text())
    assert service.sync_calls == 0
    assert "版本活動公告" in tasks.official_feed.browser.toPlainText()
    assert "角色活動喚取" in gacha.official_feed.browser.toPlainText()
    assert "日期需人工確認" in tasks.official_feed.browser.toPlainText()

    QTest.mouseClick(settings.public_data_update_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: service.sync_calls == 1)
    qtbot.waitUntil(lambda: "已更新" in settings.public_data_status_label.text())
    assert settings.public_data_update_button.isEnabled()
    presenter.shutdown()
    for widget in (settings, tasks, gacha):
        widget.close()


def test_opted_in_startup_loads_cache_then_updates(qapp: QApplication, qtbot: QtBot) -> None:
    service = _Service()
    settings = SettingsPage(MemorySettings())
    tasks = TasksEventsPage()
    gacha = GachaPage()
    presenter = OfficialPublicDataPresenter(
        service=service,  # type: ignore[arg-type]
        settings_page=settings,
        tasks_page=tasks,
        gacha_page=gacha,
        auto_update=lambda: True,
    )
    spy = QSignalSpy(presenter.feed_changed)
    try:
        presenter.initialize()
        qtbot.waitUntil(lambda: service.sync_calls == 1)
        qtbot.waitUntil(lambda: spy.count() == 2)
        assert service.cache_calls == 1
        assert "公開資料更新" in presenter.activity_notices()[-1]
    finally:
        presenter.shutdown()
        for widget in (settings, tasks, gacha):
            widget.close()


def test_farming_bonus_visible_without_account_gaps_and_expired_not_shown() -> None:
    now = datetime(2026, 10, 9, tzinfo=UTC)
    active = OfficialSchedule(
        "double",
        "聲弦滌盪",
        "event",
        "https://wutheringwaves.kurogames.com/zh-tw/main/news/detail/10",
        starts_at=now - timedelta(days=1),
        ends_at=now + timedelta(days=3),
        bonus_kind="tacet",
        daily_bonus_limit=3,
    )
    notice = OfficialNotice(
        10,
        "版本公告",
        "announcement",
        active.official_url,
        None,
        None,
        None,
        "needs_manual_confirmation",
        (active,),
    )
    feed = PublicDataFeed(1, "official", now.isoformat(), "a" * 64, None, None, (notice,))
    lines = farming_activity_notices(feed, now=now)
    assert "聲弦滌盪" in lines[0] and "每日 3 次" in lines[0]
    assert "即使目前無缺口" in lines[0]
    assert "聲弦滌盪" not in " ".join(farming_activity_notices(feed, now=now + timedelta(days=4)))
