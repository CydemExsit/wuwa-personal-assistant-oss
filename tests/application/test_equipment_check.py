from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from wuwa_assistant.application.characters import CharacterCultivationService, UpdateCharacter
from wuwa_assistant.application.echo_history import EchoHistoryService
from wuwa_assistant.application.equipment_check import equipment_check, equipment_signature
from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.storage.orm import EchoRecord


def check_payload(slot=1):
    return {
        "page_type": "echo_selection",
        "name": f"測試種類{slot}",
        "set_name": "輕雲出月",
        "level": 0,
        "cost": 1,
        "main_stats": [
            {"name": "生命", "value": 4.5, "unit": "%", "confidence": 0.99},
            {"name": "生命", "value": 456, "unit": "", "confidence": 0.99},
        ],
        "sub_stats": [],
        "is_equipped": True,
        "equipped_character_name": "珂萊塔",
        "equipped_slot_index": slot,
        "confidence": {
            "page_type": 0.99,
            "equipped_character_name": 0.99,
            "equipped_slot_index": 0.99,
            "is_equipped": 0.99,
        },
        "current_session": {
            "local_session_id": f"session-{slot}",
            "current_state": "active",
            "latest_level": 0,
            "is_discarded": False,
            "was_consumed": False,
        },
        "events": [],
    }


@pytest.fixture
def check_workspace(tmp_path):
    db = create_database(AppPaths.from_root(tmp_path / "sync"))
    history = EchoHistoryService(db.session_factory)
    dataset = history.ensure_active_primary_dataset()
    capture = history.start_capture(
        data_set_id=dataset,
        backend_kind="image_sequence",
        monitor_index=1,
        sampling_hz=5,
        stable_wait_ms=600,
        app_version="test",
    ).id
    try:
        yield db, history, CharacterCultivationService(db.session_factory), dataset, capture
    finally:
        db.dispose()


def test_five_explicit_reads_reset_and_stale_gear(check_workspace):
    db, history, characters, dataset, capture = check_workspace
    for slot in range(1, 6):
        receipt = history.ingest_live_result(capture, check_payload(slot), source_sequence=slot)
        assert receipt.equipment_check_changed
    owner = characters.list_characters(data_set_id=dataset)[0].id
    detail = characters.get_character(owner, data_set_id=dataset)
    assert equipment_check(detail).matched == 5
    duplicate = history.ingest_live_result(capture, check_payload(5), source_sequence=5)
    assert duplicate.duplicate and not duplicate.equipment_check_changed
    stable = history.ingest_live_result(capture, check_payload(5), source_sequence=6)
    assert not stable.equipment_check_changed and not stable.panel_inputs_changed
    with db.session_factory.begin() as session:
        echo = session.get(EchoRecord, detail.echo_slots[0].echo_id)
        echo.main_stats_json = [
            {**s, "value": 500} if s["unit"] == "" else s for s in echo.main_stats_json
        ]
    stale = equipment_check(characters.get_character(owner, data_set_id=dataset))
    assert stale.matched == 4 and stale.labels[0] == "1 待重讀"
    characters.reset_equipment_check(owner, data_set_id=dataset)
    reset = characters.get_character(owner, data_set_id=dataset)
    assert equipment_check(reset).matched == 0
    assert reset.echo_slots == characters.get_character(owner, data_set_id=dataset).echo_slots


@pytest.mark.parametrize("change", ["unknown_slot", "weak_slot", "weak_stat", "partial"])
def test_uncertain_read_never_counts_as_matched(check_workspace, change):
    _, history, characters, dataset, capture = check_workspace
    payload = check_payload()
    if change == "unknown_slot":
        payload["equipped_slot_index"] = None
    if change == "weak_slot":
        payload["confidence"]["equipped_slot_index"] = 0.89
    if change == "weak_stat":
        payload["main_stats"][0]["confidence"] = 0.84
    if change == "partial":
        payload["main_stats"].pop()
    history.ingest_live_result(capture, payload, source_sequence=1)
    owner = characters.list_characters(data_set_id=dataset)[0].id
    assert equipment_check(characters.get_character(owner, data_set_id=dataset)).matched == 0


