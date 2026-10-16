from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from wuwa_assistant.application import (
    CharacterCultivationService,
    EchoDisposition,
    EchoHistoryService,
)
from wuwa_assistant.application.characters import CreateCharacter
from wuwa_assistant.live import LiveEchoRoiAnalyzer
from wuwa_assistant.models import EchoObservation, EchoStat, PageType, StatKind, to_jsonable
from wuwa_assistant.state import EchoStateTracker
from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.storage.database import Database
from wuwa_assistant.storage.orm import (
    CaptureSessionRecord,
    CharacterRecord,
    DataSetRecord,
    EchoEventRecord,
    EchoObservationRecord,
    EchoRecord,
    EchoSessionRecord,
    ObservationCorrectionRecord,
)


@pytest.fixture
def storage(tmp_path: Path) -> Iterator[tuple[Database, EchoHistoryService]]:
    database = create_database(AppPaths.from_root(tmp_path / "history"))
    try:
        yield database, EchoHistoryService(database.session_factory)
    finally:
        database.dispose()


def _stat(
    name: str,
    value: int | float,
    unit: str,
    *,
    kind: str = "substat",
    slot: int | None = None,
    confidence: float = 0.95,
) -> dict[str, Any]:
    return {
        "stat_kind": kind,
        "name": name,
        "value": value,
        "unit": unit,
        "confidence": confidence,
        "name_confidence": confidence,
        "value_confidence": confidence,
        "raw_name": name,
        "raw_value": f"{value}{unit}",
        "slot_index": slot,
    }


def _observation(
    *,
    level: int = 0,
    confidence: float = 0.95,
    name: str = "不熄獵手",
    local_session_id: str = "echo-session-0001",
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "page_type": "echo_enhancement",
        "name": name,
        "level": level,
        "projected_level": None,
        "cost": 1,
        "main_stats": [
            _stat("生命", 4.5 if level == 0 else 8.2, "%", kind="primary"),
            _stat("生命", 456 if level == 0 else 820, "", kind="fixed"),
        ],
        "projected_main_stats": [],
        "sub_stats": [] if level == 0 else [_stat("暴擊", 7.5, "%", slot=1)],
        "locked_substat_slots": 5 if level == 0 else 4,
        "confidence": {
            "page_type": confidence,
            "name": confidence,
            "level": confidence,
        },
        "source_timestamp": "2026-08-01T12:00:00+00:00",
        "source_file": r"D:\private\4.png",
        "is_discarded": False,
        "material_source_selected": None,
        "material_source_level": None,
        "raw_ocr": {"name_level": [{"text": name, "confidence": confidence}]},
        "debug_roi_paths": [],
        "debug_ocr_path": None,
        "semantic_fingerprint": f"{local_session_id}:{level}",
        "current_session": {
            "local_session_id": local_session_id,
            "current_state": "active",
            "is_discarded": False,
            "was_consumed": False,
            "consumed_into_session_id": None,
            "latest_level": level,
        },
        "events": events or [],
    }
    return payload


def _start(service: EchoHistoryService, *, data_set_id: str | None = None) -> str:
    return service.start_capture(
        data_set_id=data_set_id,
        backend_kind="mss_monitor",
        monitor_index=1,
        sampling_hz=5.0,
        stable_wait_ms=600,
        app_version="0.4.0",
        roi_config_version="tw-2560x1440-v1",
        metadata={"manual_start": True},
    ).id


