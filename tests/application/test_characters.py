from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import func, select

from wuwa_assistant.application.characters import (
    CharacterCultivationService,
    CreateCharacter,
    CreateTeam,
    SaveCharacter,
    TeamMemberInput,
    UpdateCharacter,
    UpdateSkill,
    UpdateTeam,
    UpsertWeapon,
    assign_echo_record,
    ensure_recognized_character,
)
from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.storage.database import Database
from wuwa_assistant.storage.orm import (
    CaptureSessionRecord,
    CharacterRecord,
    DataSetRecord,
    EchoRecord,
    EchoSessionRecord,
    ScoringProfileRecord,
    TeamMemberRecord,
    WeaponRecord,
    utc_now,
)


@pytest.fixture
def storage(
    tmp_path: Path,
) -> Iterator[
    tuple[
        Database,
        CharacterCultivationService,
        str,
        str,
        str,
        str,
        str,
    ]
]:
    database = create_database(AppPaths.from_root(tmp_path / "characters"))
    with database.session_factory.begin() as session:
        primary = DataSetRecord(name="主要資料", kind="primary", is_active=True)
        demo = DataSetRecord(name="示範資料", kind="demo", is_active=False)
        session.add_all([primary, demo])
        session.flush()
        system_profile = ScoringProfileRecord(
            data_set_id=None,
            name="通用輸出模板",
            template_kind="generic_dps",
            is_system_template=True,
        )
        primary_profile = ScoringProfileRecord(
            data_set_id=primary.id,
            name="緋雪設定",
            template_kind="custom",
            is_system_template=False,
        )
        demo_profile = ScoringProfileRecord(
            data_set_id=demo.id,
            name="示範設定",
            template_kind="custom",
            is_system_template=False,
        )
        session.add_all([system_profile, primary_profile, demo_profile])
        session.flush()
        values = (
            primary.id,
            demo.id,
            system_profile.id,
            primary_profile.id,
            demo_profile.id,
        )
    try:
        yield (
            database,
            CharacterCultivationService(database.session_factory),
            *values,
        )
    finally:
        database.dispose()


def _create(
    service: CharacterCultivationService,
    data_set_id: str,
    name: str,
    *,
    profile_id: str | None = None,
) -> str:
    return service.create_character(
        data_set_id=data_set_id,
        command=CreateCharacter(name=name, scoring_profile_id=profile_id),
    ).summary.id


def _skill_edits(*, current: int = 6, target: int = 10) -> tuple[UpdateSkill, ...]:
    definitions = (
        ("normal_attack", "常態攻擊", "normal_attack", False),
        ("resonance_skill", "共鳴技能", "resonance_skill", True),
        ("forte_circuit", "共鳴迴路", "forte_circuit", True),
        ("resonance_liberation", "共鳴解放", "resonance_liberation", True),
        ("intro_skill", "變奏技能", "intro_skill", False),
    )
    return tuple(
        UpdateSkill(
            skill_key=key,
            name=name,
            skill_type=skill_type,
            current_level=current,
            target_level=target if is_core else 6,
            is_core=is_core,
        )
        for key, name, skill_type, is_core in definitions
    )


def test_create_character_has_default_skills_nodes_and_five_empty_echo_slots(
    storage: tuple[
        Database,
        CharacterCultivationService,
        str,
        str,
        str,
        str,
        str,
    ],
) -> None:
    _database, service, primary_id, _demo_id, system_profile_id, *_profiles = storage

    detail = service.create_character(
        data_set_id=primary_id,
        command=CreateCharacter(
            name="緋雪",
            element="冷凝",
            role="主輸出",
            priority=10,
            level=90,
            resonance_chain=1,
            scoring_profile_id=system_profile_id,
            cultivation_goal={"label": "深境區"},
            small_stat_nodes=(True, False, False, False, False, False, False, False, False, True),
            notes="固定上場",
        ),
    )

    assert detail.summary.name == "緋雪"
    assert detail.summary.scoring_profile_name == "通用輸出模板"
    assert [skill.skill_key for skill in detail.skills] == [
        "normal_attack",
        "resonance_skill",
        "forte_circuit",
        "resonance_liberation",
        "intro_skill",
    ]
    assert [skill.target_level for skill in detail.skills] == [6, 10, 10, 10, 6]
    assert detail.small_stat_nodes == (
        True,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        True,
    )
    assert detail.cultivation_goal["skill_target_defaults"] == {"core": 10, "secondary": 6}
    assert [slot.slot_index for slot in detail.echo_slots] == [1, 2, 3, 4, 5]
    assert all(slot.echo_id is None for slot in detail.echo_slots)
    assert detail.summary.completed_small_stat_nodes == 2
    assert detail.summary.completed_skill_count == 0