def test_live_sync_matches_current_gear_and_later_manual_change_is_visible(check_workspace):
    db, history, characters, dataset, capture = check_workspace
    history.ingest_live_result(capture, check_payload(), source_sequence=1)
    owner = characters.list_characters(data_set_id=dataset)[0].id
    with db.session_factory.begin() as session:
        echo = EchoRecord(
            data_set_id=dataset,
            name="手動保留",
            cost=1,
            level=0,
            set_name="輕雲出月",
            main_stats_json=check_payload()["main_stats"],
        )
        session.add(echo)
        session.flush()
        manual_id = echo.id
    characters.assign_echo_slot(owner, 1, manual_id, data_set_id=dataset, replace=True)
    payload = check_payload()
    payload["current_session"]["local_session_id"] = "other"
    receipt = history.ingest_live_result(capture, payload, source_sequence=2)
    assert receipt.equipment_check_changed and receipt.equipment_changed
    detail = characters.get_character(owner, data_set_id=dataset)
    assert (
        detail.echo_slots[0].echo_id == history.get_history(receipt.echo_session_id).session.echo_id
    )
    assert equipment_check(detail).matched == 1
    with db.session_factory() as session:
        displaced = session.get(EchoRecord, manual_id)
        assert displaced.character_id is None and displaced.slot_index is None
        assert displaced.archived_at is None and displaced.final_status == "kept"
    # A subsequent explicit edit still takes effect and exposes its difference
    # from the last screen read, until fresh live evidence arrives.
    characters.assign_echo_slot(owner, 1, manual_id, data_set_id=dataset, replace=True)
    detail = characters.get_character(owner, data_set_id=dataset)
    assert detail.echo_slots[0].echo_id == manual_id
    check = equipment_check(detail)
    assert check.matched == 0 and check.labels[0] == "1 待重讀"
    assert "手動保留" in check.details and "測試種類1" in check.details


def test_same_echo_stat_update_notifies_and_stale_draft_cannot_erase_evidence(check_workspace):
    db, history, characters, dataset, capture = check_workspace
    first = history.ingest_live_result(capture, check_payload(), source_sequence=1)
    owner = characters.list_characters(data_set_id=dataset)[0].id
    old = characters.get_character(owner, data_set_id=dataset)
    payload = check_payload()
    payload["main_stats"][1]["value"] = 500
    later = history.ingest_live_result(capture, payload, source_sequence=2)
    assert later.echo_session_id == first.echo_session_id
    assert not later.equipment_changed and later.panel_inputs_changed
    goal = deepcopy(old.cultivation_goal)
    goal.pop("equipment_check")
    characters.update_character(
        owner,
        data_set_id=dataset,
        command=UpdateCharacter(
            name="珂萊塔",
            element=None,
            role=None,
            is_candidate=True,
            priority=100,
            level=1,
            resonance_chain=0,
            scoring_profile_id=None,
            notes=None,
            cultivation_goal=goal,
        ),
    )
    saved = characters.get_character(owner, data_set_id=dataset)
    assert equipment_check(saved).matched == 1
    assert saved.echo_slots[0].main_stats[1]["value"] == 500
    with db.session_factory() as session:
        assert len(session.scalars(select(EchoRecord)).all()) == 1


def test_old_in_flight_read_cannot_complete_new_check(check_workspace):
    _, history, characters, dataset, capture = check_workspace
    history.ingest_live_result(capture, check_payload(), source_sequence=1)
    owner = characters.list_characters(data_set_id=dataset)[0].id
    old = datetime.now(UTC) - timedelta(minutes=1)
    characters.reset_equipment_check(owner, data_set_id=dataset)
    history.ingest_live_result(capture, check_payload(2), source_sequence=2, occurred_at=old)
    assert equipment_check(characters.get_character(owner, data_set_id=dataset)).matched == 0


def test_signature_ignores_order_and_confidence_jitter_but_not_stats():
    payload = check_payload()
    changed = deepcopy(payload)
    changed["main_stats"].reverse()
    changed["main_stats"][0]["confidence"] = 0.97
    assert equipment_signature(changed) == equipment_signature(payload)
    changed["main_stats"][0]["value"] += 1
    assert equipment_signature(changed) != equipment_signature(payload)


def test_new_capture_does_not_reuse_previous_five_slot_proof(check_workspace):
    _, history, characters, dataset, capture = check_workspace
    for slot in range(1, 6):
        history.ingest_live_result(capture, check_payload(slot), source_sequence=slot)
    owner = characters.list_characters(data_set_id=dataset)[0].id
    history.stop_capture(capture)
    new_capture = history.start_capture(
        data_set_id=dataset,
        backend_kind="image_sequence",
        monitor_index=1,
        sampling_hz=5,
        stable_wait_ms=600,
        app_version="test",
    ).id
    history.ingest_live_result(new_capture, check_payload(3), source_sequence=1)
    check = equipment_check(characters.get_character(owner, data_set_id=dataset))
    assert check.matched == 1
    assert check.labels[0] == "1 未讀取" and check.labels[2] == "3 詞條相符"