def _selection_identity(
    cell: int = 1,
    value: str = "0000000000001111",
) -> dict[str, object]:
    cell_index = cell - 1
    return {
        "version": "selection-grid-v1",
        "selected_cell": cell,
        "selected_card_bbox": [
            5 + 179 * (cell_index % 3),
            8 + 220 * (cell_index // 3),
            152,
            190,
        ],
        "card_art_phash": value,
        "context_phashes": [value] * 15,
    }


def _complete_stage_observation(
    level: int,
    *,
    page_type: str,
    local_session_id: str = "echo-session-0001",
) -> dict[str, Any]:
    payload = _observation(level=level, local_session_id=local_session_id)
    payload["page_type"] = page_type
    payload["sub_stats"] = [
        _stat("攻擊", 40 + index * 10, "", slot=index) for index in range(level // 5)
    ]
    payload["locked_substat_slots"] = 5 - level // 5
    if page_type == "echo_selection":
        payload["cost"] = 1
        payload["locked_substat_slots"] = None
    return payload


@pytest.mark.parametrize("level", range(26))
@pytest.mark.parametrize("page_type", ["echo_enhancement", "echo_selection"])
def test_complete_echo_stage_does_not_require_review(
    storage: tuple[Database, EchoHistoryService],
    level: int,
    page_type: str,
) -> None:
    _database, service = storage
    capture_id = _start(service)
    payload = _complete_stage_observation(level, page_type=page_type)

    receipt = service.ingest_live_result(capture_id, payload, source_sequence=1)

    assert receipt.echo_session_id is not None
    observation = service.get_history(receipt.echo_session_id).observations[0]
    assert observation.requires_review is False


@pytest.mark.parametrize("page_type", ["echo_enhancement", "echo_selection"])
def test_missing_or_extra_expected_substat_requires_review(
    storage: tuple[Database, EchoHistoryService],
    page_type: str,
) -> None:
    _database, service = storage
    capture_id = _start(service)
    missing = _complete_stage_observation(
        15,
        page_type=page_type,
        local_session_id="echo-session-missing",
    )
    missing["sub_stats"].pop()
    extra = _complete_stage_observation(
        15,
        page_type=page_type,
        local_session_id="echo-session-extra",
    )
    extra["sub_stats"].append(_stat("生命", 470, "", slot=4))

    missing_receipt = service.ingest_live_result(capture_id, missing, source_sequence=1)
    extra_receipt = service.ingest_live_result(capture_id, extra, source_sequence=2)

    assert missing_receipt.echo_session_id is not None
    assert extra_receipt.echo_session_id is not None
    assert (
        service.get_history(missing_receipt.echo_session_id).observations[0].requires_review is True
    )
    assert (
        service.get_history(extra_receipt.echo_session_id).observations[0].requires_review is True
    )


def test_selection_requires_name_level_cost_and_two_resolved_main_stats(
    storage: tuple[Database, EchoHistoryService],
) -> None:
    _database, service = storage
    capture_id = _start(service)
    changes: tuple[tuple[str, object], ...] = (
        ("name", None),
        ("level", None),
        ("cost", None),
        (
            "main_stats",
            [
                _stat("攻擊", 18.0, "%", kind="primary"),
                {
                    **_stat("生命", 2280, "", kind="fixed"),
                    "name": None,
                    "value": None,
                },
            ],
        ),
    )

    for sequence, (field, value) in enumerate(changes, start=1):
        payload = _complete_stage_observation(
            0,
            page_type="echo_selection",
            local_session_id=f"echo-session-{sequence:04d}",
        )
        payload[field] = value
        receipt = service.ingest_live_result(capture_id, payload, source_sequence=sequence)
        assert receipt.echo_session_id is not None
        assert service.get_history(receipt.echo_session_id).observations[0].requires_review is True


def test_enhancement_requires_name_level_two_main_stats_and_locked_slot_count(
    storage: tuple[Database, EchoHistoryService],
) -> None:
    _database, service = storage
    capture_id = _start(service)
    changes: tuple[tuple[str, object], ...] = (
        ("name", None),
        ("level", None),
        ("main_stats", []),
        ("locked_substat_slots", None),
        ("locked_substat_slots", 3),
    )

    for sequence, (field, value) in enumerate(changes, start=1):
        payload = _complete_stage_observation(
            15,
            page_type="echo_enhancement",
            local_session_id=f"echo-session-{sequence:04d}",
        )
        payload[field] = value
        receipt = service.ingest_live_result(capture_id, payload, source_sequence=sequence)
        assert receipt.echo_session_id is not None
        assert service.get_history(receipt.echo_session_id).observations[0].requires_review is True


def test_material_page_keeps_transition_confidence_semantics(
    storage: tuple[Database, EchoHistoryService],
) -> None:
    _database, service = storage
    capture_id = _start(service)
    payload = _observation()
    payload["page_type"] = "enhancement_material_selection"
    payload["name"] = None
    payload["main_stats"] = []
    payload["sub_stats"] = []
    payload["locked_substat_slots"] = None

    receipt = service.ingest_live_result(capture_id, payload, source_sequence=1)

    assert receipt.echo_session_id is not None
    observation = service.get_history(receipt.echo_session_id).observations[0]
    assert observation.requires_review is False


def test_owner_transition_on_same_echo_revisit_is_preserved_in_history(
    storage: tuple[Database, EchoHistoryService],
) -> None:
    _database, service = storage
    capture_id = _start(service)
    first = _complete_stage_observation(0, page_type="echo_selection")
    first["selection_identity"] = _selection_identity()
    first["is_equipped"] = True
    first["equipped_character_name"] = "角色一"
    first["confidence"]["equipped_character_name"] = 0.95
    initial = service.ingest_live_result(capture_id, first, source_sequence=1)
    assert initial.echo_session_id is not None and initial.created_character_id is not None

    changed = _complete_stage_observation(0, page_type="echo_selection")
    changed["selection_identity"] = _selection_identity()
    changed["revisit_status"] = "reused"
    changed["revisit_session_id"] = "echo-session-0001"
    changed["is_equipped"] = True
    changed["equipped_character_name"] = "角色二"
    changed["confidence"]["equipped_character_name"] = 0.95
    next_owner = service.ingest_live_result(capture_id, changed, source_sequence=2)
    assert next_owner.echo_session_id == initial.echo_session_id
    assert next_owner.observation_id is not None and next_owner.created_character_id is not None

    changed["equipped_character_name"] = None
    changed["is_equipped"] = None
    unknown_owner = service.ingest_live_result(capture_id, changed, source_sequence=3)
    assert unknown_owner.observation_id is not None and unknown_owner.created_character_id is None
    history = service.get_history(initial.echo_session_id)
    assert [item.effective_payload["equipped_character_name"] for item in history.observations] == [
        "角色一",
        "角色二",
        None,
    ]
    assert history.session.character_id == next_owner.created_character_id
    assert history.session.slot_index is None


def test_exact_revisit_adds_event_without_duplicate_observation(
    storage: tuple[Database, EchoHistoryService],
) -> None:
    _database, service = storage
    capture_id = _start(service)
    first = _complete_stage_observation(0, page_type="echo_selection")
    first["selection_identity"] = _selection_identity()
    initial = service.ingest_live_result(capture_id, first, source_sequence=1)
    assert initial.echo_session_id is not None

    revisit = _complete_stage_observation(0, page_type="echo_selection")
    revisit["selection_identity"] = _selection_identity()
    revisit["revisit_status"] = "reused"
    revisit["revisit_session_id"] = "echo-session-0001"
    revisit["semantic_fingerprint"] = "content:echo_selection:new-state-hash"
    revisit["events"] = [
        {
            "event_type": "echo_selected",
            "session_id": "echo-session-0001",
            "confidence": 0.95,
            "notes": ["revisited=true"],
        }
    ]

    receipt = service.ingest_live_result(capture_id, revisit, source_sequence=2)
    history = service.get_history(initial.echo_session_id)

    assert receipt.echo_session_id == initial.echo_session_id
    assert receipt.observation_id is None
    assert len(history.observations) == 1
    assert [event.notes for event in history.events] == [("revisited=true",)]


@pytest.mark.parametrize(
    "mismatched_identity",
    [
        _selection_identity(2, "0000000000002222"),
        _selection_identity(1, "ffffffffffffffff"),
    ],
    ids=["different-cell-and-hash", "same-cell-different-hash"],
)
def test_reused_revisit_rejects_identity_that_differs_from_durable_evidence(
    storage: tuple[Database, EchoHistoryService],
    mismatched_identity: dict[str, object],
) -> None:
    database, service = storage
    capture_id = _start(service)
    first = _complete_stage_observation(0, page_type="echo_selection")
    first["selection_identity"] = _selection_identity()
    initial = service.ingest_live_result(capture_id, first, source_sequence=1)
    assert initial.echo_session_id is not None

    revisit = _complete_stage_observation(0, page_type="echo_selection")
    revisit["selection_identity"] = mismatched_identity
    revisit["revisit_status"] = "reused"
    revisit["revisit_session_id"] = "echo-session-0001"
    revisit["events"] = [
        {
            "event_type": "echo_selected",
            "session_id": "echo-session-0001",
            "confidence": 0.95,
            "notes": ["revisited=true"],
        }
    ]

    with pytest.raises(ValueError, match="does not match persisted selection identity"):
        service.ingest_live_result(capture_id, revisit, source_sequence=2)

    history = service.get_history(initial.echo_session_id)
    assert len(history.observations) == 1
    assert history.events == ()
    with database.session_factory() as session:
        assert session.scalar(select(func.count(EchoSessionRecord.id))) == 1
        assert session.scalar(select(func.count(EchoObservationRecord.id))) == 1
        assert session.scalar(select(func.count(EchoEventRecord.id))) == 0


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("wrong_page", "only valid for echo_selection"),
        ("missing_identity", "requires selection_identity"),
        ("wrong_session", "must target current_session"),
        ("missing_current", "requires current_session"),
        ("unknown_identity_version", "unsupported selection_identity.version"),
        ("incoherent_bbox", "bbox does not match selected_cell"),
    ],
)
def test_reused_revisit_metadata_fails_closed_before_database_write(
    storage: tuple[Database, EchoHistoryService],
    case: str,
    message: str,
) -> None:
    database, service = storage
    capture_id = _start(service)
    payload = _complete_stage_observation(0, page_type="echo_selection")
    payload["selection_identity"] = _selection_identity()
    payload["revisit_status"] = "reused"
    payload["revisit_session_id"] = "echo-session-0001"

    if case == "wrong_page":
        payload["page_type"] = "echo_enhancement"
    elif case == "missing_identity":
        payload["selection_identity"] = None
    elif case == "wrong_session":
        payload["revisit_session_id"] = "echo-session-9999"
    elif case == "missing_current":
        payload.pop("current_session")
    elif case == "unknown_identity_version":
        identity = payload["selection_identity"]
        assert isinstance(identity, dict)
        identity["version"] = "selection-grid-v2"
    elif case == "incoherent_bbox":
        identity = payload["selection_identity"]
        assert isinstance(identity, dict)
        identity["selected_card_bbox"] = [184, 8, 152, 190]

    with pytest.raises(ValueError, match=message):
        service.ingest_live_result(capture_id, payload, source_sequence=1)

    with database.session_factory() as session:
        assert session.scalar(select(func.count(EchoSessionRecord.id))) == 0
        assert session.scalar(select(func.count(EchoObservationRecord.id))) == 0
        assert session.scalar(select(func.count(EchoEventRecord.id))) == 0


