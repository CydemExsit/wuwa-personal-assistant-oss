from __future__ import annotations

import json
from collections.abc import Iterator
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from wuwa_assistant.application.echo_duplicates import (
    EchoDuplicateService,
    complete_echo_signature,
    group_echo_records,
    reconcile_echo_duplicates,
)
from wuwa_assistant.application.echo_history import EchoHistoryService
from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.storage.database import Database
from wuwa_assistant.storage.orm import (
    CaptureSessionRecord,
    CharacterRecord,
    DataSetRecord,
    EchoObservationRecord,
    EchoRecord,
    EchoSessionRecord,
)

_START = datetime(2026, 9, 21, tzinfo=UTC)
_MAIN: list[dict[str, Any]] = [
    {"stat_kind": "primary", "name": "暴擊", "value": 22.0, "unit": "%"},
    {"stat_kind": "fixed", "name": "攻擊", "value": 150, "unit": None},
]
_SUBS: list[dict[str, Any]] = [
    {"name": "生命", "value": 470, "unit": None},
    {"name": "攻擊", "value": 40, "unit": None},
    {"name": "暴擊", "value": 8.1, "unit": "%"},
    {"name": "暴擊傷害", "value": 15.0, "unit": "%"},
    {"name": "共鳴效率", "value": 10.8, "unit": "%"},
]


@pytest.fixture
def duplicate_workspace(tmp_path: Path) -> Iterator[tuple[Database, str, str, tuple[str, str]]]:
    database = create_database(AppPaths.from_root(tmp_path / "duplicates"))
    with database.session_factory.begin() as session:
        dataset = DataSetRecord(name="主要資料", kind="primary", is_active=True)
        session.add(dataset)
        session.flush()
        capture = CaptureSessionRecord(
            data_set_id=dataset.id,
            backend_kind="test",
            monitor_index=1,
            sampling_hz=5.0,
            stable_wait_ms=600,
            app_version="test",
            status="stopped",
        )
        characters = [
            CharacterRecord(data_set_id=dataset.id, name=name) for name in ("愛彌斯", "緋雪")
        ]
        session.add_all([capture, *characters])
        session.flush()
        workspace = database, dataset.id, capture.id, (characters[0].id, characters[1].id)
    try:
        yield workspace
    finally:
        database.dispose()


def _add_echo(
    workspace: tuple[Database, str, str, tuple[str, str]],
    number: int,
    *,
    name: str = "異相·辛吉勒姆",
    level: int = 25,
    cost: int = 4,
    set_name: str | None = None,
    main: list[dict[str, Any]] | None = None,
    subs: list[dict[str, Any]] | None = None,
    character_id: str | None = None,
    slot: int | None = None,
    manual: bool = False,
    strong_slot: bool = True,
    consumed: bool = False,
    archived: bool = False,
) -> tuple[str, str]:
    database, data_set_id, capture_id, _characters = workspace
    with database.session_factory.begin() as session:
        record = EchoRecord(
            data_set_id=data_set_id,
            name=name,
            level=level,
            cost=cost,
            set_name=set_name,
            main_stats_json=deepcopy(_MAIN if main is None else main),
            sub_stats_json=deepcopy(_SUBS if subs is None else subs),
            character_id=character_id,
            slot_index=slot,
            final_status="equipped" if character_id else "kept",
            archived_at=_START if archived else None,
            created_at=_START + timedelta(seconds=number),
        )
        session.add(record)
        session.flush()
        tracked = EchoSessionRecord(
            capture_session_id=capture_id,
            local_session_id=f"echo-session-{number:04}",
            echo_id=record.id,
            was_consumed=consumed,
            current_state="consumed_ended" if consumed else "active",
            started_at=_START + timedelta(seconds=number),
            metadata_json={
                "personal_note": "preserve",
                "equipment_assignment_source": "manual" if manual else "recognition",
            },
        )
        session.add(tracked)
        session.flush()
        character = session.get(CharacterRecord, character_id) if character_id else None
        payload = {
            "page_type": "echo_selection",
            "name": name,
            "level": level,
            "cost": cost,
            "main_stats": record.main_stats_json,
            "sub_stats": record.sub_stats_json,
            "is_equipped": character_id is not None,
            "equipped_character_name": character.name if character else None,
            "equipped_slot_index": slot,
            "confidence": {
                "equipped_character_name": 0.99,
                "equipped_slot_index": 0.99 if strong_slot else 0.70,
            },
            "raw_ocr": {"fixture": "retained"},
        }
        session.add(
            EchoObservationRecord(
                echo_session_id=tracked.id,
                sequence_no=1,
                page_type="echo_selection",
                raw_payload_json=deepcopy(payload),
                effective_payload_json=deepcopy(payload),
                observed_at=_START + timedelta(seconds=number),
            )
        )
        return record.id, tracked.id


