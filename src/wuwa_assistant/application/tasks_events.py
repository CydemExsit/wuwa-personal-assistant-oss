"""Dataset-scoped, offline task and limited-event persistence.

Every mutation is a short transaction and every result is a detached frozen
snapshot.  Recurring completion retains the completed occurrence and creates
exactly one future occurrence in the same transaction, making stale double
clicks naturally idempotent.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from wuwa_assistant.reminders import (
    DueBucket,
    RecurrenceRule,
    UnknownRecurrenceRule,
    UnknownRecurrenceRuleError,
    classify_due,
    effective_due_at,
    next_occurrence_after,
    parse_recurrence_payload,
    recurrence_payload,
)
from wuwa_assistant.storage.engine import SessionFactory
from wuwa_assistant.storage.orm import (
    DataSetRecord,
    EventRecord,
    TaskRecord,
    new_id,
    utc_now,
)

type ParsedRecurrence = RecurrenceRule | UnknownRecurrenceRule | None


@dataclass(frozen=True, slots=True)
class TaskDefinition:
    title: str
    task_type: str = "custom"
    due_at: datetime | None = None
    priority: int = 100
    recurrence: RecurrenceRule | None = None
    event_id: str | None = None
    timezone_name: str = "Asia/Taipei"
    is_hidden: bool = False
    notes: str | None = None
    preserve_unknown_recurrence: bool = False


@dataclass(frozen=True, slots=True)
class EventDefinition:
    title: str
    event_type: str = "custom"
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    priority: int = 100
    status: str = "active"
    is_hidden: bool = False
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    id: str
    data_set_id: str
    event_id: str | None
    event_title: str | None
    title: str
    task_type: str
    due_at: datetime | None
    effective_due_at: datetime | None
    snoozed_until: datetime | None
    completed_at: datetime | None
    priority: int
    recurrence: ParsedRecurrence
    timezone_name: str
    is_hidden: bool
    notes: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def recurrence_code(self) -> str | None:
        if isinstance(self.recurrence, RecurrenceRule):
            return f"{self.recurrence.frequency.value}:{self.recurrence.interval}"
        return None

    @property
    def requires_manual_recurrence_confirmation(self) -> bool:
        return isinstance(self.recurrence, UnknownRecurrenceRule)

    def due_bucket(self, *, now: datetime) -> DueBucket:
        return classify_due(
            self.due_at,
            now=now,
            timezone_name=self.timezone_name,
            snoozed_until=self.snoozed_until,
        )

    def to_json_dict(self) -> dict[str, Any]:
        recurrence: dict[str, Any] | None
        if isinstance(self.recurrence, RecurrenceRule):
            recurrence = recurrence_payload(self.recurrence)
        elif isinstance(self.recurrence, UnknownRecurrenceRule):
            recurrence = self.recurrence.to_json_dict()
        else:
            recurrence = None
        return {
            "id": self.id,
            "data_set_id": self.data_set_id,
            "event_id": self.event_id,
            "event_title": self.event_title,
            "title": self.title,
            "task_type": self.task_type,
            "due_at": _iso(self.due_at),
            "effective_due_at": _iso(self.effective_due_at),
            "snoozed_until": _iso(self.snoozed_until),
            "completed_at": _iso(self.completed_at),
            "priority": self.priority,
            "recurrence": recurrence,
            "timezone_name": self.timezone_name,
            "is_hidden": self.is_hidden,
            "notes": self.notes,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class EventSnapshot:
    id: str
    data_set_id: str
    title: str
    event_type: str
    starts_at: datetime | None
    ends_at: datetime | None
    priority: int
    status: str
    is_hidden: bool
    source_status: str
    notes: str | None
    created_at: datetime
    updated_at: datetime

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "data_set_id": self.data_set_id,
            "title": self.title,
            "event_type": self.event_type,
            "starts_at": _iso(self.starts_at),
            "ends_at": _iso(self.ends_at),
            "priority": self.priority,
            "status": self.status,
            "is_hidden": self.is_hidden,
            "source_status": self.source_status,
            "notes": self.notes,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class TaskCompletionSnapshot:
    completed_task: TaskSnapshot
    next_occurrence: TaskSnapshot | None
    was_already_completed: bool


@dataclass(frozen=True, slots=True)
class DueBucketSnapshot:
    overdue: tuple[TaskSnapshot, ...] = ()
    today: tuple[TaskSnapshot, ...] = ()
    next_three_days: tuple[TaskSnapshot, ...] = ()
    this_week: tuple[TaskSnapshot, ...] = ()
    undated: tuple[TaskSnapshot, ...] = ()
    later: tuple[TaskSnapshot, ...] = ()

    @property
    def counts(self) -> dict[str, int]:
        return {
            DueBucket.OVERDUE.value: len(self.overdue),
            DueBucket.TODAY.value: len(self.today),
            DueBucket.NEXT_THREE_DAYS.value: len(self.next_three_days),
            DueBucket.THIS_WEEK.value: len(self.this_week),
            DueBucket.UNDATED.value: len(self.undated),
            DueBucket.LATER.value: len(self.later),
        }


class TasksEventsService:
    """Transactional application boundary for manually managed reminders."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def create_task(self, *, data_set_id: str, command: TaskDefinition) -> TaskSnapshot:
        normalized = _normalize_task(command)
        if normalized.preserve_unknown_recurrence:
            raise ValueError("preserve_unknown_recurrence is only valid for an existing task")
        with self._session_factory.begin() as session:
            _require_data_set(session, data_set_id)
            _require_optional_event(session, normalized.event_id, data_set_id)
            rule = _new_rule(normalized.recurrence)
            record = TaskRecord(
                data_set_id=data_set_id,
                event_id=normalized.event_id,
                title=normalized.title,
                task_type=normalized.task_type,
                due_at=_as_utc(normalized.due_at),
                priority=normalized.priority,
                recurrence_rule_json=None if rule is None else recurrence_payload(rule),
                timezone_name=normalized.timezone_name,
                is_hidden=normalized.is_hidden,
                notes=normalized.notes,
            )
            session.add(record)
            session.flush()
            return _task_snapshot(session, record)

    def update_task(
        self,
        task_id: str,
        *,
        data_set_id: str,
        command: TaskDefinition,
    ) -> TaskSnapshot:
        normalized = _normalize_task(command)
        with self._session_factory.begin() as session:
            record = _require_task(session, task_id, data_set_id)
            _require_optional_event(session, normalized.event_id, data_set_id)
            existing = parse_recurrence_payload(record.recurrence_rule_json)
            if normalized.preserve_unknown_recurrence:
                if not isinstance(existing, UnknownRecurrenceRule):
                    raise ValueError("the existing task does not have an unknown recurrence rule")
                recurrence_json = record.recurrence_rule_json
            else:
                rule = _rule_for_update(existing, normalized.recurrence)
                recurrence_json = None if rule is None else recurrence_payload(rule)
            record.event_id = normalized.event_id
            record.title = normalized.title
            record.task_type = normalized.task_type
            record.due_at = _as_utc(normalized.due_at)
            record.priority = normalized.priority
            record.recurrence_rule_json = recurrence_json
            record.timezone_name = normalized.timezone_name
            record.is_hidden = normalized.is_hidden
            record.notes = normalized.notes
            record.updated_at = utc_now()
            session.flush()
            return _task_snapshot(session, record)

    def get_task(self, task_id: str, *, data_set_id: str) -> TaskSnapshot:
        with self._session_factory() as session:
            _require_data_set(session, data_set_id)
            return _task_snapshot(session, _require_task(session, task_id, data_set_id))

    def list_tasks(
        self,
        *,
        data_set_id: str,
        include_completed: bool = True,
        include_hidden: bool = True,
    ) -> tuple[TaskSnapshot, ...]:
        with self._session_factory() as session:
            _require_data_set(session, data_set_id)
            statement = select(TaskRecord).where(TaskRecord.data_set_id == data_set_id)
            if not include_completed:
                statement = statement.where(TaskRecord.completed_at.is_(None))
            if not include_hidden:
                statement = statement.where(TaskRecord.is_hidden.is_(False))
            rows = session.scalars(
                statement.order_by(
                    TaskRecord.completed_at.is_not(None),
                    TaskRecord.priority,
                    TaskRecord.due_at.is_(None),
                    TaskRecord.due_at,
                    TaskRecord.title,
                    TaskRecord.id,
                )
            ).all()
            return tuple(_task_snapshot(session, row) for row in rows)

    def complete_task(
        self,
        task_id: str,
        *,
        data_set_id: str,
        completed_at: datetime | None = None,
    ) -> TaskCompletionSnapshot:
        completed = _as_utc(completed_at or utc_now())
        assert completed is not None
        with self._session_factory.begin() as session:
            record = _require_task(session, task_id, data_set_id)
            if record.completed_at is not None:
                return TaskCompletionSnapshot(
                    completed_task=_task_snapshot(session, record),
                    next_occurrence=None,
                    was_already_completed=True,
                )
            parsed = parse_recurrence_payload(record.recurrence_rule_json)
            if isinstance(parsed, UnknownRecurrenceRule):
                raise UnknownRecurrenceRuleError(
                    "cannot complete a recurring task whose rule is not understood; edit it first"
                )

            record.completed_at = completed
            record.snoozed_until = None
            record.updated_at = utc_now()
            next_record: TaskRecord | None = None
            if isinstance(parsed, RecurrenceRule):
                scheduled = _from_storage(record.due_at)
                if scheduled is None:
                    raise UnknownRecurrenceRuleError(
                        "cannot advance a recurring task without a confirmed due time"
                    )
                next_due = next_occurrence_after(
                    scheduled,
                    completed,
                    parsed,
                    timezone_name=record.timezone_name,
                )
                next_record = TaskRecord(
                    id=new_id(),
                    data_set_id=data_set_id,
                    event_id=record.event_id,
                    title=record.title,
                    task_type=record.task_type,
                    due_at=next_due,
                    priority=record.priority,
                    recurrence_rule_json=recurrence_payload(parsed.next_index()),
                    timezone_name=record.timezone_name,
                    is_hidden=record.is_hidden,
                    notes=record.notes,
                )
                session.add(next_record)
            session.flush()
            return TaskCompletionSnapshot(
                completed_task=_task_snapshot(session, record),
                next_occurrence=(
                    None if next_record is None else _task_snapshot(session, next_record)
                ),
                was_already_completed=False,
            )

    def snooze_task(
        self,
        task_id: str,
        snoozed_until: datetime,
        *,
        data_set_id: str,
        now: datetime | None = None,
    ) -> TaskSnapshot:
        safe_until = _as_utc(snoozed_until)
        safe_now = _as_utc(now or utc_now())
        assert safe_until is not None and safe_now is not None
        if safe_until <= safe_now:
            raise ValueError("snoozed_until must be in the future")
        with self._session_factory.begin() as session:
            record = _require_task(session, task_id, data_set_id)
            if record.completed_at is not None:
                raise ValueError("a completed task cannot be snoozed")
            record.snoozed_until = safe_until
            record.updated_at = utc_now()
            session.flush()
            return _task_snapshot(session, record)

    def set_task_hidden(
        self,
        task_id: str,
        hidden: bool,
        *,
        data_set_id: str,
    ) -> TaskSnapshot:
        if not isinstance(hidden, bool):
            raise ValueError("hidden must be a boolean")
        with self._session_factory.begin() as session:
            record = _require_task(session, task_id, data_set_id)
            if record.is_hidden != hidden:
                record.is_hidden = hidden
                record.updated_at = utc_now()
            session.flush()
            return _task_snapshot(session, record)

    def delete_task(self, task_id: str, *, data_set_id: str) -> bool:
        with self._session_factory.begin() as session:
            record = _require_task(session, task_id, data_set_id)
            session.delete(record)
            session.flush()
            return True

    def due_buckets(
        self,
        *,
        data_set_id: str,
        now: datetime,
    ) -> DueBucketSnapshot:
        safe_now = _require_aware(now, "now")
        tasks = self.list_tasks(
            data_set_id=data_set_id,
            include_completed=False,
            include_hidden=False,
        )
        grouped: dict[DueBucket, list[TaskSnapshot]] = {bucket: [] for bucket in DueBucket}
        for task in tasks:
            grouped[task.due_bucket(now=safe_now)].append(task)
        return DueBucketSnapshot(
            overdue=tuple(grouped[DueBucket.OVERDUE]),
            today=tuple(grouped[DueBucket.TODAY]),
            next_three_days=tuple(grouped[DueBucket.NEXT_THREE_DAYS]),
            this_week=tuple(grouped[DueBucket.THIS_WEEK]),
            undated=tuple(grouped[DueBucket.UNDATED]),
            later=tuple(grouped[DueBucket.LATER]),
        )

    def create_event(self, *, data_set_id: str, command: EventDefinition) -> EventSnapshot:
        normalized = _normalize_event(command)
        with self._session_factory.begin() as session:
            _require_data_set(session, data_set_id)
            record = EventRecord(
                data_set_id=data_set_id,
                title=normalized.title,
                event_type=normalized.event_type,
                starts_at=_as_utc(normalized.starts_at),
                ends_at=_as_utc(normalized.ends_at),
                priority=normalized.priority,
                status=normalized.status,
                is_hidden=normalized.is_hidden,
                source_status="manual",
                notes=normalized.notes,
            )
            session.add(record)
            session.flush()
            return _event_snapshot(record)

    def update_event(
        self,
        event_id: str,
        *,
        data_set_id: str,
        command: EventDefinition,
    ) -> EventSnapshot:
        normalized = _normalize_event(command)
        with self._session_factory.begin() as session:
            record = _require_event(session, event_id, data_set_id)
            record.title = normalized.title
            record.event_type = normalized.event_type
            record.starts_at = _as_utc(normalized.starts_at)
            record.ends_at = _as_utc(normalized.ends_at)
            record.priority = normalized.priority
            record.status = normalized.status
            record.is_hidden = normalized.is_hidden
            record.source_status = "manual"
            record.notes = normalized.notes
            record.updated_at = utc_now()
            session.flush()
            return _event_snapshot(record)

    def get_event(self, event_id: str, *, data_set_id: str) -> EventSnapshot:
        with self._session_factory() as session:
            _require_data_set(session, data_set_id)
            return _event_snapshot(_require_event(session, event_id, data_set_id))

    def list_events(
        self,
        *,
        data_set_id: str,
        include_hidden: bool = True,
    ) -> tuple[EventSnapshot, ...]:
        with self._session_factory() as session:
            _require_data_set(session, data_set_id)
            statement = select(EventRecord).where(EventRecord.data_set_id == data_set_id)
            if not include_hidden:
                statement = statement.where(EventRecord.is_hidden.is_(False))
            rows = session.scalars(
                statement.order_by(
                    EventRecord.priority,
                    EventRecord.ends_at.is_(None),
                    EventRecord.ends_at,
                    EventRecord.title,
                    EventRecord.id,
                )
            ).all()
            return tuple(_event_snapshot(row) for row in rows)

    def set_event_hidden(
        self,
        event_id: str,
        hidden: bool,
        *,
        data_set_id: str,
    ) -> EventSnapshot:
        if not isinstance(hidden, bool):
            raise ValueError("hidden must be a boolean")
        with self._session_factory.begin() as session:
            record = _require_event(session, event_id, data_set_id)
            if record.is_hidden != hidden:
                record.is_hidden = hidden
                record.updated_at = utc_now()
            session.flush()
            return _event_snapshot(record)

    def delete_event(self, event_id: str, *, data_set_id: str) -> bool:
        """Delete the event while the schema's SET NULL FK keeps linked tasks."""

        with self._session_factory.begin() as session:
            record = _require_event(session, event_id, data_set_id)
            session.delete(record)
            session.flush()
            return True