def test_primary_dataset_is_idempotent_and_capture_restart_is_recovered(
    storage: tuple[Database, EchoHistoryService],
) -> None:
    database, service = storage
    data_set_id = service.ensure_active_primary_dataset()
    assert service.ensure_active_primary_dataset() == data_set_id

    first_id = _start(service, data_set_id=data_set_id)
    second_id = _start(EchoHistoryService(database.session_factory), data_set_id=data_set_id)

    with database.session_factory() as session:
        first = session.get(CaptureSessionRecord, first_id)
        second = session.get(CaptureSessionRecord, second_id)
        assert first is not None and first.status == "interrupted"
        assert first.stopped_at is not None
        assert first.error_summary == "superseded by a new capture session"
        assert second is not None and second.status == "active"
        assert session.scalar(select(func.count(DataSetRecord.id))) == 1

    stopped = service.stop_capture(second_id)
    assert stopped.status == "stopped"
    assert service.stop_capture(second_id).stopped_at == stopped.stopped_at


def test_live_results_persist_atomically_are_idempotent_and_survive_reopen(
    storage: tuple[Database, EchoHistoryService],
) -> None:
    database, service = storage
    capture_id = _start(service)
    start_payload = _observation(
        events=[
            {
                "event_type": "page_changed",
                "session_id": None,
                "related_session_id": None,
                "previous_observation": None,
                "current_observation": {"page_type": "echo_enhancement"},
                "confidence": 0.95,
                "notes": ["current_page_type=echo_enhancement"],
                "previous_level": None,
                "current_level": None,
                "new_substats": [],
                "source_file": "3.png",
            },
            {
                "event_type": "echo_session_started",
                "session_id": "echo-session-0001",
                "related_session_id": None,
                "previous_observation": None,
                "current_observation": {"level": 0},
                "confidence": 0.95,
                "notes": [],
                "previous_level": None,
                "current_level": None,
                "new_substats": [],
                "source_file": "3.png",
            },
        ]
    )
    wrapped_result = {
        "generation": 1,
        "source_sequence": 10,
        "captured_at": "2026-08-01T12:00:00Z",
        "packet_kind": "content",
        "state_fingerprint": "state-10",
        "analysis": {
            "page_type": "echo_enhancement",
            "relevant": True,
            "confidence": 0.95,
            "observation": start_payload,
            "request_content": True,
        },
    }
    first = service.ingest_live_result(capture_id, wrapped_result)
    duplicate = service.ingest_live_result(capture_id, start_payload, source_sequence=10)

    assert duplicate.duplicate is True
    assert duplicate.observation_id == first.observation_id
    assert duplicate.event_ids == first.event_ids

    upgraded = _observation(
        level=5,
        events=[
            {
                "event_type": "echo_upgraded",
                "session_id": "echo-session-0001",
                "related_session_id": None,
                "previous_observation": {"level": 0},
                "current_observation": {"level": 5},
                "confidence": 0.94,
                "notes": [],
                "previous_level": 0,
                "current_level": 5,
                "new_substats": [_stat("暴擊", 7.5, "%", slot=1)],
                "source_file": "4.png",
            }
        ],
    )
    second = service.ingest_live_result(
        capture_id,
        upgraded,
        source_sequence=11,
        occurred_at=datetime(2026, 8, 1, 12, 0, 5, tzinfo=UTC),
    )
    service.stop_capture(capture_id)

    reopened = EchoHistoryService(database.session_factory)
    listed = reopened.list_history()
    assert len(listed) == 1
    assert listed[0].id == first.echo_session_id == second.echo_session_id
    assert listed[0].name == "不熄獵手"
    assert listed[0].level == 5
    assert listed[0].observation_count == 2

    history = reopened.get_history(listed[0].id)
    assert [item.sequence_no for item in history.observations] == [1, 2]
    assert [item.event_type for item in history.events] == [
        "page_changed",
        "echo_session_started",
        "echo_upgraded",
    ]
    assert history.observations[0].raw_payload["source_file"] == "4.png"
    assert history.capture.status == "stopped"
    json.dumps(asdict(history), ensure_ascii=False, allow_nan=False)


