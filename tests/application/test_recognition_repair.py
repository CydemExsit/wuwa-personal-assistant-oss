from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from wuwa_assistant.application.characters import CharacterCultivationService, CreateCharacter
from wuwa_assistant.application.recognition_repair import RecognitionRepairService
from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.storage.database import Database
from wuwa_assistant.storage.orm import (
    CaptureSessionRecord,
    CharacterRecord,
    CharacterSkillRecord,
    DataSetRecord,
    EchoObservationRecord,
    EchoRecord,
    EchoSessionRecord,
    ObservationCorrectionRecord,
    WeaponRecord,
    utc_now,
)


@pytest.fixture
def storage(tmp_path: Path) -> Iterator[tuple[Database, RecognitionRepairService, str, str]]:
    database = create_database(AppPaths.from_root(tmp_path / "repair"))
    with database.session_factory.begin() as session:
        data_set = DataSetRecord(name="主要", kind="primary", is_active=True)
        session.add(data_set)
        session.flush()
        capture = CaptureSessionRecord(
            data_set_id=data_set.id,
            backend_kind="fake",
            monitor_index=0,
            app_version="1.0.6",
            status="stopped",
            started_at=utc_now() - timedelta(minutes=5),
            stopped_at=utc_now() + timedelta(minutes=5),
        )
        session.add(capture)
        session.flush()
        ids = data_set.id, capture.id
    try:
        yield database, RecognitionRepairService(database.session_factory), *ids
    finally:
        database.dispose()


def _token(text: str, *, top: int = 40, confidence: float = 0.96) -> dict[str, Any]:
    return {"text": text, "confidence": confidence, "bbox": [5, top, 420, top + 24]}


def _payload(owner: str = "緋雪") -> dict[str, Any]:
    return {
        "page_type": "echo_selection",
        "name": "共鳴迴響·鳴式・虛造神型+25",
        "level": None,
        "cost": None,
        "is_equipped": True,
        "equipped_character_name": owner,
        "main_stats": [],
        "sub_stats": [],
        "confidence": {"page_type": 0.99, "equipped_character_name": 0.96},
        "raw_ocr": {
            "selection_detail": [_token("共鳴迴響·鳴式・虛造神型+25"), _token("COST-3", top=110)],
            "selection_equipped_owner": [_token(f"{owner}裝配中")],
            "selection_primary_action": [_token("卸下")],
        },
    }


def _seed(
    database: Database,
    data_set_id: str,
    capture_id: str,
    payload: dict[str, Any],
    *,
    echo_session_id: str | None = None,
) -> tuple[str, str, str]:
    with database.session_factory.begin() as session:
        if echo_session_id is None:
            echo = EchoRecord(
                data_set_id=data_set_id,
                name=payload.get("name"),
                level=payload.get("level"),
                cost=payload.get("cost"),
                score=75,
            )
            session.add(echo)
            session.flush()
            record = EchoSessionRecord(
                capture_session_id=capture_id,
                echo_id=echo.id,
                local_session_id=echo.id,
            )
            session.add(record)
            session.flush()
        else:
            record = session.get(EchoSessionRecord, echo_session_id)
            assert record is not None
            echo = session.get(EchoRecord, record.echo_id)
            assert echo is not None
        sequence = (
            session.scalar(
                select(func.count(EchoObservationRecord.id)).where(
                    EchoObservationRecord.echo_session_id == record.id
                )
            )
            or 0
        )
        observation = EchoObservationRecord(
            echo_session_id=record.id,
            sequence_no=sequence + 1,
            page_type="echo_selection",
            name=payload.get("name"),
            level=payload.get("level"),
            cost=payload.get("cost"),
            raw_payload_json=deepcopy(payload),
            effective_payload_json=deepcopy(payload),
            confidence_json=deepcopy(payload["confidence"]),
            requires_review=True,
        )
        session.add(observation)
        session.flush()
        return echo.id, record.id, observation.id


@pytest.mark.parametrize("low_stat", [False, True])
def test_missing_mixed_name_repair_preserves_raw_and_other_review_reasons(storage, low_stat):
    database, repair, data_set_id, capture_id = storage

    def stat(name, value, unit):
        return dict(name=name, value=value, unit=unit, confidence=0.99)

    payload = _payload()
    payload.update(
        name=None,
        level=5,
        cost=1,
        main_stats=[stat("攻擊", 5.4, "%"), stat("生命", 684, "")],
        sub_stats=[stat("暴擊", 8.1, "%")],
    )
    payload["raw_ocr"]["selection_detail"] = [_token("岩蛛S4型+5")]
    if low_stat:
        payload["sub_stats"][0]["confidence"] = 0.4
    echo_id, _, observation_id = _seed(database, data_set_id, capture_id, payload)
    result = repair.apply(capture_id, missing_names_only=True)
    assert (result.corrected_fields, result.archived_characters, result.registered_equipment) == (
        1,
        0,
        0,
    )
    with database.session_factory() as session:
        observation = session.get(EchoObservationRecord, observation_id)
        assert observation.raw_payload_json == payload
        assert observation.name == "岩蛛S4型"
        assert observation.requires_review == low_stat
        assert observation.effective_payload_json["sub_stats"] == payload["sub_stats"]
        assert session.get(EchoRecord, echo_id).name == "岩蛛S4型"
    assert repair.apply(capture_id, missing_names_only=True).corrected_fields == 0