def test_atomic_save_updates_full_aggregate_and_rolls_back_every_field_on_failure(
    storage: tuple[
        Database,
        CharacterCultivationService,
        str,
        str,
        str,
        str,
        str,
    ],
) -> None:
    database, service, primary_id, _demo_id, _system, primary_profile_id, _demo = storage
    character_id = _create(service, primary_id, "角色 A")
    nodes = (True,) * 7 + (False,) * 3

    saved = service.save_character(
        character_id=character_id,
        data_set_id=primary_id,
        command=SaveCharacter(
            character=UpdateCharacter(
                name="角色 A 完整",
                element="熱熔",
                role="輸出",
                is_candidate=True,
                priority=12,
                level=80,
                resonance_chain=2,
                scoring_profile_id=primary_profile_id,
                notes="第一隊",
                cultivation_goal={"goal": "及格"},
                small_stat_nodes=nodes,
            ),
            weapon=UpsertWeapon(
                name="專武",
                weapon_type="迅刀",
                level=90,
                resonance_rank=2,
            ),
            skills=_skill_edits(current=10),
        ),
    )
    assert saved.weapon is not None and saved.weapon.name == "專武"
    assert saved.summary.completed_skill_count == 5
    assert saved.summary.completed_small_stat_nodes == 7

    invalid_skills = list(_skill_edits(current=9))
    invalid_skills[-1] = UpdateSkill(
        skill_key="unknown_skill",
        name="不存在",
        skill_type="unknown",
        current_level=9,
        target_level=10,
        is_core=True,
    )
    with pytest.raises(ValueError, match="skill does not exist"):
        service.save_character(
            character_id=character_id,
            data_set_id=primary_id,
            command=SaveCharacter(
                character=UpdateCharacter(
                    name="不應保存",
                    element="導電",
                    role="錯誤",
                    is_candidate=False,
                    priority=999,
                    level=1,
                    resonance_chain=0,
                    scoring_profile_id=None,
                    notes="rollback",
                    small_stat_nodes=(False,) * 10,
                ),
                weapon=UpsertWeapon(name="不應保存的武器", level=1),
                skills=tuple(invalid_skills),
            ),
        )

    after = service.get_character(character_id, data_set_id=primary_id)
    assert after.summary.name == "角色 A 完整"
    assert after.summary.is_candidate is True
    assert after.weapon is not None and after.weapon.name == "專武"
    assert all(skill.current_level == 10 for skill in after.skills)
    assert after.small_stat_nodes == nodes
    with database.session_factory() as session:
        assert session.scalar(select(func.count(WeaponRecord.id))) == 1


def test_atomic_save_can_create_character_weapon_and_skill_levels_together(
    storage: tuple[
        Database,
        CharacterCultivationService,
        str,
        str,
        str,
        str,
        str,
    ],
) -> None:
    _database, service, primary_id, *_ids = storage
    result = service.save_character(
        data_set_id=primary_id,
        command=SaveCharacter(
            character=CreateCharacter(name="新角色"),
            weapon=UpsertWeapon(name="新武器", level=40, resonance_rank=1),
            skills=_skill_edits(current=4),
        ),
    )
    assert result.summary.name == "新角色"
    assert result.weapon is not None and result.weapon.name == "新武器"
    assert [skill.current_level for skill in result.skills] == [4, 4, 4, 4, 4]


