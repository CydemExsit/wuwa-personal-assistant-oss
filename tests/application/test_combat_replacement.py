from __future__ import annotations

from copy import deepcopy

import pytest

from wuwa_assistant.application.characters import (
    CharacterCultivationService,
    CreateCharacter,
    UpsertWeapon,
)
from wuwa_assistant.application.combat_build import CombatTeam, team_to_json
from wuwa_assistant.application.replacement_training import ReplacementTrainingService
from wuwa_assistant.scoring.character_guides import load_character_seed_definitions
from wuwa_assistant.storage.orm import EchoObservationRecord, EchoRecord

from .test_character_stats import _stat, panel_fixture
from .test_equipment_overview import _dump, _echo
from .test_equipment_overview import equipment as equipment_fixture


@pytest.fixture
def equipment(tmp_path):
    yield from equipment_fixture.__wrapped__(tmp_path)


def registered_build(equipment, target=145):
    example = panel_fixture()
    equipment.profiles.ensure_system_templates(load_character_seed_definitions())
    service = CharacterCultivationService(equipment.database.session_factory)
    team = CombatTeam("primary", "常用隊", energy_target=target, confirmed=True)
    owner = service.create_character(
        data_set_id=equipment.data_set_id,
        command=CreateCharacter(
            "珂萊塔",
            element="冷凝",
            level=90,
            small_stat_nodes=(True,) * 10,
            cultivation_goal={
                "panel_config": {"node_layout": 1},
                "combat_teams": [team_to_json(team)],
                "combat_primary_team": "primary",
            },
        ),
    )
    service.upsert_weapon(
        owner.summary.id,
        data_set_id=equipment.data_set_id,
        command=UpsertWeapon("死與舞", level=90),
    )
    for part in example.echo_slots:
        identifier, observation_id = _echo(
            equipment, owner.summary.id, part.slot_index, name=part.name, stats=list(part.sub_stats)
        )
        with equipment.database.session_factory.begin() as session:
            echo = session.get(EchoRecord, identifier)
            echo.cost = part.cost
            echo.set_name = part.set_name
            echo.main_stats_json = list(part.main_stats)
            row = session.get(EchoObservationRecord, observation_id)
            row.cost = part.cost
            row.main_stats_json = list(part.main_stats)
            payload = {
                **row.effective_payload_json,
                "cost": part.cost,
                "set_name": part.set_name,
                "main_stats": list(part.main_stats),
                "sub_stats": list(part.sub_stats),
            }
            row.effective_payload_json = deepcopy(payload)
            row.raw_payload_json = deepcopy(payload)
    return service.get_character(owner.summary.id, data_set_id=equipment.data_set_id)


def candidate(detail):
    part = detail.echo_slots[4]
    return {
        "name": "候選種類",
        "set_name": part.set_name,
        "cost": part.cost,
        "level": 25,
        "main_stats": list(part.main_stats),
        "sub_stats": [
            _stat("暴擊", 10.5),
            _stat("暴擊傷害", 21),
            _stat("攻擊", 11.6),
            _stat("攻擊", 60, None),
            _stat("共鳴技能傷害加成", 11.6),
        ],
    }


def test_missing_er_exceptional_candidate_is_kept_but_not_directly_equipped(equipment):
    detail = registered_build(equipment)
    service = ReplacementTrainingService(equipment.database.session_factory)
    before = _dump(equipment.database)
    value = service.evaluate(
        candidate(detail),
        data_set_id=equipment.data_set_id,
        character_id=detail.summary.id,
        slot_index=5,
    )
    assert value.combat is not None
    assert value.combat.after.sufficient is False
    assert value.combat.after.primary.energy_gap == pytest.approx(8.2)
    assert "充能" in value.conclusion
    assert "可考慮替換" not in value.conclusion
    assert value.delta is not None and value.evaluation.projection is not None
    assert "補強目標" in value.reason
    assert _dump(equipment.database) == before


def test_secondary_energy_requirement_blocks_primary_only_improvement(equipment):
    from dataclasses import replace

    from wuwa_assistant.application.combat_service import CombatBuildService, CombatSettings

    detail = registered_build(equipment, target=130)
    team = CombatTeam("primary", "主隊", energy_target=130, confirmed=True)
    other = replace(team, identifier="other", name="第二隊", energy_target=145)
    CombatBuildService(equipment.database.session_factory).save(
        equipment.data_set_id, CombatSettings(detail.summary.id, (team, other), "primary")
    )
    value = ReplacementTrainingService(equipment.database.session_factory).evaluate(
        candidate(detail),
        data_set_id=equipment.data_set_id,
        character_id=detail.summary.id,
        slot_index=5,
    )
    assert value.combat.after.primary.sufficient is True
    assert value.combat.after.teams[1].sufficient is False
    assert "可考慮替換" not in value.conclusion


def test_partial_candidate_keeps_original_quality_probability_and_adds_battle_probability(
    equipment,
):
    detail = registered_build(equipment)
    payload = candidate(detail)
    payload["level"] = 20
    payload["sub_stats"] = payload["sub_stats"][:4]
    service = ReplacementTrainingService(equipment.database.session_factory)
    value = service.evaluate(
        payload, data_set_id=equipment.data_set_id, character_id=detail.summary.id, slot_index=5
    )
    assert value.evaluation.projection.improvement_probability is not None
    assert value.combat_projection is not None
    assert 0 < value.combat_projection.probability_sufficient < 1
    assert "剩餘副詞條" in value.reason
    assert service.equipped_target(equipment.data_set_id, detail.echo_slots[4].echo_id) == (
        detail.summary.id,
        5,
    )


def test_unknown_candidate_species_and_ocr_review_cannot_gain_battle_truth(equipment):
    detail = registered_build(equipment)
    payload = candidate(detail)
    payload.pop("name")
    service = ReplacementTrainingService(equipment.database.session_factory)
    value = service.evaluate(
        payload, data_set_id=equipment.data_set_id, character_id=detail.summary.id, slot_index=5
    )
    assert value.combat.after.sufficient is None
    reviewed = service.evaluate(
        candidate(detail),
        data_set_id=equipment.data_set_id,
        character_id=detail.summary.id,
        slot_index=5,
        requires_review=True,
    )
    assert reviewed.combat is None