def _normalize_task(command: TaskDefinition) -> TaskDefinition:
    if not isinstance(command, TaskDefinition):
        raise ValueError("command must be TaskDefinition")
    title = _text(command.title, "task title", 240)
    task_type = _text(command.task_type, "task type", 60)
    due_at = _optional_aware(command.due_at, "due_at")
    priority = _priority(command.priority)
    if command.recurrence is not None and not isinstance(command.recurrence, RecurrenceRule):
        raise ValueError("recurrence must be RecurrenceRule or null")
    if command.recurrence is not None and due_at is None:
        raise ValueError("a recurring task requires due_at")
    timezone_name = _timezone(command.timezone_name)
    event_id = _optional_text(command.event_id, "event_id", 36)
    hidden = _boolean(command.is_hidden, "is_hidden")
    preserve_unknown = _boolean(
        command.preserve_unknown_recurrence,
        "preserve_unknown_recurrence",
    )
    notes = _optional_text(command.notes, "notes", 10_000)
    return replace(
        command,
        title=title,
        task_type=task_type,
        due_at=due_at,
        priority=priority,
        event_id=event_id,
        timezone_name=timezone_name,
        is_hidden=hidden,
        notes=notes,
        preserve_unknown_recurrence=preserve_unknown,
    )


def _normalize_event(command: EventDefinition) -> EventDefinition:
    if not isinstance(command, EventDefinition):
        raise ValueError("command must be EventDefinition")
    starts_at = _optional_aware(command.starts_at, "starts_at")
    ends_at = _optional_aware(command.ends_at, "ends_at")
    if starts_at is not None and ends_at is not None and ends_at < starts_at:
        raise ValueError("event ends_at must not be earlier than starts_at")
    return replace(
        command,
        title=_text(command.title, "event title", 240),
        event_type=_text(command.event_type, "event type", 60),
        starts_at=starts_at,
        ends_at=ends_at,
        priority=_priority(command.priority),
        status=_text(command.status, "event status", 40),
        is_hidden=_boolean(command.is_hidden, "is_hidden"),
        notes=_optional_text(command.notes, "notes", 10_000),
    )