@pytest.mark.parametrize(
    ("command", "message"),
    [
        (CreateCharacter(name="A", level=91), "level"),
        (CreateCharacter(name="A", resonance_chain=7), "resonance_chain"),
    ],
)
def test_character_game_ranges_are_enforced(
    storage: tuple[
        Database,
        CharacterCultivationService,
        str,
        str,
        str,
        str,
        str,
    ],
    command: CreateCharacter,
    message: str,
) -> None:
    database, service, primary_id, *_ids = storage
    with pytest.raises(ValueError, match=message):
        service.create_character(data_set_id=primary_id, command=command)
    with database.session_factory() as session:
        assert session.scalar(select(func.count(CharacterRecord.id))) == 0


def test_weapon_skill_and_node_edits_validate_ranges_and_remain_one_to_one(
    storage: tuple[
        Database,
        CharacterCultivationService,
        str,
        str,
        str,
        str,
        str,
    ],
) -> None:
    database, service, primary_id, *_ids = storage
    character_id = _create(service, primary_id, "角色 B")
    first = service.upsert_weapon(
        character_id,
        data_set_id=primary_id,
        command=UpsertWeapon(name="武器一", level=80, resonance_rank=1),
    )
    second = service.upsert_weapon(
        character_id,
        data_set_id=primary_id,
        command=UpsertWeapon(name="武器二", level=90, resonance_rank=5),
    )
    assert first.weapon is not None and second.weapon is not None
    assert first.weapon.id == second.weapon.id
    assert second.weapon.name == "武器二"

    with pytest.raises(ValueError, match="weapon level"):
        service.upsert_weapon(
            character_id,
            data_set_id=primary_id,
            command=UpsertWeapon(name="錯誤", level=91),
        )
    with pytest.raises(ValueError, match="resonance rank"):
        service.upsert_weapon(
            character_id,
            data_set_id=primary_id,
            command=UpsertWeapon(name="錯誤", resonance_rank=0),
        )
    with pytest.raises(ValueError, match="target skill level"):
        service.update_skill(
            character_id,
            data_set_id=primary_id,
            command=UpdateSkill(
                skill_key="normal_attack",
                name="常態攻擊",
                skill_type="normal_attack",
                current_level=1,
                target_level=0,
                is_core=False,
            ),
        )

    updated = service.update_skill(
        character_id,
        data_set_id=primary_id,
        command=UpdateSkill(
            skill_key="normal_attack",
            name="常態攻擊",
            skill_type="normal_attack",
            current_level=6,
            target_level=6,
            is_core=False,
        ),
    )
    assert updated.skills[0].is_completed is True
    updated = service.set_small_stat_node(
        character_id,
        10,
        True,
        data_set_id=primary_id,
    )
    assert updated.small_stat_nodes[-1] is True
    with pytest.raises(ValueError, match="node_index"):
        service.set_small_stat_node(character_id, 0, True, data_set_id=primary_id)

    without_weapon = service.remove_weapon(character_id, data_set_id=primary_id)
    assert without_weapon.weapon is None
    with database.session_factory() as session:
        assert session.scalar(select(func.count(WeaponRecord.id))) == 0


def test_profile_and_character_names_are_scoped_to_dataset(
    storage: tuple[
        Database,
        CharacterCultivationService,
        str,
        str,
        str,
        str,
        str,
    ],
) -> None:
    database, service, primary_id, demo_id, _system, _primary_profile, demo_profile = storage
    _create(service, primary_id, "同名角色")
    _create(service, demo_id, "同名角色")

    with pytest.raises(ValueError, match="scoring profile"):
        service.create_character(
            data_set_id=primary_id,
            command=CreateCharacter(name="跨資料", scoring_profile_id=demo_profile),
        )
    with pytest.raises(ValueError, match="already exists"):
        _create(service, primary_id, "同名角色")

    with database.session_factory() as session:
        primary_names = session.scalars(
            select(CharacterRecord.name).where(CharacterRecord.data_set_id == primary_id)
        ).all()
        assert primary_names == ["同名角色"]