def _snapshot(database: Database) -> str:
    contents = {}
    with database.session_factory() as session:
        for model in (EchoRecord, EchoSessionRecord, EchoObservationRecord, CaptureSessionRecord):
            rows = [
                dict(row)
                for row in session.execute(select(model.__table__).order_by(model.id)).mappings()
            ]
            for row in rows:
                if model is EchoSessionRecord:
                    row["metadata_json"] = {
                        key: value
                        for key, value in row["metadata_json"].items()
                        if key != "echo_duplicate_group_v1"
                    }
            contents[model.__tablename__] = rows
    return json.dumps(contents, sort_keys=True, ensure_ascii=False, default=str)


def test_exact_groups_preserve_raw_rows_and_are_reversible_and_idempotent(
    duplicate_workspace,
) -> None:
    database, dataset, _capture, _characters = duplicate_workspace
    first, _first_session = _add_echo(duplicate_workspace, 1, name="異相・辛吉勒姆")
    second, _second_session = _add_echo(
        duplicate_workspace, 2, main=list(reversed(_MAIN)), subs=list(reversed(_SUBS))
    )
    before = _snapshot(database)
    service = EchoDuplicateService(database.session_factory)
    preview = service.preview(dataset)
    assert preview.physical_count == 2 and preview.logical_count == 1 and preview.rescan_count == 1
    assert preview.groups[0].member_echo_ids == tuple(sorted((first, second)))
    assert preview.groups[0].representative_echo_id == second
    result = service.apply(dataset)
    assert result.updated_session_count == 2
    assert _snapshot(database) == before
    assert service.apply(dataset).updated_session_count == 0
    assert service.clear(dataset) == 2
    assert service.clear(dataset) == 0
    assert _snapshot(database) == before


def test_unknown_legacy_set_joins_only_one_compatible_known_set(duplicate_workspace) -> None:
    database, dataset, _capture, _characters = duplicate_workspace
    unknown, _ = _add_echo(duplicate_workspace, 1)
    known, _ = _add_echo(duplicate_workspace, 2, set_name="長路啟航之星")
    service = EchoDuplicateService(database.session_factory)
    assert service.apply(dataset).preview.logical_count == 1
    other, _ = _add_echo(duplicate_workspace, 3, set_name="雪落無聲之願")
    changed = service.apply(dataset)
    assert changed.preview.logical_count == 3 and changed.updated_session_count == 2
    assert {group.representative_echo_id for group in changed.preview.groups} == {
        unknown,
        known,
        other,
    }
    legacy = next(
        group for group in changed.preview.groups if group.representative_echo_id == unknown
    )
    assert legacy.reason == "ambiguous_legacy_set"


@pytest.mark.parametrize("change", ["main", "sub", "unit", "name", "cost"])
def test_any_exact_signature_difference_keeps_records_distinct(
    duplicate_workspace, change: str
) -> None:
    database, dataset, _capture, _characters = duplicate_workspace
    _add_echo(duplicate_workspace, 1)
    main, subs, name, cost = deepcopy(_MAIN), deepcopy(_SUBS), "異相·辛吉勒姆", 4
    if change == "main":
        main[0]["value"] = 22.0001
    elif change == "sub":
        subs[0]["value"] = 471
    elif change == "unit":
        subs[0]["unit"] = "%"
    elif change == "name":
        name = "辛吉勒姆"
    else:
        cost = 3
    _add_echo(duplicate_workspace, 2, main=main, subs=subs, name=name, cost=cost)
    assert EchoDuplicateService(database.session_factory).preview(dataset).logical_count == 2


def test_incomplete_unrolled_and_consumed_records_never_merge(duplicate_workspace) -> None:
    database, dataset, _capture, _characters = duplicate_workspace
    for number in (1, 2):
        _add_echo(duplicate_workspace, number, subs=_SUBS[:3])
    for number in (3, 4):
        _add_echo(duplicate_workspace, number, level=0, subs=[])
    _add_echo(duplicate_workspace, 5)
    consumed, _ = _add_echo(duplicate_workspace, 6, consumed=True)
    archived, _ = _add_echo(duplicate_workspace, 7, archived=True)
    service = EchoDuplicateService(database.session_factory)
    preview = service.preview(dataset)
    assert preview.physical_count == preview.logical_count == 5
    assert set(preview.excluded_echo_ids) == {consumed, archived}
    before = _snapshot(database)
    assert service.apply(dataset).updated_session_count == 0
    assert _snapshot(database) == before