def _new_rule(rule: RecurrenceRule | None) -> RecurrenceRule | None:
    if rule is None:
        return None
    if rule.series_id:
        return rule
    return RecurrenceRule.new(rule.frequency, rule.interval)


def _rule_for_update(
    existing: ParsedRecurrence,
    requested: RecurrenceRule | None,
) -> RecurrenceRule | None:
    if requested is None:
        return None
    if (
        isinstance(existing, RecurrenceRule)
        and existing.frequency is requested.frequency
        and existing.interval == requested.interval
    ):
        return existing
    return _new_rule(requested)


def _require_data_set(session: Session, data_set_id: str) -> DataSetRecord:
    record = session.get(DataSetRecord, data_set_id)
    if record is None:
        raise ValueError("data set does not exist")
    return record


def _require_task(session: Session, task_id: str, data_set_id: str) -> TaskRecord:
    record = session.get(TaskRecord, task_id)
    if record is None:
        raise ValueError("task does not exist")
    if record.data_set_id != data_set_id:
        raise ValueError("task does not belong to the requested data set")
    return record


def _require_event(session: Session, event_id: str, data_set_id: str) -> EventRecord:
    record = session.get(EventRecord, event_id)
    if record is None:
        raise ValueError("event does not exist")
    if record.data_set_id != data_set_id:
        raise ValueError("event does not belong to the requested data set")
    return record