def test_team_crud_membership_and_cross_dataset_replacement_roll_back(
    storage: tuple[
        Database,
        CharacterCultivationService,
        str,
        str,
        str,
        str,
        str,
    ],
) -> None:
    database, service, primary_id, demo_id, *_profiles = storage
    first = _create(service, primary_id, "隊員一")
    second = _create(service, primary_id, "隊員二")
    demo = _create(service, demo_id, "示範隊員")
    team = service.create_team(
        data_set_id=primary_id,
        command=CreateTeam(name="第一隊", priority=10),
        members=(
            TeamMemberInput(first, 1, "主輸出"),
            TeamMemberInput(second, 2, "輔助"),
        ),
    )
    assert [member.character_name for member in team.members] == ["隊員一", "隊員二"]

    with pytest.raises(ValueError, match="requested data set"):
        service.replace_team_members(
            team.id,
            (TeamMemberInput(first, 1), TeamMemberInput(demo, 2)),
            data_set_id=primary_id,
        )
    after = service.get_team(team.id, data_set_id=primary_id)
    assert [member.character_id for member in after.members] == [first, second]
    assert service.get_character(first, data_set_id=primary_id).teams[0].team_name == "第一隊"

    renamed = service.update_team(
        team.id,
        data_set_id=primary_id,
        command=UpdateTeam(name="核心隊", priority=1, notes="週期內容"),
    )
    assert renamed.name == "核心隊"
    assert service.list_teams(data_set_id=primary_id)[0].priority == 1
    assert service.delete_team(team.id, data_set_id=primary_id) is True
    with database.session_factory() as session:
        assert session.scalar(select(func.count(TeamMemberRecord.team_id))) == 0


def test_echo_slots_are_dataset_safe_idempotent_and_replace_explicitly(
    storage: tuple[
        Database,
        CharacterCultivationService,
        str,
        str,
        str,
        str,
        str,
    ],
) -> None:
    database, service, primary_id, demo_id, *_profiles = storage
    first_character = _create(service, primary_id, "裝備角色")
    other_character = _create(service, primary_id, "其他角色")
    with database.session_factory.begin() as session:
        first = EchoRecord(
            data_set_id=primary_id,
            name="聲骸一",
            level=25,
            cost=4,
            score=80,
            main_stats_json=[{"name": "暴擊", "value": 22, "unit": "%"}],
            sub_stats_json=[{"name": "暴擊傷害", "value": 15, "unit": "%"}],
        )
        second = EchoRecord(data_set_id=primary_id, name="聲骸二", level=25, cost=4)
        archived = EchoRecord(
            data_set_id=primary_id,
            name="已封存",
            level=25,
            cost=4,
            archived_at=utc_now(),
        )
        demo = EchoRecord(data_set_id=demo_id, name="示範聲骸", level=25, cost=4)
        session.add_all([first, second, archived, demo])
        session.flush()
        first_id, second_id, demo_id_value = first.id, second.id, demo.id

    assigned = service.assign_echo_slot(
        first_character,
        1,
        first_id,
        data_set_id=primary_id,
    )
    assert assigned.echo_slots[0].echo_id == first_id
    assert assigned.echo_slots[0].main_stats[0]["name"] == "暴擊"
    choices = service.list_echoes_for_assignment(
        data_set_id=primary_id,
        character_id=first_character,
    )
    assert {choice.id for choice in choices} == {first_id, second_id}
    first_choice = next(choice for choice in choices if choice.id == first_id)
    assert first_choice.current_character_id == first_character
    assert first_choice.current_character_name == "裝備角色"
    assert first_choice.current_slot_index == 1
    assert first_choice.is_assigned_to_requested_character is True
    assert all(choice.name != "示範聲骸" for choice in choices)
    assert all(choice.name != "已封存" for choice in choices)
    assert (
        service.assign_echo_slot(first_character, 1, first_id, data_set_id=primary_id)
        .echo_slots[0]
        .echo_id
        == first_id
    )

    with pytest.raises(ValueError, match="requested data set"):
        service.assign_echo_slot(
            first_character,
            2,
            demo_id_value,
            data_set_id=primary_id,
        )
    with pytest.raises(ValueError, match="already assigned"):
        service.assign_echo_slot(
            first_character,
            1,
            second_id,
            data_set_id=primary_id,
        )
    assert (
        service.get_character(first_character, data_set_id=primary_id).echo_slots[0].echo_id
        == first_id
    )

    replaced = service.assign_echo_slot(
        first_character,
        1,
        second_id,
        data_set_id=primary_id,
        replace=True,
    )
    assert replaced.echo_slots[0].echo_id == second_id
    with database.session_factory() as session:
        old = session.get(EchoRecord, first_id)
        assert old is not None and old.character_id is None and old.final_status == "kept"

    with pytest.raises(ValueError, match="already assigned"):
        service.assign_echo_slot(
            other_character,
            1,
            second_id,
            data_set_id=primary_id,
        )
    moved = service.assign_echo_slot(
        other_character,
        1,
        second_id,
        data_set_id=primary_id,
        replace=True,
    )
    assert moved.echo_slots[0].echo_id == second_id
    cleared = service.unassign_echo_slot(other_character, 1, data_set_id=primary_id)
    assert cleared.echo_slots[0].echo_id is None


