from __future__ import annotations

from collections.abc import Iterator
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
from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.storage.database import Database
from wuwa_assistant.storage.orm import CharacterRecord, DataSetRecord, EchoRecord


@pytest.fixture
def owner_history(tmp_path: Path) -> Iterator[tuple[Database, EchoHistoryService, str, str]]:
    database = create_database(AppPaths.from_root(tmp_path / "owner-history"))
    service = EchoHistoryService(database.session_factory)
    data_set_id = service.ensure_active_primary_dataset()
    capture_id = _start_capture(service, data_set_id)
    try:
        yield database, service, data_set_id, capture_id
    finally:
        database.dispose()


def _start_capture(service: EchoHistoryService, data_set_id: str) -> str:
    return service.start_capture(
        data_set_id=data_set_id,
        backend_kind="image_sequence",
        monitor_index=1,
        sampling_hz=5.0,
        stable_wait_ms=600,
        app_version="test",
    ).id


def _owner_payload(name: str | None = "愛彌斯") -> dict[str, Any]:
    return {
        "page_type": "echo_selection",
        "name": "不熄獵手",
        "level": 25,
        "cost": 1,
        "main_stats": [],
        "sub_stats": [],
        "confidence": {"page_type": 0.98, "is_equipped": 0.95, "equipped_character_name": 0.95},
        "is_equipped": True,
        "equipped_character_name": name,
        "semantic_fingerprint": "selection-owner",
        "current_session": {
            "local_session_id": "echo-session-0001",
            "current_state": "active",
            "is_discarded": False,
            "was_consumed": False,
            "latest_level": 25,
        },
        "events": [],
    }


@pytest.mark.parametrize("change", [None, "set", "value", "partial", "manual"])
def test_exact_maxed_rescan_can_reuse_confirmed_slot_but_never_guess_or_replace_manual(
    owner_history: tuple[Database, EchoHistoryService, str, str],
    change: str | None,
) -> None:
    database, service, _data_set_id, capture_id = owner_history
    payload = _owner_payload()
    payload["set_name"] = "羽落空塵之歌"
    payload["main_stats"] = [
        {"stat_kind": kind, "name": "生命", "value": value, "unit": unit, "confidence": 0.99}
        for kind, value, unit in [("primary", 22.8, "%"), ("fixed", 2280, "")]
    ]
    payload["sub_stats"] = [
        {"name": name, "value": value, "unit": "%", "confidence": 0.99, "slot_index": i}
        for i, (name, value) in enumerate(
            [("暴擊", 7.5), ("暴擊傷害", 15), ("共鳴效率", 10), ("生命", 7.9), ("攻擊", 7.9)]
        )
    ]
    payload["equipped_slot_index"] = 3
    payload["confidence"]["equipped_slot_index"] = 0.95
    first = service.ingest_live_result(capture_id, payload, source_sequence=1)
    assert first.echo_session_id
    first_history = service.get_history(first.echo_session_id)
    assert first_history.session.slot_index == 3
    if change == "manual":
        from wuwa_assistant.storage.orm import EchoSessionRecord

        with database.session_factory.begin() as session:
            row = session.get(EchoSessionRecord, first.echo_session_id)
            row.metadata_json = {"equipment_assignment_source": "manual"}
    payload["current_session"]["local_session_id"] = "echo-session-0002"
    payload["equipped_slot_index"] = None
    payload["confidence"]["equipped_slot_index"] = None
    if change == "set":
        payload["set_name"] = "輕雲出月"
    elif change == "value":
        payload["sub_stats"][0]["value"] = 6.9
    elif change == "partial":
        payload["sub_stats"].pop()
    second = service.ingest_live_result(capture_id, payload, source_sequence=2)
    assert second.echo_session_id
    second_history = service.get_history(second.echo_session_id)
    assert second_history.session.slot_index == (3 if change is None else None)