def _duplicate_characters(database: Database, data_set_id: str) -> tuple[str, str]:
    service = CharacterCultivationService(database.session_factory)
    canonical = service.create_character(
        data_set_id=data_set_id, command=CreateCharacter(name="秧秧·玄翎")
    )
    duplicate = service.create_character(
        data_set_id=data_set_id, command=CreateCharacter(name="暫存別名")
    )
    with database.session_factory.begin() as session:
        record = session.get(CharacterRecord, duplicate.summary.id)
        assert record is not None
        record.name = "秧秧・玄翎"
    return canonical.summary.id, duplicate.summary.id


def test_preview_apply_preserve_raw_and_rows_and_are_idempotent(
    storage: tuple[Database, RecognitionRepairService, str, str],
) -> None:
    database, repair, data_set_id, capture_id = storage
    canonical, duplicate = _duplicate_characters(database, data_set_id)
    payload = _payload("秧秧・玄翎")
    echo_id, session_id, observation_id = _seed(database, data_set_id, capture_id, payload)
    plan = repair.preview(capture_id)
    assert {item.field for item in plan.field_repairs} == {"name", "level", "cost"}
    assert len(plan.character_archives) == len(plan.owner_registrations) == 1
    with database.session_factory() as session:
        assert session.get(CharacterRecord, duplicate).archived_at is None
        assert session.get(EchoRecord, echo_id).level is None
        assert session.scalar(select(func.count(ObservationCorrectionRecord.id))) == 0
    result = repair.apply(capture_id)
    assert (result.corrected_fields, result.archived_characters, result.registered_equipment) == (
        3,
        1,
        1,
    )
    with database.session_factory() as session:
        echo = session.get(EchoRecord, echo_id)
        observation = session.get(EchoObservationRecord, observation_id)
        assert echo is not None and observation is not None
        assert (echo.name, echo.level, echo.cost) == ("共鳴迴響·鳴式·虛造神型", 25, 3)
        assert echo.character_id == canonical and echo.slot_index is None and echo.set_name is None
        assert echo.score is None
        assert observation.raw_payload_json == payload
        assert observation.payload_version == 2 and observation.requires_review
        assert session.get(CharacterRecord, duplicate).archived_at is not None
        assert session.scalar(select(func.count(CharacterRecord.id))) == 2
        assert session.scalar(select(func.count(CharacterSkillRecord.id))) == 10
        assert session.scalar(select(func.count(EchoSessionRecord.id))) == 1
        audits = session.scalars(select(ObservationCorrectionRecord)).all()
        assert {item.actor for item in audits} == {"recognition_repair"}
        assert {item.field_path for item in audits} == {"/name", "/level", "/cost"}
        assert (
            session.get(EchoSessionRecord, session_id).metadata_json["equipment_assignment_source"]
            == "recognition"
        )
    repeated = repair.apply(capture_id)
    assert (
        repeated.corrected_fields,
        repeated.archived_characters,
        repeated.registered_equipment,
    ) == (0, 0, 0)


def test_repair_preserves_explicit_corrections_and_manual_assignment(
    storage: tuple[Database, RecognitionRepairService, str, str],
) -> None:
    database, repair, data_set_id, capture_id = storage
    echo_id, _session_id, observation_id = _seed(database, data_set_id, capture_id, _payload())
    character = CharacterCultivationService(database.session_factory).create_character(
        data_set_id=data_set_id, command=CreateCharacter(name="手動角色")
    )
    with database.session_factory.begin() as session:
        for field in ("name", "level", "cost"):
            session.add(
                ObservationCorrectionRecord(
                    observation_id=observation_id,
                    field_path=f"/{field}",
                    actor="user",
                    old_value_json=None,
                    new_value_json=None,
                )
            )
        echo = session.get(EchoRecord, echo_id)
        assert echo is not None
        echo.character_id = character.summary.id
        echo.slot_index = 2
        echo.final_status = "equipped"
    result = repair.apply(capture_id)
    assert result.corrected_fields == result.registered_equipment == 0
    with database.session_factory() as session:
        echo = session.get(EchoRecord, echo_id)
        assert (
            echo is not None and echo.character_id == character.summary.id and echo.slot_index == 2
        )
        assert echo.level is None and echo.cost is None
        assert session.scalar(select(func.count(ObservationCorrectionRecord.id))) == 3