def test_recognized_character_reuses_names_and_preserves_personal_fields(
    storage: tuple[Database, CharacterCultivationService, str, str, str, str, str],
) -> None:
    database, service, primary_id, demo_id, *_profiles = storage
    existing = service.create_character(
        data_set_id=primary_id,
        command=CreateCharacter(name="Rover", level=90, element="衍射", notes="手動設定"),
    )
    with database.session_factory.begin() as session:
        matched, created = ensure_recognized_character(
            session, data_set_id=primary_id, name="  ROVER  "
        )
        assert matched is not None and matched.id == existing.summary.id and not created
        assert (matched.name, matched.level, matched.element, matched.notes) == (
            "Rover",
            90,
            "衍射",
            "手動設定",
        )
        added, created = ensure_recognized_character(session, data_set_id=demo_id, name="ROVER")
        assert added is not None and created and added.id != matched.id
        added_id = added.id
        again, created = ensure_recognized_character(session, data_set_id=demo_id, name="rover")
        assert again is added and not created

    initial = service.get_character(added_id, data_set_id=demo_id)
    assert initial.summary.element is None and initial.summary.role is None
    assert len(initial.skills) == 5
    assert all(slot.echo_id is None for slot in initial.echo_slots)


def test_recognized_character_does_not_restore_or_duplicate_archived_name(
    storage: tuple[Database, CharacterCultivationService, str, str, str, str, str],
) -> None:
    database, service, primary_id, *_ids = storage
    character_id = _create(service, primary_id, "Archived")
    service.archive_character(character_id, data_set_id=primary_id)
    with database.session_factory.begin() as session:
        assert ensure_recognized_character(session, data_set_id=primary_id, name="ARCHIVED") == (
            None,
            False,
        )
        assert session.scalar(select(func.count(CharacterRecord.id))) == 1
    assert (
        service.get_character(character_id, data_set_id=primary_id).summary.archived_at is not None
    )


