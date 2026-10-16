from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

from wuwa_assistant.application.characters import (
    CharacterCultivationService,
    CreateCharacter,
    CreateTeam,
    TeamMemberInput,
    TeamMembershipSnapshot,
    UpdateSkill,
)
from wuwa_assistant.application.farming import _farm_character, _role, build_today_farming
from wuwa_assistant.application.scoring_profiles import ScoringProfileService
from wuwa_assistant.planning.farming import FarmRole
from wuwa_assistant.scoring import load_template_seed_definitions
from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.storage.orm import DataSetRecord, EchoRecord


def test_today_farming_reads_existing_skill_targets_and_never_mutates_inventory_or_scores(
    tmp_path: Path,
) -> None:
    database = create_database(AppPaths.from_root(tmp_path / "farming"))
    try:
        with database.session_factory.begin() as session:
            dataset = DataSetRecord(name="主要", kind="primary", is_active=True)
            session.add(dataset)
            session.flush()
            dataset_id = dataset.id
        characters = CharacterCultivationService(database.session_factory)
        profiles = ScoringProfileService(database.session_factory)
        profiles.ensure_system_templates(load_template_seed_definitions())
        healer = characters.create_character(
            data_set_id=dataset_id, command=CreateCharacter("維里奈")
        )
        characters.create_character(
            data_set_id=dataset_id, command=CreateCharacter("不規劃", is_candidate=False)
        )
        archived = characters.create_character(
            data_set_id=dataset_id, command=CreateCharacter("封存角色")
        )
        characters.archive_character(archived.summary.id, data_set_id=dataset_id)
        for index, skill in enumerate(healer.skills):
            characters.update_skill(
                healer.summary.id,
                data_set_id=dataset_id,
                command=UpdateSkill(
                    skill.skill_key,
                    skill.name,
                    skill.skill_type,
                    8 if index == 0 else 10,
                    9 if index == 0 else 10,
                    skill.is_core,
                ),
            )
        with database.session_factory.begin() as session:
            session.add(
                EchoRecord(
                    data_set_id=dataset_id,
                    character_id=healer.summary.id,
                    slot_index=1,
                    name="舊紀錄",
                    level=25,
                    cost=1,
                    set_name="隱世回光",
                    score=0,
                    final_status="equipped",
                    main_stats_json=[
                        {"name": "攻擊", "unit": "%", "value": 18, "stat_kind": "primary"}
                    ],
                )
            )
        with sqlite3.connect(database.paths.database_path) as connection:
            before = "\n".join(connection.iterdump())
        result = build_today_farming(characters, profiles, data_set_id=dataset_id)
        assert result.character_count == 1
        assert len(result.skills) == 1
        assert (result.skills[0].current_level, result.skills[0].target_level) == (8, 9)
        assert result.skills[0].character_name == "維里奈"
        assert result.farming.recommendations
        assert all(row.points == 0 for row in result.farming.recommendations)
        assert any("建議共鳴效率" in note for note in result.farming.notices)
        with sqlite3.connect(database.paths.database_path) as connection:
            assert "\n".join(connection.iterdump()) == before
    finally:
        database.dispose()


def test_role_resolution_uses_explicit_function_and_team_priority_not_slot_number(
    tmp_path: Path,
) -> None:
    database = create_database(AppPaths.from_root(tmp_path / "roles"))
    try:
        with database.session_factory.begin() as session:
            dataset = DataSetRecord(name="主要", kind="primary", is_active=True)
            session.add(dataset)
            session.flush()
            dataset_id = dataset.id
        characters = CharacterCultivationService(database.session_factory)
        detail = characters.create_character(
            data_set_id=dataset_id, command=CreateCharacter("千咲")
        )
        assert _role(detail)[0] is FarmRole.SECONDARY
        positioned = replace(detail, teams=(TeamMembershipSnapshot("team", "第一隊", 1, 3, None),))
        assert _role(positioned)[0] is FarmRole.SECONDARY
        roles = replace(
            detail,
            teams=(
                TeamMembershipSnapshot("secondary", "備用隊", 100, 1, "副C"),
                TeamMembershipSnapshot("main", "主力隊", 1, 3, "主C"),
            ),
        )
        assert _role(roles)[0] is FarmRole.MAIN
        assert "主力隊" in _role(roles)[1]
        explicit = replace(roles, summary=replace(roles.summary, role="治療"))
        assert _role(explicit)[0] is FarmRole.HEALER
    finally:
        database.dispose()


def test_importance_reads_saved_character_and_best_team_priority_without_duplicate_membership(
    tmp_path: Path,
) -> None:
    with create_database(AppPaths.from_root(tmp_path)) as database:
        with database.session_factory.begin() as session:
            dataset = DataSetRecord(name="主要", kind="primary", is_active=True)
            session.add(dataset)
            session.flush()
            dataset_id = dataset.id
        characters = CharacterCultivationService(database.session_factory)
        profiles = ScoringProfileService(database.session_factory)
        profiles.ensure_system_templates(load_template_seed_definitions())
        detail = characters.create_character(
            data_set_id=dataset_id, command=CreateCharacter("千咲", priority=7)
        )
        for index, priority in enumerate((50, 2)):
            characters.create_team(
                data_set_id=dataset_id,
                command=CreateTeam(f"隊伍{index}", priority=priority),
                members=(TeamMemberInput(detail.summary.id, 3, "副C"),),
            )
        loaded = characters.get_character(detail.summary.id, data_set_id=dataset_id)
        overview = characters.equipment_overview(data_set_id=dataset_id).characters[0]
        farm = _farm_character(loaded, overview, profiles)
        assert farm.priority == 7 and farm.best_team_priority == 2
        assert farm.role is FarmRole.SECONDARY
        plan = build_today_farming(characters, profiles, data_set_id=dataset_id)
        assert plan.character_count == 1
        assert all(row.character_count <= 1 for row in plan.farming.recommendations)