def test_current_proven_slot_outranks_newer_owner_only_rescan(duplicate_workspace) -> None:
    database, dataset, _capture, characters = duplicate_workspace
    slotted, _ = _add_echo(duplicate_workspace, 1, character_id=characters[0], slot=3)
    _add_echo(duplicate_workspace, 2, character_id=characters[1])
    group = EchoDuplicateService(database.session_factory).preview(dataset).groups[0]
    assert group.representative_echo_id == slotted
    assert (group.owner_character_id, group.slot_index) == (characters[0], 3)
    newest, _ = _add_echo(duplicate_workspace, 3, character_id=characters[1], slot=1)
    group = EchoDuplicateService(database.session_factory).preview(dataset).groups[0]
    assert group.representative_echo_id == newest
    assert (group.owner_character_id, group.slot_index) == (characters[1], 1)


def test_manual_slot_wins_and_conflicting_manual_slots_remain_distinct(duplicate_workspace) -> None:
    database, dataset, _capture, characters = duplicate_workspace
    manual, _ = _add_echo(duplicate_workspace, 1, character_id=characters[0], slot=3, manual=True)
    automatic, _ = _add_echo(duplicate_workspace, 2, character_id=characters[1], slot=1)
    service = EchoDuplicateService(database.session_factory)
    group = service.preview(dataset).groups[0]
    assert group.representative_echo_id == manual
    assert group.member_echo_ids == tuple(sorted((manual, automatic)))
    second_manual, _ = _add_echo(
        duplicate_workspace, 3, character_id=characters[1], slot=2, manual=True
    )
    preview = service.preview(dataset)
    assert preview.logical_count == 3
    assert {group.representative_echo_id for group in preview.groups} == {
        manual,
        automatic,
        second_manual,
    }


def test_reconciliation_rolls_back_and_refreshes_after_signature_changes(
    duplicate_workspace,
) -> None:
    database, dataset, _capture, _characters = duplicate_workspace
    first, _ = _add_echo(duplicate_workspace, 1)
    second, _ = _add_echo(duplicate_workspace, 2)
    with (
        pytest.raises(RuntimeError, match="rollback"),
        database.session_factory.begin() as session,
    ):
        assert reconcile_echo_duplicates(session, data_set_id=dataset).updated_session_count == 2
        raise RuntimeError("rollback")
    service = EchoDuplicateService(database.session_factory)
    assert service.apply(dataset).updated_session_count == 2
    with database.session_factory.begin() as session:
        record = session.get(EchoRecord, second)
        assert record is not None
        record.sub_stats_json = [*_SUBS[:-1], {"name": "共鳴效率", "value": 11.6, "unit": "%"}]
        result = reconcile_echo_duplicates(session, data_set_id=dataset)
        assert result.preview.logical_count == 2
        assert result.updated_session_count == 2
    with database.session_factory() as session:
        groups = group_echo_records(session, data_set_id=dataset)
        assert {group.representative_echo_id for group in groups} == {first, second}


def test_signature_decimal_normalization_is_exact_and_rejects_unknown_fields() -> None:
    options = {"name": "辛吉勒姆", "level": 25, "cost": 4, "main_stats": _MAIN, "sub_stats": _SUBS}
    assert complete_echo_signature(**options) is not None
    unknown = deepcopy(_SUBS)
    unknown[0]["value"] = None
    assert complete_echo_signature(**{**options, "sub_stats": unknown}) is None
    unknown[0]["value"] = float("nan")
    assert complete_echo_signature(**{**options, "sub_stats": unknown}) is None
    unknown[0]["value"] = True
    assert complete_echo_signature(**{**options, "sub_stats": unknown}) is None
    decimals = deepcopy(_SUBS)
    decimals[0]["value"] = 470.0
    assert complete_echo_signature(**{**options, "sub_stats": decimals}) == complete_echo_signature(
        **options
    )