def test_tracker_discard_cancellation_persists_without_erasing_history(
    storage: tuple[Database, EchoHistoryService],
) -> None:
    database, service = storage
    capture_id = _start(service)
    tracker = EchoStateTracker(defer_material_consumption=True)
    states = [False, True, None, False, None, True, False]
    expected_flags = [False, True, True, False, False, True, False]
    session_ids: set[str] = set()

    for sequence, (observed, expected) in enumerate(zip(states, expected_flags, strict=True), 1):
        observation = EchoObservation(
            page_type=PageType.ECHO_SELECTION,
            name="不熄獵手",
            level=0,
            cost=1,
            main_stats=[
                EchoStat(StatKind.PRIMARY, "生命", 4.5, "%", 0.99, 0.99, 0.99),
                EchoStat(StatKind.FIXED, "生命", 456, None, 0.99, 0.99, 0.99),
            ],
            is_discarded=observed,
            confidence={"page_type": 0.99, "name": 0.99, "level": 0.99},
        )
        events = tracker.process(observation)
        payload = to_jsonable(observation)
        payload["events"] = to_jsonable(events)
        payload["current_session"] = LiveEchoRoiAnalyzer._current_session_summary(tracker)
        receipt = service.ingest_live_result(capture_id, payload, source_sequence=sequence)

        assert receipt.echo_session_id is not None
        session_ids.add(receipt.echo_session_id)
        history = service.get_history(receipt.echo_session_id)
        assert history.session.is_discarded is expected
        assert history.session.current_state == ("discarded_active" if expected else "active")
        if sequence in {4, 7}:
            # Cancellation changes the durable summary even without a new event type.
            assert events == []
            assert receipt.event_ids == ()

    assert len(session_ids) == 1
    session_id = session_ids.pop()
    service.stop_capture(capture_id)
    database.dispose()

    with create_database(database.paths) as reopened:
        history = EchoHistoryService(reopened.session_factory).get_history(session_id)
        assert history.session.is_discarded is False
        assert history.session.current_state == "active"
        assert history.session.was_consumed is False
        assert history.session.observation_count == len(states)
        assert [event.event_type for event in history.events].count("echo_marked_discarded") == 2
        assert history.observations[0].raw_payload["is_discarded"] is False