def test_recognized_owner_creates_roster_once_without_guessing_permanent_slot(
    owner_history: tuple[Database, EchoHistoryService, str, str],
) -> None:
    database, service, data_set_id, capture_id = owner_history
    first = service.ingest_live_result(capture_id, _owner_payload(), source_sequence=1)
    assert first.created_character_id is not None and first.echo_session_id is not None
    duplicate = service.ingest_live_result(capture_id, _owner_payload(), source_sequence=1)
    assert duplicate.duplicate and duplicate.created_character_id is None
    later = service.ingest_live_result(capture_id, _owner_payload(), source_sequence=2)
    assert later.created_character_id is None
    detail = CharacterCultivationService(database.session_factory).get_character(
        first.created_character_id, data_set_id=data_set_id
    )
    assert detail.summary.name == "愛彌斯"
    assert detail.summary.element is None and detail.summary.role is None
    assert len(detail.skills) == 5 and all(slot.echo_id is None for slot in detail.echo_slots)
    history = service.get_history(first.echo_session_id)
    assert history.session.character_id == first.created_character_id
    assert history.session.slot_index is None
    assert len(detail.unpositioned_echoes) == 1
    assert detail.unpositioned_echoes[0].id == history.session.echo_id
    assert history.observations[0].raw_payload["equipped_character_name"] == "愛彌斯"
    with database.session_factory() as session:
        assert session.scalar(select(func.count(CharacterRecord.id))) == 1


@pytest.mark.parametrize(
    ("page", "equipped", "name", "name_confidence", "threshold", "created"),
    [
        ("echo_selection", True, "愛彌斯", 0.90, 0.80, True),
        ("echo_selection", True, "愛彌斯", 0.89, 0.80, False),
        ("echo_selection", True, "愛彌斯", 0.94, 0.95, False),
        ("echo_selection", True, "愛彌斯", 0.95, 0.95, True),
        ("echo_selection", True, "愛彌斯", None, 0.80, False),
        ("echo_selection", False, "愛彌斯", 0.99, 0.80, False),
        ("echo_selection", None, "愛彌斯", 0.99, 0.80, False),
        ("echo_selection", True, None, 0.99, 0.80, False),
        ("echo_enhancement", True, "愛彌斯", 0.99, 0.80, False),
    ],
)
def test_owner_roster_registration_requires_selection_and_strong_consistent_evidence(
    owner_history: tuple[Database, EchoHistoryService, str, str],
    page: str,
    equipped: bool | None,
    name: str | None,
    name_confidence: float | None,
    threshold: float,
    created: bool,
) -> None:
    database, service, _data_set_id, capture_id = owner_history
    payload = _owner_payload(name)
    payload["page_type"] = page
    payload["is_equipped"] = equipped
    payload["confidence"]["equipped_character_name"] = name_confidence
    receipt = service.ingest_live_result(
        capture_id, payload, source_sequence=1, confidence_threshold=threshold
    )
    assert (receipt.created_character_id is not None) is created
    with database.session_factory() as session:
        assert session.scalar(select(func.count(CharacterRecord.id))) == int(created)


def test_owner_registration_reuses_normalized_names_and_respects_dataset_and_archive(
    owner_history: tuple[Database, EchoHistoryService, str, str],
) -> None:
    database, service, data_set_id, capture_id = owner_history
    characters = CharacterCultivationService(database.session_factory)
    existing = characters.create_character(
        data_set_id=data_set_id, command=CreateCharacter(name="Rover", level=90, notes="保留")
    )
    reused = service.ingest_live_result(capture_id, _owner_payload("  ROVER  "), source_sequence=1)
    assert reused.created_character_id is None
    characters.archive_character(existing.summary.id, data_set_id=data_set_id)
    archived = service.ingest_live_result(capture_id, _owner_payload("rover"), source_sequence=2)
    assert archived.created_character_id is None
    old = characters.get_character(existing.summary.id, data_set_id=data_set_id)
    assert old.summary.archived_at is not None and old.summary.level == 90 and old.notes == "保留"

    with database.session_factory.begin() as session:
        demo = DataSetRecord(name="示範資料", kind="demo", is_active=False)
        session.add(demo)
        session.flush()
        demo_id = demo.id
    demo_capture = _start_capture(service, demo_id)
    added = service.ingest_live_result(demo_capture, _owner_payload("Rover"), source_sequence=1)
    assert (
        added.created_character_id is not None and added.created_character_id != existing.summary.id
    )
    with database.session_factory() as session:
        assert session.scalar(select(func.count(CharacterRecord.id))) == 2
        assert session.get(CharacterRecord, added.created_character_id).data_set_id == demo_id


def test_recognized_owner_change_does_not_overwrite_explicit_permanent_assignment(
    owner_history: tuple[Database, EchoHistoryService, str, str],
) -> None:
    database, service, data_set_id, capture_id = owner_history
    first = service.ingest_live_result(capture_id, _owner_payload("角色一"), source_sequence=1)
    assert first.echo_session_id is not None and first.created_character_id is not None
    service.set_disposition(
        first.echo_session_id,
        EchoDisposition.EQUIPPED,
        character_id=first.created_character_id,
        slot_index=3,
    )
    changed = service.ingest_live_result(capture_id, _owner_payload("角色二"), source_sequence=2)
    assert changed.created_character_id is not None
    history = service.get_history(first.echo_session_id)
    assert history.session.character_id == first.created_character_id
    assert history.session.slot_index == 3 and history.session.final_status == "equipped"
    assert history.observations[-1].effective_payload["equipped_character_name"] == "角色二"
    second = CharacterCultivationService(database.session_factory).get_character(
        changed.created_character_id, data_set_id=data_set_id
    )
    assert all(slot.echo_id is None for slot in second.echo_slots)