def test_assignment_and_recognized_character_creation_roll_back_with_caller(
    storage: tuple[Database, CharacterCultivationService, str, str, str, str, str],
) -> None:
    database, service, primary_id, *_ids = storage
    character_id = _create(service, primary_id, "已裝備角色")
    with database.session_factory.begin() as session:
        original = EchoRecord(data_set_id=primary_id, name="原聲骸")
        replacement = EchoRecord(data_set_id=primary_id, name="替換聲骸")
        session.add_all([original, replacement])
        session.flush()
        original_id, replacement_id = original.id, replacement.id
    service.assign_echo_slot(character_id, 1, original_id, data_set_id=primary_id)

    with (
        pytest.raises(RuntimeError, match="later ingestion failure"),
        database.session_factory.begin() as session,
    ):
        ensure_recognized_character(session, data_set_id=primary_id, name="不應保存角色")
        assign_echo_record(
            session, character_id, 1, replacement_id, data_set_id=primary_id, replace=True
        )
        raise RuntimeError("later ingestion failure")

    assert (
        service.get_character(character_id, data_set_id=primary_id).echo_slots[0].echo_id
        == original_id
    )
    with database.session_factory() as session:
        assert session.scalar(select(func.count(CharacterRecord.id))) == 1
        replacement = session.get(EchoRecord, replacement_id)
        assert replacement is not None
        assert replacement.character_id is None and replacement.slot_index is None


@pytest.mark.parametrize("unavailable", ["character", "echo", "consumed"])
def test_assignment_helper_rejects_unavailable_records_before_replacement(
    storage: tuple[Database, CharacterCultivationService, str, str, str, str, str],
    unavailable: str,
) -> None:
    database, service, primary_id, *_ids = storage
    character_id = _create(service, primary_id, "裝備角色")
    with database.session_factory.begin() as session:
        original = EchoRecord(data_set_id=primary_id, name="原聲骸")
        incoming = EchoRecord(data_set_id=primary_id, name="不可使用聲骸")
        session.add_all([original, incoming])
        session.flush()
        original_id, incoming_id = original.id, incoming.id
        if unavailable == "echo":
            incoming.archived_at = utc_now()
        elif unavailable == "consumed":
            capture = CaptureSessionRecord(
                data_set_id=primary_id, backend_kind="fake", monitor_index=0, app_version="test"
            )
            session.add(capture)
            session.flush()
            session.add(
                EchoSessionRecord(
                    capture_session_id=capture.id,
                    local_session_id="consumed",
                    echo_id=incoming.id,
                    was_consumed=True,
                )
            )
    service.assign_echo_slot(character_id, 1, original_id, data_set_id=primary_id)
    if unavailable == "character":
        service.archive_character(character_id, data_set_id=primary_id)

    with (
        pytest.raises(ValueError, match=r"archived|consumed"),
        database.session_factory.begin() as session,
    ):
        assign_echo_record(
            session, character_id, 1, incoming_id, data_set_id=primary_id, replace=True
        )
    assert (
        service.get_character(character_id, data_set_id=primary_id).echo_slots[0].echo_id
        == original_id
    )


def test_archive_is_non_destructive_and_controls_candidate_lists(
    storage: tuple[
        Database,
        CharacterCultivationService,
        str,
        str,
        str,
        str,
        str,
    ],
) -> None:
    _database, service, primary_id, *_ids = storage
    first = _create(service, primary_id, "常用角色")
    second = _create(service, primary_id, "候補角色")
    service.set_candidate(second, False, data_set_id=primary_id)
    assert {item.id for item in service.list_characters(data_set_id=primary_id)} == {first, second}
    assert [
        item.id for item in service.list_characters(data_set_id=primary_id, candidates_only=True)
    ] == [first]

    archived = service.delete_character(first, data_set_id=primary_id)
    assert archived.summary.archived_at is not None
    assert archived.summary.is_candidate is False
    assert [item.id for item in service.list_characters(data_set_id=primary_id)] == [second]
    assert set(
        item.id for item in service.list_characters(data_set_id=primary_id, include_archived=True)
    ) == {first, second}
    with pytest.raises(ValueError, match="archived"):
        service.set_candidate(first, True, data_set_id=primary_id)
    restored = service.restore_character(first, data_set_id=primary_id)
    assert restored.summary.archived_at is None
    assert restored.summary.is_candidate is True