def test_material_consumption_links_two_distinct_durable_sessions(
    storage: tuple[Database, EchoHistoryService],
) -> None:
    database, service = storage
    capture_id = _start(service)
    source = service.ingest_live_result(
        capture_id,
        _observation(level=15),
        source_sequence=1,
    )
    assert source.echo_session_id is not None

    material = _observation(local_session_id="echo-session-0002")
    material["page_type"] = "enhancement_material_selection"
    material["projected_level"] = 15
    material["events"] = [
        {
            "event_type": "new_echo_session_started",
            "session_id": "echo-session-0002",
            "related_session_id": None,
            "confidence": 0.94,
            "notes": ["material screen target"],
        }
    ]
    target = service.ingest_live_result(capture_id, material, source_sequence=2)
    assert target.echo_session_id is not None
    assert target.echo_session_id != source.echo_session_id

    confirmed = _observation(level=15, local_session_id="echo-session-0002")
    confirmed["events"] = [
        {
            "event_type": "echo_used_as_material",
            "session_id": "echo-session-0001",
            "related_session_id": "echo-session-0002",
            "confidence": 0.93,
            "notes": ["consumed_into=echo-session-0002"],
        },
        {
            "event_type": "echo_session_ended",
            "session_id": "echo-session-0001",
            "related_session_id": "echo-session-0002",
            "confidence": 0.93,
            "notes": ["reason=consumed"],
        },
    ]
    service.ingest_live_result(capture_id, confirmed, source_sequence=3)

    source_history = service.get_history(source.echo_session_id)
    target_history = service.get_history(target.echo_session_id)
    assert source_history.session.was_consumed is True
    assert source_history.session.current_state == "consumed_ended"
    assert source_history.session.consumed_into_session_id == target.echo_session_id
    assert source_history.session.ended_at is not None
    assert target_history.session.was_consumed is False
    with database.session_factory() as session:
        consumed_echo = session.get(EchoRecord, source_history.session.echo_id)
        assert consumed_echo is not None
        assert consumed_echo.final_status == "consumed"
        assert consumed_echo.archived_at is not None
        assert consumed_echo.character_id is None
        assert consumed_echo.slot_index is None
    assignment_service = CharacterCultivationService(database.session_factory)
    candidates = assignment_service.list_echoes_for_assignment(
        data_set_id=source_history.capture.data_set_id
    )
    assert source_history.session.echo_id not in {candidate.id for candidate in candidates}
    character = assignment_service.create_character(
        data_set_id=source_history.capture.data_set_id,
        command=CreateCharacter("素材防護測試角色"),
    )
    with database.session_factory.begin() as session:
        legacy_consumed = session.get(EchoRecord, source_history.session.echo_id)
        assert legacy_consumed is not None
        legacy_consumed.archived_at = None
    candidates = assignment_service.list_echoes_for_assignment(
        data_set_id=source_history.capture.data_set_id
    )
    assert source_history.session.echo_id not in {candidate.id for candidate in candidates}
    with pytest.raises(ValueError, match="consumed Echo"):
        assignment_service.assign_echo_slot(
            character.summary.id,
            1,
            source_history.session.echo_id or "",
            data_set_id=source_history.capture.data_set_id,
        )
    assert [item.event_type for item in source_history.events] == [
        "echo_used_as_material",
        "echo_session_ended",
    ]
    assert [item.event_type for item in target_history.events] == [
        "new_echo_session_started",
        "echo_used_as_material",
        "echo_session_ended",
    ]


