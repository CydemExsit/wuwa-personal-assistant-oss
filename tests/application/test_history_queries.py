from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event

from wuwa_assistant.application import echo_history as history_module
from wuwa_assistant.application.echo_history import EchoHistoryService
from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.storage.database import Database
from wuwa_assistant.storage.orm import (
    CaptureSessionRecord,
    DataSetRecord,
    EchoObservationRecord,
    EchoRecord,
    EchoSessionRecord,
)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    database = create_database(AppPaths.from_root(tmp_path))
    try:
        yield database
    finally:
        database.dispose()


def _seed(database: Database, size: int) -> tuple[str, str]:
    with database.session_factory.begin() as session:
        dataset = DataSetRecord(name="Synthetic history", kind="primary", is_active=True)
        session.add(dataset)
        session.flush()
        capture = CaptureSessionRecord(
            data_set_id=dataset.id, backend_kind="fake", monitor_index=0, app_version="test"
        )
        session.add(capture)
        session.flush()
        start = datetime(2026, 1, 1, tzinfo=UTC)
        for index in range(size):
            echo = EchoRecord(id=f"echo-{index}", data_set_id=dataset.id, name=f"Echo {index}")
            record = EchoSessionRecord(
                id=f"session-{index}",
                capture_session_id=capture.id,
                echo_id=echo.id,
                local_session_id=str(index),
                started_at=start + timedelta(seconds=index),
            )
            session.add(echo)
            session.flush()
            session.add(record)
            session.flush()
            # History follows sequence, even when observation clocks move backwards.
            if index % 2:
                session.add_all(
                    EchoObservationRecord(
                        id=f"observation-{index}-{sequence}",
                        echo_session_id=record.id,
                        sequence_no=sequence,
                        page_type="echo_selection",
                        observed_at=start - timedelta(seconds=sequence),
                        requires_review=sequence == 2,
                        raw_payload_json={},
                        effective_payload_json={"raw_ocr": "x" * 2000},
                    )
                    for sequence in (1, 2)
                )
        return dataset.id, capture.id


@pytest.mark.parametrize("size", [10, 1001])
def test_history_queries_are_batched_and_do_not_load_observation_payloads(
    database: Database, size: int
) -> None:
    dataset, _ = _seed(database, size)
    service = EchoHistoryService(database.session_factory)
    statements: list[str] = []
    hydrated: list[object] = []

    def record_query(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    def record_observation(target, _context):
        hydrated.append(target)

    event.listen(database.engine, "before_cursor_execute", record_query)
    event.listen(EchoObservationRecord, "load", record_observation)
    try:
        rows = service.list_history_snapshot(data_set_id=dataset)
    finally:
        event.remove(database.engine, "before_cursor_execute", record_query)
        event.remove(EchoObservationRecord, "load", record_observation)
    assert len(statements) <= 2 + (size + 399) // 400
    assert hydrated == []
    assert not any("raw_payload_json" in statement for statement in statements)
    assert [row.id for row in rows] == [f"session-{i}" for i in reversed(range(size))]
    for row in rows:
        index = int(row.local_session_id)
        assert row.name == f"Echo {index}"
        assert row.observation_count == (2 if index % 2 else 0)
        assert row.requires_review is bool(index % 2)
        assert row.latest_observation_id == (f"observation-{index}-2" if index % 2 else None)
    assert service.list_history(data_set_id=dataset, limit=3, offset=2) == rows[2:5]
    assert service.list_history_snapshot(data_set_id="absent") == ()


def test_grouped_snapshot_folds_only_once_above_page_limit(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset, _ = _seed(database, 1001)
    original = history_module.group_echo_records
    calls: list[str] = []

    def group(session, *, data_set_id):
        calls.append(data_set_id)
        return original(session, data_set_id=data_set_id)

    monkeypatch.setattr(history_module, "group_echo_records", group)
    service = EchoHistoryService(database.session_factory)
    grouped = service.list_history_snapshot(data_set_id=dataset, group_duplicates=True)
    assert len(grouped) == 1001
    assert calls == [dataset]
    assert grouped == service.list_history_snapshot(data_set_id=dataset)


def test_history_snapshot_does_not_mix_a_concurrent_append(database: Database) -> None:
    dataset, capture = _seed(database, 3)
    appended = False

    def append_after_list(_connection, _cursor, statement, _parameters, _context, _many):
        nonlocal appended
        if appended or not statement.startswith("SELECT echo_sessions."):
            return
        appended = True
        with database.session_factory.begin() as writer:
            writer.add(
                EchoObservationRecord(
                    echo_session_id="session-0",
                    sequence_no=1,
                    page_type="echo_selection",
                    raw_payload_json={},
                    effective_payload_json={},
                )
            )
            writer.add(EchoSessionRecord(capture_session_id=capture, local_session_id="new"))

    event.listen(database.engine, "after_cursor_execute", append_after_list)
    try:
        rows = EchoHistoryService(database.session_factory).list_history_snapshot(
            data_set_id=dataset
        )
    finally:
        event.remove(database.engine, "after_cursor_execute", append_after_list)
    assert appended
    assert len(rows) == 3
    assert next(row for row in rows if row.id == "session-0").observation_count == 0
    later = EchoHistoryService(database.session_factory).list_history_snapshot(data_set_id=dataset)
    assert len(later) == 4
    assert next(row for row in later if row.id == "session-0").observation_count == 1
