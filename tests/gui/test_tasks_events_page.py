from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from wuwa_assistant.ui.pages.tasks_events import (
    DueSummaryView,
    EventSaveRequest,
    EventView,
    SnoozeTaskRequest,
    TaskSaveRequest,
    TasksEventsPage,
    TasksEventsSnapshot,
    TaskView,
)

NOW = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)


def _snapshot() -> TasksEventsSnapshot:
    return TasksEventsSnapshot(
        tasks=(
            TaskView("task-active", "完成每日事項", "daily", NOW, 1, "daily:1"),
            TaskView(
                "task-complete",
                "已完成事項",
                "custom",
                NOW,
                5,
                completed_at=NOW,
            ),
            TaskView("task-hidden", "隱藏事項", "custom", None, 10, hidden=True),
        ),
        events=(
            EventView("event-active", "限時活動", "limited", NOW, NOW, 1, "active"),
            EventView("event-hidden", "隱藏活動", "custom", None, None, 5, "active", True),
            EventView("event-ignored", "忽略活動", "custom", None, NOW, 6, "ignored"),
        ),
        due=DueSummaryView(overdue=1, today=2, next_three_days=3, this_week=4),
        as_of=NOW,
    )


def test_tasks_events_page_renders_exclusive_counts_filters_and_user_intents(
    qapp: QApplication,
) -> None:
    page = TasksEventsPage()
    completes: list[str] = []
    snoozes: list[SnoozeTaskRequest] = []
    page.task_complete_requested.connect(completes.append)
    page.task_snooze_requested.connect(snoozes.append)
    page.set_snapshot(_snapshot())
    page.show()
    page.legacy_button.setChecked(True)
    qapp.processEvents()

    assert page.overdue_card.value_label.text() == "1"
    assert page.today_card.value_label.text() == "2"
    assert page.three_day_card.value_label.text() == "3"
    assert page.week_card.value_label.text() == "4"
    assert page.task_model.rowCount() == 1
    assert page.event_model.rowCount() == 1
    assert page.task_model.data(page.task_model.index(0, 3)) == "每 1 日"

    page.task_table.selectRow(0)
    QTest.mouseClick(page.task_complete_button, Qt.MouseButton.LeftButton)
    QTest.mouseClick(page.task_snooze_button, Qt.MouseButton.LeftButton)
    assert completes == ["task-active"]
    assert snoozes[0].task_id == "task-active"
    assert snoozes[0].until > datetime.now(UTC) + timedelta(hours=23)

    page.show_inactive_check.setChecked(True)
    assert page.task_model.rowCount() == 3
    assert page.event_model.rowCount() == 3
    page.close()


def test_task_and_event_requests_reject_guesses_and_naive_datetimes() -> None:
    request = TaskSaveRequest(
        None,
        "每週重置",
        "weekly",
        NOW,
        1,
        "weekly",
        1,
        None,
        False,
        "",
    )
    assert request.recurrence_frequency == "weekly"
    preserve = TaskSaveRequest(
        "future-rule",
        "保留未知規則",
        "custom",
        NOW,
        1,
        None,
        1,
        None,
        False,
        "",
        True,
    )
    assert preserve.preserve_unknown_recurrence
    EventSaveRequest(None, "活動", "limited", NOW, NOW, 1, "active", False, "")

    with pytest.raises(ValueError, match="時區"):
        TaskSaveRequest(
            None,
            "錯誤",
            "custom",
            datetime(2026, 8, 8, 10),
            1,
            None,
            1,
            None,
            False,
            "",
        )
    with pytest.raises(ValueError, match="recurrence_frequency"):
        TaskSaveRequest(None, "錯誤", "custom", None, 1, "monthly", 1, None, False, "")
