from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from wuwa_assistant.application.tasks_events import (
    EventDefinition,
    TaskDefinition,
    TasksEventsService,
)
from wuwa_assistant.reminders import (
    RecurrenceRule,
    UnknownRecurrenceRule,
    UnknownRecurrenceRuleError,
)
from wuwa_assistant.storage.engine import create_session_factory, create_sqlite_engine
from wuwa_assistant.storage.orm import Base, DataSetRecord, EventRecord, TaskRecord


@pytest.fixture
def task_service(tmp_path):  # type: ignore[no-untyped-def]
    engine = create_sqlite_engine(tmp_path / "tasks.db")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        primary = DataSetRecord(name="主要資料", kind="primary", is_active=True)
        demo = DataSetRecord(name="示範資料", kind="demo", is_active=False)
        session.add_all((primary, demo))
        session.flush()
        ids = (primary.id, demo.id)
    try:
        yield TasksEventsService(factory), factory, ids
    finally:
        engine.dispose()


def test_dataset_scoped_crud_and_utc_persistence(task_service) -> None:  # type: ignore[no-untyped-def]
    service, factory, (primary_id, demo_id) = task_service
    event = service.create_event(
        data_set_id=primary_id,
        command=EventDefinition(
            title="限時活動",
            event_type="limited",
            starts_at=datetime(2026, 8, 9, 8, tzinfo=UTC),
            ends_at=datetime(2026, 8, 12, 16, tzinfo=UTC),
        ),
    )
    task = service.create_task(
        data_set_id=primary_id,
        command=TaskDefinition(
            title="完成活動任務",
            task_type="event",
            due_at=datetime(2026, 8, 10, 0, tzinfo=UTC),
            event_id=event.id,
            notes="手動建立",
        ),
    )

    assert task.event_title == "限時活動"
    assert task.due_at == datetime(2026, 8, 10, 0, tzinfo=UTC)
    assert service.list_tasks(data_set_id=demo_id) == ()
    with pytest.raises(ValueError, match="does not belong"):
        service.get_task(task.id, data_set_id=demo_id)
    with factory() as session:
        stored = session.get(TaskRecord, task.id)
        assert stored is not None
        assert stored.due_at == datetime(2026, 8, 10, 0)

    updated = service.update_task(
        task.id,
        data_set_id=primary_id,
        command=TaskDefinition(
            title="完成所有活動任務",
            task_type="event",
            due_at=datetime(2026, 8, 11, 0, tzinfo=UTC),
            event_id=event.id,
            priority=3,
            is_hidden=True,
        ),
    )
    assert updated.title == "完成所有活動任務"
    assert updated.priority == 3
    assert updated.is_hidden
    assert service.set_task_hidden(task.id, False, data_set_id=primary_id).is_hidden is False
    assert service.delete_task(task.id, data_set_id=primary_id)
    assert service.list_tasks(data_set_id=primary_id) == ()


def test_snapshots_are_frozen_and_json_exports_are_detached(task_service) -> None:  # type: ignore[no-untyped-def]
    service, _, (data_set_id, _) = task_service
    task = service.create_task(
        data_set_id=data_set_id,
        command=TaskDefinition(title="每日事項"),
    )

    with pytest.raises(FrozenInstanceError):
        task.title = "mutated"  # type: ignore[misc]
    exported = task.to_json_dict()
    exported["title"] = "mutated"
    assert service.get_task(task.id, data_set_id=data_set_id).title == "每日事項"


def test_recurring_completion_is_atomic_late_safe_and_idempotent(task_service) -> None:  # type: ignore[no-untyped-def]
    service, factory, (data_set_id, _) = task_service
    task = service.create_task(
        data_set_id=data_set_id,
        command=TaskDefinition(
            title="每日重置",
            task_type="daily",
            due_at=datetime(2026, 8, 1, 21, tzinfo=UTC),
            recurrence=RecurrenceRule.new("daily"),
        ),
    )
    completed_at = datetime(2026, 8, 4, 22, tzinfo=UTC)

    first = service.complete_task(task.id, data_set_id=data_set_id, completed_at=completed_at)
    second = service.complete_task(task.id, data_set_id=data_set_id, completed_at=completed_at)

    assert first.completed_task.completed_at == completed_at
    assert first.next_occurrence is not None
    assert first.next_occurrence.due_at == datetime(2026, 8, 5, 21, tzinfo=UTC)
    assert first.next_occurrence.recurrence is not None
    assert second.was_already_completed
    assert second.next_occurrence is None
    with factory() as session:
        assert session.scalar(select(func.count(TaskRecord.id))) == 2


def test_unknown_rule_is_visible_but_completion_refuses_to_guess(task_service) -> None:  # type: ignore[no-untyped-def]
    service, factory, (data_set_id, _) = task_service
    task = service.create_task(
        data_set_id=data_set_id,
        command=TaskDefinition(title="未來規則", due_at=datetime(2026, 8, 9, tzinfo=UTC)),
    )
    with factory.begin() as session:
        record = session.get(TaskRecord, task.id)
        assert record is not None
        record.recurrence_rule_json = {
            "schema_version": 2,
            "frequency": "monthly",
            "interval": 1,
        }

    snapshot = service.get_task(task.id, data_set_id=data_set_id)
    assert isinstance(snapshot.recurrence, UnknownRecurrenceRule)
    assert snapshot.requires_manual_recurrence_confirmation
    updated = service.update_task(
        task.id,
        data_set_id=data_set_id,
        command=TaskDefinition(
            title="未來規則 (只改標題)",
            due_at=snapshot.due_at,
            preserve_unknown_recurrence=True,
        ),
    )
    assert isinstance(updated.recurrence, UnknownRecurrenceRule)
    with pytest.raises(UnknownRecurrenceRuleError, match="not understood"):
        service.complete_task(task.id, data_set_id=data_set_id)
    assert service.get_task(task.id, data_set_id=data_set_id).completed_at is None


