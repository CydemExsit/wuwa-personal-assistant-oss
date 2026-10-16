from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Lock
from typing import cast

from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from wuwa_assistant.application.tasks_events import (
    EventDefinition,
    TaskDefinition,
    TasksEventsService,
)
from wuwa_assistant.reminders import RecurrenceRule, UnknownRecurrenceRule
from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.storage.orm import DataSetRecord, TaskRecord
from wuwa_assistant.ui.controllers.tasks_events import TasksEventsPresenter
from wuwa_assistant.ui.pages.tasks_events import (
    EventSaveRequest,
    SnoozeTaskRequest,
    TaskSaveRequest,
    TasksEventsPage,
    TasksEventsSnapshot,
)

NOW = datetime(2026, 8, 3, 1, tzinfo=UTC)  # Monday 09:00 in Asia/Taipei.


def _data_set(database) -> str:  # type: ignore[no-untyped-def]
    with database.session_factory.begin() as session:
        record = DataSetRecord(name="主要資料", kind="primary", is_active=True)
        session.add(record)
        session.flush()
        return record.id


def test_presenter_connects_task_event_crud_recurrence_and_dashboard(
    qapp: QApplication,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    del qapp
    database = create_database(AppPaths.from_root(tmp_path / "tasks-presenter"))
    data_set_id = _data_set(database)
    service = TasksEventsService(database.session_factory)
    overdue = service.create_task(
        data_set_id=data_set_id,
        command=TaskDefinition(
            title="逾期事項",
            due_at=NOW - timedelta(days=1),
        ),
    )
    page = TasksEventsPage()
    presenter = TasksEventsPresenter(
        service=service,
        page=page,
        data_set_id=data_set_id,
        now=lambda: NOW,
    )
    dashboard_updates: list[str] = []
    presenter.dashboard_due_changed.connect(dashboard_updates.append)
    try:
        presenter.initialize()
        qtbot.waitUntil(lambda: page.task_model.rowCount() == 1)
        assert dashboard_updates[-1] == "逾期 1 項"

        page.event_save_requested.emit(
            EventSaveRequest(
                None,
                "限時活動",
                "limited",
                NOW,
                NOW + timedelta(days=7),
                3,
                "active",
                False,
                "本機手動資料",
            )
        )
        qtbot.waitUntil(lambda: len(service.list_events(data_set_id=data_set_id)) == 1)
        event = service.list_events(data_set_id=data_set_id)[0]
        qtbot.waitUntil(lambda: page.event_model.rowCount() == 1)

        page.task_save_requested.emit(
            TaskSaveRequest(
                None,
                "每日活動任務",
                "daily",
                NOW + timedelta(hours=3),
                1,
                "daily",
                1,
                event.id,
                False,
                "每日重置",
            )
        )

        def recurring_task_exists() -> bool:
            return any(
                item.title == "每日活動任務" for item in service.list_tasks(data_set_id=data_set_id)
            )

        qtbot.waitUntil(recurring_task_exists)
        recurring = next(
            item
            for item in service.list_tasks(data_set_id=data_set_id)
            if item.title == "每日活動任務"
        )
        assert isinstance(recurring.recurrence, RecurrenceRule)
        assert recurring.recurrence.frequency.value == "daily"
        assert recurring.event_title == "限時活動"

        page.task_save_requested.emit(
            TaskSaveRequest(
                recurring.id,
                "每兩日活動任務",
                "daily",
                recurring.due_at,
                2,
                "daily",
                2,
                event.id,
                False,
                "更新後仍由使用者確認",
            )
        )
        qtbot.waitUntil(
            lambda: (
                service.get_task(recurring.id, data_set_id=data_set_id).title == "每兩日活動任務"
            )
        )
        updated = service.get_task(recurring.id, data_set_id=data_set_id)
        assert isinstance(updated.recurrence, RecurrenceRule)
        assert updated.recurrence.interval == 2

        page.task_snooze_requested.emit(SnoozeTaskRequest(recurring.id, NOW + timedelta(days=1)))
        qtbot.waitUntil(
            lambda: (
                service.get_task(recurring.id, data_set_id=data_set_id).snoozed_until
                == NOW + timedelta(days=1)
            )
        )

        page.task_complete_requested.emit(recurring.id)
        qtbot.waitUntil(
            lambda: service.get_task(recurring.id, data_set_id=data_set_id).completed_at == NOW
        )
        tasks = service.list_tasks(data_set_id=data_set_id)
        next_occurrences = [
            item for item in tasks if item.id != recurring.id and item.title == "每兩日活動任務"
        ]
        assert len(next_occurrences) == 1
        assert next_occurrences[0].completed_at is None

        page.task_delete_requested.emit(overdue.id)
        qtbot.waitUntil(
            lambda: all(
                item.id != overdue.id for item in service.list_tasks(data_set_id=data_set_id)
            )
        )
        page.event_delete_requested.emit(event.id)
        qtbot.waitUntil(lambda: service.list_events(data_set_id=data_set_id) == ())
        qtbot.waitUntil(
            lambda: all(
                item.event_id is None for item in service.list_tasks(data_set_id=data_set_id)
            )
        )
        assert "活動已刪除" in page.status_label.text()
        assert presenter.shutdown()
    finally:
        presenter.shutdown()
        page.close()
        database.dispose()


def test_presenter_exposes_unknown_recurrence_and_refuses_completion(
    qapp: QApplication,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    del qapp
    database = create_database(AppPaths.from_root(tmp_path / "unknown-recurrence"))
    data_set_id = _data_set(database)
    service = TasksEventsService(database.session_factory)
    task = service.create_task(
        data_set_id=data_set_id,
        command=TaskDefinition(title="新版重複規則", due_at=NOW),
    )
    with database.session_factory.begin() as session:
        record = session.get(TaskRecord, task.id)
        assert record is not None
        record.recurrence_rule_json = {
            "schema_version": 2,
            "frequency": "monthly",
            "interval": 1,
        }

    page = TasksEventsPage()
    presenter = TasksEventsPresenter(
        service=service,
        page=page,
        data_set_id=data_set_id,
        now=lambda: NOW,
    )
    try:
        presenter.initialize()
        qtbot.waitUntil(lambda: page.task_model.rowCount() == 1)
        view = page.task_model.row(page.task_model.index(0, 0))
        assert view is not None
        assert view.recurrence == "unknown"
        assert isinstance(
            service.get_task(task.id, data_set_id=data_set_id).recurrence,
            UnknownRecurrenceRule,
        )

        page.task_save_requested.emit(
            TaskSaveRequest(
                task.id,
                "新版重複規則 (保留)",
                "custom",
                NOW,
                100,
                None,
                1,
                None,
                False,
                "只改一般欄位",
                True,
            )
        )
        qtbot.waitUntil(
            lambda: (
                service.get_task(task.id, data_set_id=data_set_id).title == "新版重複規則 (保留)"
            )
        )
        assert isinstance(
            service.get_task(task.id, data_set_id=data_set_id).recurrence,
            UnknownRecurrenceRule,
        )

        page.task_complete_requested.emit(task.id)
        qtbot.waitUntil(lambda: "UnknownRecurrenceRuleError" in page.status_label.text())
        assert service.get_task(task.id, data_set_id=data_set_id).completed_at is None
    finally:
        presenter.shutdown()
        page.close()
        database.dispose()


def test_presenter_counts_event_only_deadlines_on_dashboard(
    qapp: QApplication,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    del qapp
    database = create_database(AppPaths.from_root(tmp_path / "event-dashboard"))
    data_set_id = _data_set(database)
    service = TasksEventsService(database.session_factory)
    service.create_event(
        data_set_id=data_set_id,
        command=EventDefinition(
            title="今日結束活動",
            starts_at=NOW - timedelta(days=1),
            ends_at=NOW + timedelta(hours=2),
            status="active",
        ),
    )
    page = TasksEventsPage()
    presenter = TasksEventsPresenter(
        service=service,
        page=page,
        data_set_id=data_set_id,
        now=lambda: NOW,
    )
    updates: list[str] = []
    presenter.dashboard_due_changed.connect(updates.append)
    try:
        presenter.initialize()
        qtbot.waitUntil(lambda: bool(updates))
        assert updates[-1] == "今日 1 項"
        assert page.today_card.value_label.text() == "1"
    finally:
        presenter.shutdown()
        page.close()
        database.dispose()


class _RecordingPage(TasksEventsPage):
    def __init__(self) -> None:
        super().__init__()
        self.applied_snapshots: list[TasksEventsSnapshot] = []

    def set_snapshot(self, snapshot: object) -> None:
        assert isinstance(snapshot, TasksEventsSnapshot)
        self.applied_snapshots.append(snapshot)
        super().set_snapshot(snapshot)


class _BlockingTasksService:
    """Capture the first task list, then let a newer refresh overtake its result."""

    def __init__(self, inner: TasksEventsService) -> None:
        self._inner = inner
        self.started = Event()
        self.release = Event()
        self._lock = Lock()
        self._first = True

    def list_tasks(self, **kwargs):  # type: ignore[no-untyped-def]
        rows = self._inner.list_tasks(**kwargs)
        with self._lock:
            first = self._first
            self._first = False
        if first:
            self.started.set()
            if not self.release.wait(timeout=5):
                raise TimeoutError("test did not release the first presenter read")
        return rows

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def test_presenter_ignores_stale_background_snapshot(
    qapp: QApplication,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    del qapp
    database = create_database(AppPaths.from_root(tmp_path / "stale-tasks"))
    data_set_id = _data_set(database)
    actual = TasksEventsService(database.session_factory)
    actual.create_task(
        data_set_id=data_set_id,
        command=TaskDefinition(title="舊快照"),
    )
    blocking = _BlockingTasksService(actual)
    page = _RecordingPage()
    presenter = TasksEventsPresenter(
        service=cast(TasksEventsService, blocking),
        page=page,
        data_set_id=data_set_id,
        now=lambda: NOW,
    )
    try:
        presenter.initialize()
        assert blocking.started.wait(timeout=2)
        actual.create_task(
            data_set_id=data_set_id,
            command=TaskDefinition(title="較新快照"),
        )
        presenter.refresh()
        blocking.release.set()

        qtbot.waitUntil(lambda: page.task_model.rowCount() == 2)
        assert len(page.applied_snapshots) == 1
        assert len(page.applied_snapshots[0].tasks) == 2
    finally:
        blocking.release.set()
        presenter.shutdown()
        page.close()
        database.dispose()
