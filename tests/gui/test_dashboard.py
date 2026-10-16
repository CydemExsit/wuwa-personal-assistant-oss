from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from wuwa_assistant.ui.navigation import PageId
from wuwa_assistant.ui.pages.dashboard import DashboardPage, DashboardSnapshot


def test_empty_dashboard_has_one_primary_next_step_that_navigates_to_echo(
    qapp: QApplication,
) -> None:
    page = DashboardPage()
    destinations: list[PageId] = []
    page.navigate_requested.connect(destinations.append)
    page.show()
    qapp.processEvents()
    try:
        assert page.quick_start_card.isVisible()
        assert page.metrics_widget.isHidden()
        assert page.regular_actions.isHidden()
        assert page.quick_start_button.property("role") == "primary"
        assert "開始聲骸辨識" in page.quick_start_button.text()

        QTest.mouseClick(page.quick_start_button, Qt.MouseButton.LeftButton)
        assert destinations == [PageId.ECHO_ENHANCEMENT]
    finally:
        page.close()


def test_dashboard_reveals_metrics_after_echo_data_exists(qapp: QApplication) -> None:
    page = DashboardPage(
        DashboardSnapshot(
            character_progress="1 名角色",
            qualified_echoes="1 件聲骸",
            has_character_data=True,
            has_echo_data=True,
        )
    )
    page.show()
    qapp.processEvents()
    try:
        assert page.quick_start_card.isHidden()
        assert page.metrics_widget.isVisible()
        assert page.regular_actions.isVisible()
    finally:
        page.close()


def test_independent_dashboard_updates_do_not_overwrite_other_sections(
    qapp: QApplication,
) -> None:
    del qapp
    page = DashboardPage(
        DashboardSnapshot(
            character_progress="舊角色進度",
            qualified_echoes="舊聲骸件數",
            today_priority="刷取角色突破素材",
            due_tasks="兩項即將到期",
        )
    )
    try:
        page.set_character_snapshot(
            DashboardSnapshot(
                character_progress="3 名常用角色, 平均 72%",
                qualified_echoes="11 件聲骸已及格",
            )
        )

        assert page.findChild(QLabel, "DashboardCharacterProgress").text() == (
            "3 名常用角色, 平均 72%"
        )
        assert page.findChild(QLabel, "DashboardQualifiedEchoes").text() == ("11 件聲骸已及格")
        assert page.findChild(QLabel, "DashboardTodayPriority").text() == ("刷取角色突破素材")
        assert page.findChild(QLabel, "DashboardDueTasks").text() == "兩項即將到期"

        page.set_today_priority("今天先刷技能素材 3 次")
        page.set_due_tasks("本週無到期事項")
        assert page.findChild(QLabel, "DashboardTodayPriority").text() == ("今天先刷技能素材 3 次")
        assert page.findChild(QLabel, "DashboardDueTasks").text() == "本週無到期事項"
    finally:
        page.close()