def test_history_events_are_partitioned_by_echo_session_within_one_capture(
    storage: tuple[Database, EchoHistoryService],
) -> None:
    _database, service = storage
    capture_id = _start(service)
    first_payload = _observation(
        local_session_id="echo-session-0001",
        name="第一顆",
        events=[
            {
                "event_type": "page_changed",
                "session_id": None,
                "related_session_id": None,
                "confidence": 0.95,
                "notes": ["current_page_type=echo_selection"],
            },
            {
                "event_type": "echo_session_started",
                "session_id": "echo-session-0001",
                "related_session_id": None,
                "confidence": 0.95,
                "notes": [],
            },
        ],
    )
    first = service.ingest_live_result(capture_id, first_payload, source_sequence=1)
    second_payload = _observation(
        local_session_id="echo-session-0002",
        name="第二顆",
        events=[
            {
                "event_type": "page_changed",
                "session_id": None,
                "related_session_id": None,
                "confidence": 0.96,
                "notes": ["current_page_type=echo_selection"],
            },
            {
                "event_type": "new_echo_session_started",
                "session_id": "echo-session-0002",
                "related_session_id": None,
                "confidence": 0.96,
                "notes": [],
            },
            {
                "event_type": "echo_selected",
                "session_id": "echo-session-0002",
                "related_session_id": None,
                "confidence": 0.96,
                "notes": [],
            },
        ],
    )
    second = service.ingest_live_result(capture_id, second_payload, source_sequence=2)
    assert first.echo_session_id is not None
    assert second.echo_session_id is not None

    first_history = service.get_history(first.echo_session_id)
    second_history = service.get_history(second.echo_session_id)

    assert [event.event_type for event in first_history.events] == [
        "page_changed",
        "echo_session_started",
    ]
    assert [event.event_type for event in second_history.events] == [
        "page_changed",
        "new_echo_session_started",
        "echo_selected",
    ]


def test_corrections_preserve_raw_payload_update_effective_value_and_append_audit(
    storage: tuple[Database, EchoHistoryService],
) -> None:
    database, service = storage
    capture_id = _start(service)
    receipt = service.ingest_live_result(
        capture_id,
        _observation(confidence=0.55, name="不熄獵乎"),
        source_sequence=1,
    )
    assert receipt.observation_id is not None
    assert receipt.echo_session_id is not None

    name_audit = service.correct_observation(
        receipt.observation_id,
        "/name",
        "不熄獵手",
        reason="使用者確認名稱",
    )
    value_audit = service.correct_observation(
        receipt.observation_id,
        "/main_stats/0/value",
        4.5,
        mark_reviewed=True,
    )
    append_audit = service.correct_observation(
        receipt.observation_id,
        "/sub_stats/-",
        _stat("暴擊", 7.5, "%", slot=1),
    )

    history = service.get_history(receipt.echo_session_id)
    observation = history.observations[0]
    assert observation.raw_payload["name"] == "不熄獵乎"
    assert observation.effective_payload["name"] == "不熄獵手"
    assert observation.effective_payload["sub_stats"][0]["name"] == "暴擊"
    assert observation.requires_review is True
    assert observation.effective_payload["manual_confirmed_fields"] == ["/main_stats/0/value"]
    assert name_audit.old_value == "不熄獵乎"
    assert name_audit.new_value == "不熄獵手"
    assert value_audit.field_path == "/main_stats/0/value"
    assert append_audit.old_value is None
    assert len(history.corrections) == 3
    assert history.session.name == "不熄獵手"

    with database.session_factory() as session:
        record = session.get(EchoObservationRecord, receipt.observation_id)
        assert record is not None and record.payload_version == 4
        assert record.name == "不熄獵手"


def test_manual_confirmation_releases_only_inspected_low_confidence_field(storage) -> None:
    from wuwa_assistant.application.scoring_workflow import observed_stats_from_payload

    _, service = storage
    payload = _observation(level=5)
    payload["sub_stats"][0].update(confidence=0.4, value_confidence=0.4)
    receipt = service.ingest_live_result(_start(service), payload, source_sequence=1)
    service.correct_observation(
        receipt.observation_id, "/sub_stats/0/name", "暴擊", mark_reviewed=True
    )
    first = service.get_history(receipt.echo_session_id).observations[0]
    assert first.requires_review
    assert observed_stats_from_payload(first.effective_payload)[0].confidence == 0.4
    service.correct_observation(
        receipt.observation_id, "/sub_stats/0/value", 7.5, mark_reviewed=True
    )
    final = service.get_history(receipt.echo_session_id).observations[0]
    assert not final.requires_review
    assert observed_stats_from_payload(final.effective_payload)[0].confidence == 1.0
    assert final.raw_payload == first.raw_payload
    assert final.effective_payload["sub_stats"][0]["confidence"] == 0.4