def test_snooze_and_due_buckets_exclude_completed_and_hidden(task_service) -> None:  # type: ignore[no-untyped-def]
    service, _, (data_set_id, _) = task_service
    now = datetime(2026, 8, 3, 1, tzinfo=UTC)  # Monday 09:00 Taipei
    overdue = service.create_task(
        data_set_id=data_set_id,
        command=TaskDefinition(title="逾期", due_at=datetime(2026, 8, 1, tzinfo=UTC)),
    )
    service.create_task(
        data_set_id=data_set_id,
        command=TaskDefinition(title="今日", due_at=datetime(2026, 8, 3, 4, tzinfo=UTC)),
    )
    service.create_task(
        data_set_id=data_set_id,
        command=TaskDefinition(title="三日", due_at=datetime(2026, 8, 5, 1, tzinfo=UTC)),
    )
    service.create_task(
        data_set_id=data_set_id,
        command=TaskDefinition(title="本週", due_at=datetime(2026, 8, 7, 1, tzinfo=UTC)),
    )
    service.create_task(
        data_set_id=data_set_id,
        command=TaskDefinition(title="未定日期"),
    )
    hidden = service.create_task(
        data_set_id=data_set_id,
        command=TaskDefinition(title="隱藏逾期", due_at=datetime(2026, 8, 1, tzinfo=UTC)),
    )
    completed = service.create_task(
        data_set_id=data_set_id,
        command=TaskDefinition(title="完成逾期", due_at=datetime(2026, 8, 1, tzinfo=UTC)),
    )
    service.set_task_hidden(hidden.id, True, data_set_id=data_set_id)
    service.complete_task(completed.id, data_set_id=data_set_id, completed_at=now)
    snoozed = service.snooze_task(
        overdue.id,
        datetime(2026, 8, 4, 1, tzinfo=UTC),
        data_set_id=data_set_id,
        now=now,
    )

    buckets = service.due_buckets(data_set_id=data_set_id, now=now)

    assert snoozed.effective_due_at == datetime(2026, 8, 4, 1, tzinfo=UTC)
    assert buckets.counts == {
        "overdue": 0,
        "today": 1,
        "next3days": 2,
        "thisweek": 1,
        "undated": 1,
        "later": 0,
    }


def test_deleting_event_keeps_linked_tasks_and_nulls_relation(task_service) -> None:  # type: ignore[no-untyped-def]
    service, factory, (data_set_id, _) = task_service
    event = service.create_event(
        data_set_id=data_set_id,
        command=EventDefinition(title="即將結束", ends_at=datetime(2026, 8, 9, tzinfo=UTC)),
    )
    task = service.create_task(
        data_set_id=data_set_id,
        command=TaskDefinition(title="兌換獎勵", event_id=event.id),
    )

    assert service.delete_event(event.id, data_set_id=data_set_id)

    retained = service.get_task(task.id, data_set_id=data_set_id)
    assert retained.event_id is None
    assert retained.event_title is None
    with factory() as session:
        assert session.scalar(select(func.count(EventRecord.id))) == 0
        assert session.scalar(select(func.count(TaskRecord.id))) == 1


def test_event_edits_hide_and_dataset_boundary(task_service) -> None:  # type: ignore[no-untyped-def]
    service, _, (primary_id, demo_id) = task_service
    event = service.create_event(
        data_set_id=primary_id,
        command=EventDefinition(title="活動", event_type="limited"),
    )
    with pytest.raises(ValueError, match="does not belong"):
        service.update_event(
            event.id,
            data_set_id=demo_id,
            command=EventDefinition(title="錯誤資料集"),
        )

    updated = service.update_event(
        event.id,
        data_set_id=primary_id,
        command=EventDefinition(
            title="更新活動",
            event_type="limited",
            status="completed",
            priority=8,
            notes="本機手動資料",
        ),
    )
    assert updated.title == "更新活動"
    assert updated.source_status == "manual"
    assert service.set_event_hidden(event.id, True, data_set_id=primary_id).is_hidden
    assert service.list_events(data_set_id=primary_id, include_hidden=False) == ()


def test_invalid_aggregate_rolls_back_without_partial_write(task_service) -> None:  # type: ignore[no-untyped-def]
    service, factory, (primary_id, demo_id) = task_service
    foreign_event = service.create_event(
        data_set_id=demo_id,
        command=EventDefinition(title="其他資料集"),
    )

    with pytest.raises(ValueError, match="does not belong"):
        service.create_task(
            data_set_id=primary_id,
            command=TaskDefinition(title="不應建立", event_id=foreign_event.id),
        )

    with factory() as session:
        assert session.scalar(select(func.count(TaskRecord.id))) == 0