def test_failed_ingest_rolls_back_owner_roster_creation(
    owner_history: tuple[Database, EchoHistoryService, str, str],
) -> None:
    database, service, _data_set_id, capture_id = owner_history
    payload = _owner_payload()
    payload["events"] = [{"event_type": "unsupported", "session_id": "echo-session-0001"}]
    with pytest.raises(ValueError, match="unsupported echo event"):
        service.ingest_live_result(capture_id, payload, source_sequence=1)
    with database.session_factory() as session:
        assert session.scalar(select(func.count(CharacterRecord.id))) == 0
        assert session.scalar(select(func.count(EchoRecord.id))) == 0


def test_equipped_tri_state_survives_live_ingest_and_manual_correction(tmp_path: Path) -> None:
    database = create_database(AppPaths.from_root(tmp_path / "equipped-history"))
    try:
        service = EchoHistoryService(database.session_factory)
        capture = service.start_capture(
            data_set_id=service.ensure_active_primary_dataset(),
            backend_kind="image_sequence",
            monitor_index=1,
            sampling_hz=5.0,
            stable_wait_ms=600,
            app_version="test",
            roi_config_version="tw-2560x1440-v1",
        )
        payload = {
            "page_type": "echo_selection",
            "name": "不熄獵手",
            "level": 25,
            "projected_level": None,
            "cost": 1,
            "main_stats": [],
            "projected_main_stats": [],
            "sub_stats": [],
            "locked_substat_slots": None,
            "confidence": {"page_type": 0.98, "is_equipped": 0.92},
            "source_timestamp": "2026-08-24T12:00:00+08:00",
            "source_file": None,
            "is_discarded": None,
            "is_equipped": True,
            "material_source_selected": None,
            "material_source_level": None,
            "raw_ocr": {},
            "debug_roi_paths": [],
            "debug_ocr_path": None,
            "semantic_fingerprint": "selection-equipped",
            "current_session": {
                "local_session_id": "echo-session-0001",
                "current_state": "active",
                "is_discarded": False,
                "was_consumed": False,
                "consumed_into_session_id": None,
                "latest_level": 25,
            },
            "events": [],
        }

        receipt = service.ingest_live_result(capture.id, payload, source_sequence=1)

        assert receipt.echo_session_id is not None
        assert receipt.observation_id is not None
        observation = service.get_history(receipt.echo_session_id).observations[0]
        assert observation.raw_payload["is_equipped"] is True
        assert observation.effective_payload["is_equipped"] is True

        audit = service.correct_observation(
            receipt.observation_id,
            "/is_equipped",
            False,
            reason="使用者確認清單徽章",
        )
        corrected = service.get_history(receipt.echo_session_id).observations[0]
        assert audit.old_value is True
        assert audit.new_value is False
        assert corrected.raw_payload["is_equipped"] is True
        assert corrected.effective_payload["is_equipped"] is False
    finally:
        database.dispose()


def test_scanning_five_equipped_echoes_populates_character_slots_without_manual_save(owner_history):
    database, service, data_set_id, capture_id = owner_history
    character_id = None
    for slot in range(1, 6):
        payload = _owner_payload("秧秧・玄翎" if slot == 1 else "秧秧·玄翎")
        payload["current_session"]["local_session_id"] = f"echo-session-{slot:04}"
        payload["name"] = f"測試聲骸{slot}"
        payload["set_name"] = "雪落無聲之願"
        payload["equipped_slot_index"] = slot
        payload["confidence"]["equipped_slot_index"] = 0.99
        receipt = service.ingest_live_result(capture_id, payload, source_sequence=slot)
        assert receipt.equipment_changed
        character_id = character_id or receipt.created_character_id
    detail = CharacterCultivationService(database.session_factory).get_character(
        character_id, data_set_id=data_set_id
    )
    assert detail.summary.name == "秧秧·玄翎"
    assert [slot.name for slot in detail.echo_slots] == [f"測試聲骸{slot}" for slot in range(1, 6)]
    assert all(slot.set_name == "雪落無聲之願" for slot in detail.echo_slots)
    assert detail.unpositioned_echoes == ()
    assert (
        len(
            CharacterCultivationService(database.session_factory).list_characters(
                data_set_id=data_set_id
            )
        )
        == 1
    )