def _require_optional_event(
    session: Session,
    event_id: str | None,
    data_set_id: str,
) -> EventRecord | None:
    return None if event_id is None else _require_event(session, event_id, data_set_id)


def _task_snapshot(session: Session, record: TaskRecord) -> TaskSnapshot:
    event = None if record.event_id is None else session.get(EventRecord, record.event_id)
    due_at = _from_storage(record.due_at)
    snoozed_until = _from_storage(record.snoozed_until)
    return TaskSnapshot(
        id=record.id,
        data_set_id=record.data_set_id,
        event_id=record.event_id,
        event_title=None if event is None else event.title,
        title=record.title,
        task_type=record.task_type,
        due_at=due_at,
        effective_due_at=effective_due_at(due_at, snoozed_until),
        snoozed_until=snoozed_until,
        completed_at=_from_storage(record.completed_at),
        priority=record.priority,
        recurrence=parse_recurrence_payload(record.recurrence_rule_json),
        timezone_name=record.timezone_name,
        is_hidden=record.is_hidden,
        notes=record.notes,
        created_at=_from_storage_required(record.created_at),
        updated_at=_from_storage_required(record.updated_at),
    )


def _event_snapshot(record: EventRecord) -> EventSnapshot:
    return EventSnapshot(
        id=record.id,
        data_set_id=record.data_set_id,
        title=record.title,
        event_type=record.event_type,
        starts_at=_from_storage(record.starts_at),
        ends_at=_from_storage(record.ends_at),
        priority=record.priority,
        status=record.status,
        is_hidden=record.is_hidden,
        source_status=record.source_status,
        notes=record.notes,
        created_at=_from_storage_required(record.created_at),
        updated_at=_from_storage_required(record.updated_at),
    )


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _require_aware(value, "datetime").astimezone(UTC)


