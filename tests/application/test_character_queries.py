from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event

from wuwa_assistant.application.characters import CharacterCultivationService
from wuwa_assistant.application.echo_duplicates import group_echo_records
from wuwa_assistant.application.equipment_overview import _latest_observations
from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.storage.database import Database
from wuwa_assistant.storage.orm import (
    CaptureSessionRecord,
    CharacterRecord,
    DataSetRecord,
    EchoObservationRecord,
    EchoRecord,
    EchoSessionRecord,
    WeaponRecord,
)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    database = create_database(AppPaths.from_root(tmp_path))
    try:
        yield database
    finally:
        database.dispose()


@pytest.mark.parametrize("size", [10, 100, 401])
def test_roster_queries_scale_by_batch_and_preserve_details(database: Database, size: int) -> None:
    with database.session_factory.begin() as session:
        dataset = DataSetRecord(name="Synthetic", kind="primary", is_active=True)
        session.add(dataset)
        session.flush()
        dataset_id = dataset.id
        for index in range(size):
            character = CharacterRecord(
                id=f"owner-{index}", data_set_id=dataset.id, name=f"Owner {index}", priority=index
            )
            session.add(character)
            session.flush()
            session.add(
                WeaponRecord(
                    data_set_id=dataset.id, character_id=character.id, name=f"Weapon {index}"
                )
            )
    statements: list[str] = []

    def count(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(database.engine, "before_cursor_execute", count)
    service = CharacterCultivationService(database.session_factory)
    try:
        details = service.list_character_details(data_set_id=dataset_id)
    finally:
        event.remove(database.engine, "before_cursor_execute", count)
    assert len(statements) <= 3 + 4 * ((size + 399) // 400)
    assert [row.summary.id for row in details] == [f"owner-{i}" for i in range(size)]
    assert [row.weapon.name for row in details if row.weapon] == [
        f"Weapon {i}" for i in range(size)
    ]
    assert service.list_characters(data_set_id=dataset_id) == tuple(row.summary for row in details)
    assert service.get_character("owner-0", data_set_id=dataset_id) == details[0]


def test_latest_observation_uses_original_ties_and_loads_only_winner(database: Database) -> None:
    with database.session_factory.begin() as session:
        dataset = DataSetRecord(name="Synthetic", kind="primary", is_active=True)
        other = DataSetRecord(name="Other", kind="demo", is_active=False)
        session.add_all([dataset, other])
        session.flush()
        dataset_id = dataset.id
        echo = EchoRecord(id="echo", data_set_id=dataset.id, name="Echo")
        session.add(echo)
        session.flush()
        now = datetime(2026, 1, 1, tzinfo=UTC)
        for index, capture_dataset in enumerate([dataset.id, dataset.id, other.id]):
            capture = CaptureSessionRecord(
                data_set_id=capture_dataset,
                backend_kind="fake",
                monitor_index=0,
                app_version="test",
            )
            session.add(capture)
            session.flush()
            record = EchoSessionRecord(
                capture_session_id=capture.id, echo_id=echo.id, local_session_id="session"
            )
            session.add(record)
            session.flush()
            for sequence in range(1, 11):
                session.add(
                    EchoObservationRecord(
                        id=f"obs-{index}-{sequence:02}",
                        echo_session_id=record.id,
                        sequence_no=sequence,
                        observed_at=now + timedelta(days=1 if index == 2 else 0),
                        page_type="echo_selection",
                        raw_payload_json={"large": "x" * 2000},
                        effective_payload_json={},
                        requires_review=True,
                    )
                )
    loaded: list[str] = []

    def on_load(target, _context):
        loaded.append(target.id)

    event.listen(EchoObservationRecord, "load", on_load)
    try:
        with database.session_factory() as session:
            latest = _latest_observations(session, dataset_id)
            assert latest["echo"].id == "obs-1-10"
            assert latest["echo"].requires_review
        assert loaded == ["obs-1-10"]
        loaded.clear()
        with database.session_factory() as session:
            group_echo_records(session, data_set_id=dataset_id)
        assert loaded == []
    finally:
        event.remove(EchoObservationRecord, "load", on_load)