@pytest.mark.parametrize("level", [7, 12, 17, 22, 24])
def test_non_node_rescan_signature_keeps_exact_level_and_main_values(level: int) -> None:
    options = {
        "name": "封庭械囿",
        "level": level,
        "cost": 3,
        "main_stats": _MAIN,
        "sub_stats": _SUBS[: level // 5],
    }
    signature = complete_echo_signature(**options)
    assert signature is not None
    assert signature == complete_echo_signature(**deepcopy(options))
    assert signature != complete_echo_signature(**{**options, "level": level // 5 * 5})
    assert complete_echo_signature(**{**options, "sub_stats": _SUBS[: level // 5 - 1]}) is None


def test_grouped_history_paginates_after_selecting_an_older_manual_representative(
    duplicate_workspace,
) -> None:
    database, dataset, _capture, characters = duplicate_workspace
    original_echo, original_session = _add_echo(
        duplicate_workspace, 1, character_id=characters[0], slot=3, manual=True
    )
    _different_echo, different_session = _add_echo(duplicate_workspace, 2, name="冠頂械隼")
    _rescan_echo, rescan_session = _add_echo(duplicate_workspace, 3)
    service = EchoHistoryService(database.session_factory)
    before = _snapshot(database)

    raw = service.list_history(data_set_id=dataset)
    assert [row.id for row in raw] == [rescan_session, different_session, original_session]
    assert all(row.rescan_count == 0 and row.grouped_session_ids == () for row in raw)
    first = service.list_history(data_set_id=dataset, group_duplicates=True, limit=1)
    assert len(first) == 1
    assert first[0].id == original_session and first[0].echo_id == original_echo
    assert (first[0].character_id, first[0].slot_index) == (characters[0], 3)
    assert first[0].rescan_count == 1
    assert first[0].grouped_session_ids == (rescan_session, original_session)
    assert service.get_history(first[0].id).session.id == original_session
    second = service.list_history(data_set_id=dataset, group_duplicates=True, limit=1, offset=1)
    assert [row.id for row in second] == [different_session]
    assert service.list_history(data_set_id=dataset, group_duplicates=True, offset=2) == ()
    assert _snapshot(database) == before


def test_grouped_history_keeps_consumed_archived_and_manual_conflict_rows(
    duplicate_workspace,
) -> None:
    database, dataset, _capture, characters = duplicate_workspace
    _add_echo(duplicate_workspace, 1, character_id=characters[0], slot=1, manual=True)
    _add_echo(duplicate_workspace, 2, character_id=characters[1], slot=2, manual=True)
    _add_echo(duplicate_workspace, 3, consumed=True)
    _add_echo(duplicate_workspace, 4, archived=True)
    service = EchoHistoryService(database.session_factory)
    assert service.list_history(data_set_id=dataset, group_duplicates=True) == service.list_history(
        data_set_id=dataset
    )
    assert len(service.list_history(data_set_id=dataset, group_duplicates=True)) == 4


def test_grouped_history_does_not_invent_a_session_for_an_unscanned_representative(
    duplicate_workspace,
) -> None:
    database, dataset, _capture, characters = duplicate_workspace
    _add_echo(duplicate_workspace, 1)
    _add_echo(duplicate_workspace, 2)
    with database.session_factory.begin() as session:
        session.add(
            EchoRecord(
                data_set_id=dataset,
                name="異相·辛吉勒姆",
                level=25,
                cost=4,
                main_stats_json=deepcopy(_MAIN),
                sub_stats_json=deepcopy(_SUBS),
                character_id=characters[0],
                slot_index=3,
                final_status="equipped",
            )
        )
    service = EchoHistoryService(database.session_factory)
    assert service.list_history(data_set_id=dataset, group_duplicates=True) == service.list_history(
        data_set_id=dataset
    )


def test_grouped_history_never_combines_datasets(duplicate_workspace) -> None:
    database, dataset, _capture, characters = duplicate_workspace
    _add_echo(duplicate_workspace, 1)
    _add_echo(duplicate_workspace, 2)
    with database.session_factory.begin() as session:
        other_dataset = DataSetRecord(name="示範", kind="demo", is_active=False)
        session.add(other_dataset)
        session.flush()
        other_capture = CaptureSessionRecord(
            data_set_id=other_dataset.id,
            backend_kind="test",
            monitor_index=1,
            sampling_hz=5.0,
            stable_wait_ms=600,
            app_version="test",
            status="stopped",
        )
        session.add(other_capture)
        session.flush()
        other_workspace = database, other_dataset.id, other_capture.id, characters
    _add_echo(other_workspace, 3)
    _add_echo(other_workspace, 4)
    service = EchoHistoryService(database.session_factory)
    combined = service.list_history(group_duplicates=True)
    assert len(combined) == 2
    assert [row.rescan_count for row in combined] == [1, 1]
    assert len(set(combined[0].grouped_session_ids + combined[1].grouped_session_ids)) == 4
    assert len(service.list_history(data_set_id=dataset, group_duplicates=True)) == 1
    assert len(service.list_history(data_set_id=other_workspace[1], group_duplicates=True)) == 1