def test_invalid_correction_rolls_back_without_changing_payload_or_audit(
    storage: tuple[Database, EchoHistoryService],
) -> None:
    database, service = storage
    capture_id = _start(service)
    receipt = service.ingest_live_result(capture_id, _observation(), source_sequence=1)
    assert receipt.observation_id is not None

    with pytest.raises(ValueError, match="nonnegative integer"):
        service.correct_observation(receipt.observation_id, "/level", -1)
    with pytest.raises(ValueError, match="not a supported"):
        service.correct_observation(receipt.observation_id, "/raw_ocr", {})

    with database.session_factory() as session:
        record = session.get(EchoObservationRecord, receipt.observation_id)
        assert record is not None
        assert record.effective_payload_json["level"] == 0
        assert record.payload_version == 1
        assert session.scalar(select(func.count(ObservationCorrectionRecord.id))) == 0


def test_uncertain_later_fields_do_not_erase_confirmed_echo_summary(
    storage: tuple[Database, EchoHistoryService],
) -> None:
    _database, service = storage
    capture_id = _start(service)
    first = service.ingest_live_result(capture_id, _observation(level=5), source_sequence=1)
    uncertain = _observation(level=5)
    uncertain["name"] = None
    uncertain["cost"] = None
    uncertain["main_stats"] = []
    uncertain["sub_stats"] = []
    service.ingest_live_result(capture_id, uncertain, source_sequence=2)

    assert first.echo_session_id is not None
    summary = service.get_history(first.echo_session_id).session
    assert summary.name == "不熄獵手"
    assert summary.cost == 1
    assert summary.level == 5


def test_disposition_links_echo_to_character_and_cross_dataset_update_rolls_back(
    storage: tuple[Database, EchoHistoryService],
) -> None:
    database, service = storage
    data_set_id = service.ensure_active_primary_dataset()
    capture_id = _start(service, data_set_id=data_set_id)
    receipt = service.ingest_live_result(capture_id, _observation(), source_sequence=1)
    assert receipt.echo_session_id is not None

    with database.session_factory.begin() as session:
        character = CharacterRecord(data_set_id=data_set_id, name="測試角色")
        other_data_set = DataSetRecord(name="示範資料", kind="demo", is_active=False)
        session.add_all([character, other_data_set])
        session.flush()
        other_character = CharacterRecord(data_set_id=other_data_set.id, name="示範角色")
        session.add(other_character)
        session.flush()
        character_id = character.id
        other_character_id = other_character.id

    equipped = service.set_disposition(
        receipt.echo_session_id,
        EchoDisposition.EQUIPPED,
        character_id=character_id,
        slot_index=1,
    )
    assert equipped.final_status == "equipped"
    assert equipped.character_id == character_id
    assert equipped.slot_index == 1

    with pytest.raises(ValueError, match="same data set"):
        service.set_disposition(
            receipt.echo_session_id,
            EchoDisposition.EQUIPPED,
            character_id=other_character_id,
            slot_index=2,
        )
    still_equipped = service.get_history(receipt.echo_session_id).session
    assert still_equipped.character_id == character_id
    assert still_equipped.slot_index == 1

    kept = service.set_disposition(receipt.echo_session_id, EchoDisposition.KEPT)
    assert kept.final_status == "equipped"
    assert kept.character_id == character_id and kept.slot_index == 1
    with database.session_factory() as session:
        echo = session.get(EchoRecord, kept.echo_id)
        assert echo is not None and echo.equipped_at is not None and echo.archived_at is None

    discarded = service.set_disposition(receipt.echo_session_id, EchoDisposition.DISCARDED)
    assert discarded.final_status == "discarded"
    assert discarded.character_id is None
    assert discarded.slot_index is None


def test_invalid_event_rolls_back_whole_ingest_and_closed_capture_rejects_writes(
    storage: tuple[Database, EchoHistoryService],
) -> None:
    database, service = storage
    capture_id = _start(service)
    invalid = _observation(
        events=[
            {
                "event_type": "invented_event",
                "session_id": "echo-session-0001",
                "notes": [],
            }
        ]
    )
    with pytest.raises(ValueError, match="unsupported echo event"):
        service.ingest_live_result(capture_id, invalid, source_sequence=1)

    with database.session_factory() as session:
        assert session.scalar(select(func.count(EchoSessionRecord.id))) == 0
        assert session.scalar(select(func.count(EchoObservationRecord.id))) == 0
        assert session.scalar(select(func.count(EchoEventRecord.id))) == 0

    service.stop_capture(capture_id)
    with pytest.raises(RuntimeError, match="not active"):
        service.ingest_live_result(capture_id, _observation(), source_sequence=2)


@pytest.mark.parametrize("key", ["frame", "pixels", "images", "screenshot", "roi_images"])
def test_ingest_rejects_image_or_full_frame_payloads_before_database_write(
    storage: tuple[Database, EchoHistoryService],
    key: str,
) -> None:
    database, service = storage
    capture_id = _start(service)
    payload = _observation()
    payload["raw_ocr"] = {"nested": {key: [[0, 1], [2, 3]]}}

    with pytest.raises(ValueError, match="image data"):
        service.ingest_live_result(capture_id, payload, source_sequence=1)

    with database.session_factory() as session:
        assert session.scalar(select(func.count(EchoObservationRecord.id))) == 0


