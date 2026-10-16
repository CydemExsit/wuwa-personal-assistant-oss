"""Non-blocking presenter for manually managed tasks and limited events."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from threading import RLock
from typing import cast

from PySide6.QtCore import QObject, Qt, Signal, Slot

from wuwa_assistant.application.tasks_events import (
    DueBucketSnapshot,
    EventDefinition,
    EventSnapshot,
    TaskDefinition,
    TasksEventsService,
    TaskSnapshot,
)
from wuwa_assistant.public_data.schedule_state import ScheduleCompletionService
from wuwa_assistant.reminders import (
    DueBucket,
    RecurrenceRule,
    UnknownRecurrenceRule,
    classify_due,
)
from wuwa_assistant.ui.controllers.background_executor import BackgroundExecutor
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


class _Operation(StrEnum):
    LOAD = "load"
    TASK_SAVE = "task_save"
    TASK_COMPLETE = "task_complete"
    TASK_SNOOZE = "task_snooze"
    TASK_DELETE = "task_delete"
    EVENT_SAVE = "event_save"
    EVENT_DELETE = "event_delete"
    SCHEDULE_COMPLETE = "schedule_complete"


@dataclass(frozen=True, slots=True)
class _Bundle:
    snapshot: TasksEventsSnapshot
    dashboard_due: str
    completed_keys: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class _JobResult:
    operation: _Operation
    request_id: int
    bundle: _Bundle | None = None
    error: str | None = None


class TasksEventsPresenter(QObject):
    """Coordinate task persistence off the UI thread and publish frozen views."""

    dashboard_due_changed = Signal(object)
    _job_completed = Signal(object)

    def __init__(
        self,
        *,
        service: TasksEventsService,
        page: TasksEventsPage,
        data_set_id: str,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        completion_service: ScheduleCompletionService | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._page = page
        self._data_set_id = data_set_id
        self._now = now
        self._completion_service = completion_service
        self._completion_keys: frozenset[str] = frozenset()
        self._executor = BackgroundExecutor(max_workers=1, thread_name_prefix="wuwa-tasks-db")
        self._lock = RLock()
        self._futures: set[Future[object]] = set()
        self._latest_request_id = 0
        self._closing = False

        self._job_completed.connect(self._handle_result, Qt.ConnectionType.QueuedConnection)
        page.refresh_requested.connect(self.refresh)
        page.task_save_requested.connect(self.save_task)
        page.task_complete_requested.connect(self.complete_task)
        page.task_snooze_requested.connect(self.snooze_task)
        page.task_delete_requested.connect(self.delete_task)
        page.event_save_requested.connect(self.save_event)
        page.event_delete_requested.connect(self.delete_event)
        page.schedule_completion_requested.connect(self.set_schedule_completed)

    @Slot(str, bool)
    def set_schedule_completed(self, key: str, completed: bool) -> None:
        if self._closing:
            return
        if self._completion_service is None:
            self._page.set_schedule_completed(self._completion_keys)
            self._page.show_error("完成狀態儲存服務尚未初始化。")
            return
        self._page.set_loading(True, "正在儲存完成狀態…")

        def work() -> _Bundle:
            assert self._completion_service is not None
            self._completion_service.set_completed(
                data_set_id=self._data_set_id, key=key, completed=completed
            )
            return self._load_bundle()

        self._submit(_Operation.SCHEDULE_COMPLETE, work)

    def initialize(self) -> None:
        if self._closing:
            return
        self._page.set_loading(True, "正在載入本機待辦與活動…")
        self._submit(_Operation.LOAD, self._load_bundle)

    @Slot()
    def refresh(self) -> None:
        if self._closing:
            return
        self._page.set_loading(True, "正在重新整理本機待辦與活動…")
        self._submit(_Operation.LOAD, self._load_bundle)

    @Slot(object)
    def save_task(self, payload: object) -> None:
        if self._closing:
            return
        if not isinstance(payload, TaskSaveRequest):
            self._page.show_error("待辦編輯資料格式不正確。")
            return
        self._page.set_loading(True, "正在儲存待辦…")

        def work() -> _Bundle:
            command = _task_definition(payload)
            if payload.identifier is None:
                self._service.create_task(data_set_id=self._data_set_id, command=command)
            else:
                self._service.update_task(
                    payload.identifier,
                    data_set_id=self._data_set_id,
                    command=command,
                )
            return self._load_bundle()

        self._submit(_Operation.TASK_SAVE, work)

    @Slot(str)
    def complete_task(self, task_id: str) -> None:
        if self._closing or not task_id.strip():
            return
        self._page.set_loading(True, "正在完成待辦並安排下一次重複事項…")

        def work() -> _Bundle:
            self._service.complete_task(
                task_id,
                data_set_id=self._data_set_id,
                completed_at=_aware_now(self._now()),
            )
            return self._load_bundle()

        self._submit(_Operation.TASK_COMPLETE, work)

    @Slot(object)
    def snooze_task(self, payload: object) -> None:
        if self._closing:
            return
        if not isinstance(payload, SnoozeTaskRequest):
            self._page.show_error("延後待辦資料格式不正確。")
            return
        self._page.set_loading(True, "正在延後待辦…")

        def work() -> _Bundle:
            self._service.snooze_task(
                payload.task_id,
                payload.until,
                data_set_id=self._data_set_id,
                now=_aware_now(self._now()),
            )
            return self._load_bundle()

        self._submit(_Operation.TASK_SNOOZE, work)

    @Slot(str)
    def delete_task(self, task_id: str) -> None:
        if self._closing or not task_id.strip():
            return
        self._page.set_loading(True, "正在刪除待辦…")

        def work() -> _Bundle:
            self._service.delete_task(task_id, data_set_id=self._data_set_id)
            return self._load_bundle()

        self._submit(_Operation.TASK_DELETE, work)

    @Slot(object)
    def save_event(self, payload: object) -> None:
        if self._closing:
            return
        if not isinstance(payload, EventSaveRequest):
            self._page.show_error("活動編輯資料格式不正確。")
            return
        self._page.set_loading(True, "正在儲存活動…")

        def work() -> _Bundle:
            command = _event_definition(payload)
            if payload.identifier is None:
                self._service.create_event(data_set_id=self._data_set_id, command=command)
            else:
                self._service.update_event(
                    payload.identifier,
                    data_set_id=self._data_set_id,
                    command=command,
                )
            return self._load_bundle()

        self._submit(_Operation.EVENT_SAVE, work)

    @Slot(str)
    def delete_event(self, event_id: str) -> None:
        if self._closing or not event_id.strip():
            return
        self._page.set_loading(True, "正在刪除活動; 關聯待辦會保留…")

        def work() -> _Bundle:
            self._service.delete_event(event_id, data_set_id=self._data_set_id)
            return self._load_bundle()

        self._submit(_Operation.EVENT_DELETE, work)

    def prepare_shutdown(self) -> None:
        """Quiesce UI callbacks and cancel queued reads without waiting."""

        self._closing = True
        self._executor.prepare_shutdown()

    def shutdown(self) -> bool:
        self.prepare_shutdown()
        return self.finish_shutdown()

    def finish_shutdown(self) -> bool:
        self._executor.shutdown()
        with self._lock:
            self._futures.clear()
        return True

    def _load_bundle(self) -> _Bundle:
        now = _aware_now(self._now())
        tasks = self._service.list_tasks(
            data_set_id=self._data_set_id,
            include_completed=True,
            include_hidden=True,
        )
        events = self._service.list_events(
            data_set_id=self._data_set_id,
            include_hidden=True,
        )
        due = self._service.due_buckets(data_set_id=self._data_set_id, now=now)
        due_view = _combined_due_view(due, events, now=now)
        return _Bundle(
            TasksEventsSnapshot(
                tasks=tuple(_task_view(item) for item in tasks),
                events=tuple(_event_view(item) for item in events),
                due=due_view,
                as_of=now,
            ),
            _dashboard_due_text(due_view),
            self._completion_service.completed_keys(data_set_id=self._data_set_id)
            if self._completion_service
            else frozenset(),
        )

    def _submit(self, operation: _Operation, work: Callable[[], _Bundle]) -> None:
        if self._closing:
            return
        with self._lock:
            self._latest_request_id += 1
            request_id = self._latest_request_id

        def run() -> _JobResult:
            try:
                return _JobResult(operation, request_id, bundle=work())
            except Exception as exc:
                return _JobResult(
                    operation,
                    request_id,
                    error=f"{type(exc).__name__}: {exc}",
                )

        future = cast(
            Future[object], self._executor.submit(run, read_only=operation is _Operation.LOAD)
        )
        with self._lock:
            self._futures.add(future)

        def completed(done: Future[object]) -> None:
            with self._lock:
                self._futures.discard(done)
            if not self._closing:
                self._job_completed.emit(done.result())

        future.add_done_callback(completed)

    @Slot(object)
    def _handle_result(self, payload: object) -> None:
        if self._closing or not isinstance(payload, _JobResult):
            return
        with self._lock:
            if payload.request_id != self._latest_request_id:
                return
        if payload.error is not None or payload.bundle is None:
            self._page.set_schedule_completed(self._completion_keys)
            self._page.show_error(payload.error or "待辦與活動結果不完整")
            return
        self._page.set_snapshot(payload.bundle.snapshot)
        self._page.set_schedule_completed(payload.bundle.completed_keys)
        self._completion_keys = payload.bundle.completed_keys
        message = {
            _Operation.TASK_SAVE: "待辦已儲存。",
            _Operation.TASK_COMPLETE: "待辦已完成; 如有重複規則, 下一次事項已建立。",
            _Operation.TASK_SNOOZE: "待辦已延後。",
            _Operation.TASK_DELETE: "待辦已刪除。",
            _Operation.EVENT_SAVE: "活動已儲存。",
            _Operation.EVENT_DELETE: "活動已刪除, 原有關聯待辦仍保留。",
            _Operation.SCHEDULE_COMPLETE: "完成狀態已儲存; 未完成項目會在週末提醒。",
        }.get(payload.operation)
        if message is not None:
            self._page.show_status(message)
        self.dashboard_due_changed.emit(payload.bundle.dashboard_due)


def _task_definition(payload: TaskSaveRequest) -> TaskDefinition:
    recurrence = (
        None
        if payload.recurrence_frequency is None
        else RecurrenceRule.new(payload.recurrence_frequency, payload.recurrence_interval)
    )
    return TaskDefinition(
        title=payload.title,
        task_type=payload.task_type,
        due_at=payload.due_at,
        priority=payload.priority,
        recurrence=recurrence,
        event_id=payload.event_id,
        timezone_name="Asia/Taipei",
        is_hidden=payload.hidden,
        notes=payload.notes or None,
        preserve_unknown_recurrence=payload.preserve_unknown_recurrence,
    )


def _event_definition(payload: EventSaveRequest) -> EventDefinition:
    return EventDefinition(
        title=payload.title,
        event_type=payload.event_type,
        starts_at=payload.starts_at,
        ends_at=payload.ends_at,
        priority=payload.priority,
        status=payload.status,
        is_hidden=payload.hidden,
        notes=payload.notes or None,
    )


def _task_view(snapshot: TaskSnapshot) -> TaskView:
    recurrence: str | None
    if isinstance(snapshot.recurrence, RecurrenceRule):
        recurrence = f"{snapshot.recurrence.frequency.value}:{snapshot.recurrence.interval}"
    elif isinstance(snapshot.recurrence, UnknownRecurrenceRule):
        recurrence = "unknown"
    else:
        recurrence = None
    return TaskView(
        identifier=snapshot.id,
        title=snapshot.title,
        task_type=snapshot.task_type,
        due_at=snapshot.due_at,
        priority=snapshot.priority,
        recurrence=recurrence,
        event_id=snapshot.event_id,
        event_title=snapshot.event_title,
        completed_at=snapshot.completed_at,
        snoozed_until=snapshot.snoozed_until,
        hidden=snapshot.is_hidden,
        notes=snapshot.notes or "",
    )


def _event_view(snapshot: EventSnapshot) -> EventView:
    return EventView(
        identifier=snapshot.id,
        title=snapshot.title,
        event_type=snapshot.event_type,
        starts_at=snapshot.starts_at,
        ends_at=snapshot.ends_at,
        priority=snapshot.priority,
        status=snapshot.status,
        hidden=snapshot.is_hidden,
        notes=snapshot.notes or "",
    )


def _aware_now(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    return value


def _dashboard_due_text(value: DueSummaryView) -> str:
    parts: list[str] = []
    for count, label in (
        (value.overdue, "逾期"),
        (value.today, "今日"),
        (value.next_three_days, "三日內"),
        (value.this_week, "本週"),
    ):
        if count:
            parts.append(f"{label} {count} 項")
    return " · ".join(parts) if parts else "目前無到期項目"


def _combined_due_view(
    task_due: DueBucketSnapshot,
    events: tuple[EventSnapshot, ...],
    *,
    now: datetime,
) -> DueSummaryView:
    counts = {
        DueBucket.OVERDUE: len(task_due.overdue),
        DueBucket.TODAY: len(task_due.today),
        DueBucket.NEXT_THREE_DAYS: len(task_due.next_three_days),
        DueBucket.THIS_WEEK: len(task_due.this_week),
    }
    inactive = {"completed", "ignored", "cancelled", "ended"}
    for event in events:
        if event.is_hidden or event.status.casefold() in inactive or event.ends_at is None:
            continue
        bucket = classify_due(event.ends_at, now=now, timezone_name="Asia/Taipei")
        if bucket in counts:
            counts[bucket] += 1
    return DueSummaryView(
        overdue=counts[DueBucket.OVERDUE],
        today=counts[DueBucket.TODAY],
        next_three_days=counts[DueBucket.NEXT_THREE_DAYS],
        this_week=counts[DueBucket.THIS_WEEK],
    )


__all__ = ["TasksEventsPresenter"]