def test_weak_slot_stays_visible_under_owner_then_strong_slot_promotes_it(owner_history):
    database, service, data_set_id, capture_id = owner_history
    payload = _owner_payload()
    payload["equipped_slot_index"] = 3
    payload["confidence"]["equipped_slot_index"] = 0.89
    first = service.ingest_live_result(capture_id, payload, source_sequence=1)
    characters = CharacterCultivationService(database.session_factory)
    detail = characters.get_character(first.created_character_id, data_set_id=data_set_id)
    assert len(detail.unpositioned_echoes) == 1
    pending_echo_id = detail.unpositioned_echoes[0].id
    assert all(slot.echo_id is None for slot in detail.echo_slots)
    payload["confidence"]["equipped_slot_index"] = 0.99
    promoted = service.ingest_live_result(capture_id, payload, source_sequence=2)
    assert promoted.equipment_changed
    detail = characters.get_character(first.created_character_id, data_set_id=data_set_id)
    assert detail.echo_slots[2].echo_id == pending_echo_id
    assert detail.unpositioned_echoes == ()
    payload["equipped_slot_index"] = None
    later = service.ingest_live_result(capture_id, payload, source_sequence=3)
    assert not later.equipment_changed
    assert service.get_history(first.echo_session_id).session.slot_index == 3


@pytest.mark.parametrize(
    "owner_confidence,slot_confidence,threshold,synchronized",
    [
        (0.99, 0.99, 0.85, True),
        (0.90, 0.90, 0.85, True),
        (0.89, 0.99, 0.85, False),
        (None, 0.99, 0.85, False),
        (0.99, 0.89, 0.85, False),
        (0.99, None, 0.85, False),
        (0.95, 0.99, 0.98, False),
        (0.99, 0.95, 0.98, False),
    ],
)
def test_live_equipment_sync_requires_strong_owner_and_explicit_slot(
    owner_history, owner_confidence, slot_confidence, threshold, synchronized
):
    database, service, data_set_id, capture_id = owner_history
    first = service.ingest_live_result(capture_id, _owner_payload(), source_sequence=1)
    service.set_disposition(
        first.echo_session_id,
        EchoDisposition.EQUIPPED,
        character_id=first.created_character_id,
        slot_index=1,
    )
    payload = _owner_payload()
    payload["current_session"]["local_session_id"] = "echo-session-0002"
    payload["equipped_slot_index"] = 1
    payload["confidence"]["equipped_character_name"] = owner_confidence
    payload["confidence"]["equipped_slot_index"] = slot_confidence
    second = service.ingest_live_result(
        capture_id, payload, source_sequence=2, confidence_threshold=threshold
    )
    old_id = service.get_history(first.echo_session_id).session.echo_id
    new_id = service.get_history(second.echo_session_id).session.echo_id
    detail = CharacterCultivationService(database.session_factory).get_character(
        first.created_character_id, data_set_id=data_set_id
    )
    assert detail.echo_slots[0].echo_id == (new_id if synchronized else old_id)
    if synchronized:
        assert second.equipment_changed
        assert detail.unpositioned_echoes == ()
        repeated = service.ingest_live_result(capture_id, payload, source_sequence=3)
        assert not repeated.equipment_changed
        database.engine.dispose()
        assert service.get_history(second.echo_session_id).session.slot_index == 1
        displaced = service.get_history(first.echo_session_id).session
        assert displaced.slot_index is None and displaced.character_id is None
        assert displaced.final_status == "kept"
    elif owner_confidence is not None and owner_confidence >= max(0.90, threshold):
        assert detail.unpositioned_echoes[0].id == new_id
    # Explicit saving remains available when live evidence cannot synchronize.
    service.set_disposition(
        second.echo_session_id,
        EchoDisposition.EQUIPPED,
        character_id=first.created_character_id,
        slot_index=1,
        replace=True,
    )
    database.engine.dispose()
    saved = service.get_history(second.echo_session_id).session
    old = service.get_history(first.echo_session_id).session
    assert saved.slot_index == 1 and saved.character_id == first.created_character_id
    assert old.slot_index is None and old.character_id is None
    with database.session_factory() as session:
        assert session.get(EchoRecord, old.echo_id).archived_at is None