def test_ingest_rejects_dense_pixel_matrix_under_generic_json_key(
    storage: tuple[Database, EchoHistoryService],
) -> None:
    database, service = storage
    capture_id = _start(service)
    payload = _observation()
    payload["raw_ocr"] = {"opaque_data": [[0] * 16 for _row in range(16)]}

    with pytest.raises(ValueError, match="image data"):
        service.ingest_live_result(capture_id, payload, source_sequence=1)

    with database.session_factory() as session:
        assert session.scalar(select(func.count(EchoObservationRecord.id))) == 0


def test_start_capture_validates_foreign_keys_without_partial_row(
    storage: tuple[Database, EchoHistoryService],
) -> None:
    database, service = storage
    with pytest.raises(LookupError, match="character not found"):
        service.start_capture(
            backend_kind="mss_monitor",
            monitor_index=1,
            sampling_hz=5.0,
            stable_wait_ms=600,
            app_version="0.4.0",
            assigned_character_id="missing-character",
        )

    with database.session_factory() as session:
        assert session.scalar(select(func.count(CaptureSessionRecord.id))) == 0

    primary_id = service.ensure_active_primary_dataset()
    with database.session_factory.begin() as session:
        demo = DataSetRecord(name="另一資料集", kind="demo", is_active=False)
        session.add(demo)
        session.flush()
        character = CharacterRecord(data_set_id=demo.id, name="跨資料集角色")
        session.add(character)
        session.flush()
        character_id = character.id

    with pytest.raises(ValueError, match="capture data set"):
        service.start_capture(
            data_set_id=primary_id,
            backend_kind="mss_monitor",
            monitor_index=1,
            sampling_hz=5.0,
            stable_wait_ms=600,
            app_version="0.4.0",
            assigned_character_id=character_id,
        )


def test_disposition_rejects_occupied_slot_and_replaces_only_when_explicit(
    storage: tuple[Database, EchoHistoryService],
) -> None:
    database, service = storage
    data_set_id = service.ensure_active_primary_dataset()
    capture_id = _start(service, data_set_id=data_set_id)
    first = service.ingest_live_result(capture_id, _observation(), source_sequence=1)
    second = service.ingest_live_result(
        capture_id,
        _observation(local_session_id="echo-session-0002", name="第二顆"),
        source_sequence=2,
    )
    assert first.echo_session_id is not None and second.echo_session_id is not None
    with database.session_factory.begin() as session:
        character = CharacterRecord(data_set_id=data_set_id, name="測試角色")
        session.add(character)
        session.flush()
        character_id = character.id
    service.set_disposition(
        first.echo_session_id,
        EchoDisposition.EQUIPPED,
        character_id=character_id,
        slot_index=1,
    )
    with pytest.raises(ValueError, match="already assigned"):
        service.set_disposition(
            second.echo_session_id,
            EchoDisposition.EQUIPPED,
            character_id=character_id,
            slot_index=1,
        )
    assert service.get_history(first.echo_session_id).session.character_id == character_id
    assert service.get_history(second.echo_session_id).session.character_id is None

    replacement = service.set_disposition(
        second.echo_session_id,
        EchoDisposition.EQUIPPED,
        character_id=character_id,
        slot_index=1,
        replace=True,
    )
    assert replacement.character_id == character_id and replacement.slot_index == 1
    previous = service.get_history(first.echo_session_id).session
    assert previous.final_status == "kept"
    assert previous.character_id is None and previous.slot_index is None
    with database.session_factory() as session:
        old_echo = session.get(EchoRecord, previous.echo_id)
        assert (
            old_echo is not None and old_echo.archived_at is None and old_echo.equipped_at is None
        )


@pytest.mark.parametrize("disposition", list(EchoDisposition))
def test_consumed_echo_cannot_be_kept_reassigned_or_discarded(
    storage: tuple[Database, EchoHistoryService],
    disposition: EchoDisposition,
) -> None:
    database, service = storage
    data_set_id = service.ensure_active_primary_dataset()
    capture_id = _start(service, data_set_id=data_set_id)
    payload = _observation()
    payload["current_session"]["was_consumed"] = True
    payload["current_session"]["current_state"] = "consumed_ended"
    receipt = service.ingest_live_result(capture_id, payload, source_sequence=1)
    assert receipt.echo_session_id is not None
    character = CharacterCultivationService(database.session_factory).create_character(
        data_set_id=data_set_id, command=CreateCharacter(name="角色")
    )
    before = service.get_history(receipt.echo_session_id).session
    with pytest.raises(ValueError, match="consumed"):
        service.set_disposition(
            receipt.echo_session_id,
            disposition,
            character_id=character.summary.id if disposition is EchoDisposition.EQUIPPED else None,
            slot_index=1 if disposition is EchoDisposition.EQUIPPED else None,
            replace=True,
        )
    assert service.get_history(receipt.echo_session_id).session == before