def _from_storage(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _from_storage_required(value: datetime) -> datetime:
    result = _from_storage(value)
    assert result is not None
    return result


def _require_aware(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{label} must be a timezone-aware datetime")
    return value


def _optional_aware(value: datetime | None, label: str) -> datetime | None:
    if value is None:
        return None
    return _require_aware(value, label)


def _timezone(value: str) -> str:
    safe = _text(value, "timezone_name", 80)
    try:
        ZoneInfo(safe)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone_name must be an installed IANA timezone") from exc
    return safe


def _priority(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("priority must be a nonnegative integer")
    return value


def _boolean(value: bool, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


def _text(value: str, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    safe = value.strip()
    if not safe:
        raise ValueError(f"{label} cannot be empty")
    if len(safe) > maximum:
        raise ValueError(f"{label} must not exceed {maximum} characters")
    return safe


def _optional_text(value: str | None, label: str, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text or null")
    safe = value.strip()
    if not safe:
        return None
    if len(safe) > maximum:
        raise ValueError(f"{label} must not exceed {maximum} characters")
    return safe


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


TaskEventService = TasksEventsService

__all__ = [
    "DueBucketSnapshot",
    "EventDefinition",
    "EventSnapshot",
    "TaskCompletionSnapshot",
    "TaskDefinition",
    "TaskEventService",
    "TaskSnapshot",
    "TasksEventsService",
]