@pytest.mark.parametrize("explicit_slot", [False, True])
def test_live_owner_change_moves_manual_assignment_only_with_explicit_slot(
    owner_history, explicit_slot
):
    database, service, data_set_id, capture_id = owner_history
    first = service.ingest_live_result(capture_id, _owner_payload(), source_sequence=1)
    service.set_disposition(
        first.echo_session_id,
        EchoDisposition.EQUIPPED,
        character_id=first.created_character_id,
        slot_index=1,
    )
    payload = _owner_payload("緋雪")
    if explicit_slot:
        payload["equipped_slot_index"] = 5
        payload["confidence"]["equipped_slot_index"] = 0.99
    receipt = service.ingest_live_result(capture_id, payload, source_sequence=2)
    old_owner = CharacterCultivationService(database.session_factory).get_character(
        first.created_character_id, data_set_id=data_set_id
    )
    database.engine.dispose()
    saved = service.get_history(first.echo_session_id).session
    if explicit_slot:
        assert receipt.equipment_changed
        assert saved.character_id == receipt.created_character_id and saved.slot_index == 5
        assert old_owner.echo_slots[0].echo_id is None
    else:
        assert not receipt.equipment_changed
        assert saved.character_id == first.created_character_id and saved.slot_index == 1


def test_historical_registration_with_explicit_slot_preserves_manual_choices(owner_history):
    from wuwa_assistant.application.recognized_equipment import register_recognized_equipment
    from wuwa_assistant.storage.orm import EchoSessionRecord

    database, service, data_set_id, capture_id = owner_history
    first = service.ingest_live_result(capture_id, _owner_payload(), source_sequence=1)
    service.set_disposition(
        first.echo_session_id,
        EchoDisposition.EQUIPPED,
        character_id=first.created_character_id,
        slot_index=1,
    )
    payload = _owner_payload()
    payload["current_session"]["local_session_id"] = "echo-session-0002"
    second = service.ingest_live_result(capture_id, payload, source_sequence=2)
    with database.session_factory.begin() as session:
        # Historical evidence must not replace a manually occupied slot.
        register_recognized_equipment(
            session,
            session.get(EchoSessionRecord, second.echo_session_id),
            data_set_id=data_set_id,
            name="愛彌斯",
            slot_index=1,
        )
        # Nor move an existing manual assignment to another position.
        register_recognized_equipment(
            session,
            session.get(EchoSessionRecord, first.echo_session_id),
            data_set_id=data_set_id,
            name="愛彌斯",
            slot_index=5,
        )
    assert service.get_history(first.echo_session_id).session.slot_index == 1
    assert service.get_history(second.echo_session_id).session.slot_index is None


def test_newer_explicit_equipment_replaces_only_prior_automatic_assignment(owner_history):
    database, service, _data_set_id, capture_id = owner_history
    payload = _owner_payload()
    payload["equipped_slot_index"] = 1
    payload["confidence"]["equipped_slot_index"] = 0.99
    first = service.ingest_live_result(capture_id, payload, source_sequence=1)
    payload["current_session"]["local_session_id"] = "echo-session-0002"
    second = service.ingest_live_result(capture_id, payload, source_sequence=2)
    old = service.get_history(first.echo_session_id).session
    new = service.get_history(second.echo_session_id).session
    assert old.character_id is None and old.slot_index is None
    assert new.character_id == first.created_character_id and new.slot_index == 1
    with database.session_factory() as session:
        assert session.get(EchoRecord, old.echo_id).archived_at is None


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("action", ["裝配", "", "儲存到剪貼簿的"])
def test_unequip_requires_explicit_action_and_never_erases_manual_assignment(
    owner_history, manual, action
):
    _database, service, _data_set_id, capture_id = owner_history
    payload = _owner_payload()
    payload["equipped_slot_index"] = 2
    payload["confidence"]["equipped_slot_index"] = 0.99
    first = service.ingest_live_result(capture_id, payload, source_sequence=1)
    if manual:
        service.set_disposition(
            first.echo_session_id,
            EchoDisposition.EQUIPPED,
            character_id=first.created_character_id,
            slot_index=None,
        )
    payload["is_equipped"] = False
    payload["equipped_character_name"] = None
    payload["equipped_slot_index"] = None
    payload["raw_ocr"] = {"selection_primary_action": [{"text": action, "confidence": 0.99}]}
    result = service.ingest_live_result(capture_id, payload, source_sequence=2)
    should_clear = action == "裝配" and not manual
    assert result.equipment_changed is should_clear
    summary = service.get_history(first.echo_session_id).session
    assert (summary.character_id is None) is should_clear