@pytest.mark.parametrize("protected", ["notes", "level", "skill", "weapon", "echo", "older"])
def test_duplicate_character_with_personal_progress_or_ownership_is_never_archived(
    storage: tuple[Database, RecognitionRepairService, str, str],
    protected: str,
) -> None:
    database, repair, data_set_id, capture_id = storage
    _canonical, duplicate = _duplicate_characters(database, data_set_id)
    _seed(database, data_set_id, capture_id, _payload("秧秧・玄翎"))
    with database.session_factory.begin() as session:
        character = session.get(CharacterRecord, duplicate)
        assert character is not None
        if protected == "notes":
            character.notes = "個人備註"
        elif protected == "level":
            character.level = 90
        elif protected == "older":
            character.created_at = utc_now() - timedelta(days=1)
        elif protected == "skill":
            skill = session.scalar(
                select(CharacterSkillRecord).where(CharacterSkillRecord.character_id == duplicate)
            )
            assert skill is not None
            skill.current_level = 2
        elif protected == "weapon":
            session.add(
                WeaponRecord(data_set_id=data_set_id, character_id=duplicate, name="已填武器")
            )
        else:
            session.add(
                EchoRecord(data_set_id=data_set_id, character_id=duplicate, name="手動聲骸")
            )
    assert repair.preview(capture_id).character_archives == ()


@pytest.mark.parametrize("last_action, expected", [("儲存到剪貼簿的", 1), ("裝配", 0)])
def test_owner_recovery_skips_occluded_evidence_but_respects_explicit_unequip(
    storage: tuple[Database, RecognitionRepairService, str, str],
    last_action: str,
    expected: int,
) -> None:
    database, repair, data_set_id, capture_id = storage
    _echo_id, session_id, _observation_id = _seed(database, data_set_id, capture_id, _payload())
    occluded = _payload()
    occluded["is_equipped"] = False
    occluded["equipped_character_name"] = None
    occluded["raw_ocr"]["selection_primary_action"] = [_token(last_action)]
    occluded["raw_ocr"]["selection_equipped_owner"] = [_token("剪取與繪圖")]
    _seed(database, data_set_id, capture_id, occluded, echo_session_id=session_id)
    assert len(repair.preview(capture_id).owner_registrations) == expected


def test_failure_rolls_back_field_audits_and_duplicate_archive_together(
    storage: tuple[Database, RecognitionRepairService, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, repair, data_set_id, capture_id = storage
    _canonical, duplicate = _duplicate_characters(database, data_set_id)
    echo_id, _session_id, observation_id = _seed(
        database, data_set_id, capture_id, _payload("秧秧・玄翎")
    )

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("registration failed")

    monkeypatch.setattr(
        "wuwa_assistant.application.recognition_repair.register_recognized_equipment", fail
    )
    with pytest.raises(RuntimeError, match="registration failed"):
        repair.apply(capture_id)
    with database.session_factory() as session:
        assert session.get(EchoRecord, echo_id).level is None
        assert session.get(EchoObservationRecord, observation_id).payload_version == 1
        assert session.get(CharacterRecord, duplicate).archived_at is None
        assert session.scalar(select(func.count(ObservationCorrectionRecord.id))) == 0


def test_duplicate_echoes_are_reported_without_merging_or_removing_records(
    storage: tuple[Database, RecognitionRepairService, str, str],
) -> None:
    database, repair, data_set_id, capture_id = storage
    first = _seed(database, data_set_id, capture_id, _payload())
    second = _seed(database, data_set_id, capture_id, _payload())
    plan = repair.preview(capture_id)
    assert len(plan.possible_duplicate_sessions) == 1
    assert set(plan.possible_duplicate_sessions[0]) == {first[1], second[1]}
    repair.apply(capture_id)
    with database.session_factory() as session:
        assert session.scalar(select(func.count(EchoRecord.id))) == 2
        assert session.scalar(select(func.count(EchoSessionRecord.id))) == 2
        assert session.scalar(select(func.count(EchoObservationRecord.id))) == 2


def test_active_or_missing_capture_is_rejected(
    storage: tuple[Database, RecognitionRepairService, str, str],
) -> None:
    database, repair, _data_set_id, capture_id = storage
    with pytest.raises(ValueError, match="does not exist"):
        repair.preview("unknown")
    with database.session_factory.begin() as session:
        capture = session.get(CaptureSessionRecord, capture_id)
        assert capture is not None
        capture.status = "active"
    with pytest.raises(ValueError, match="stop capture"):
        repair.apply(capture_id)
