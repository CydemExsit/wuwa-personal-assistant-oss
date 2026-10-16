from copy import deepcopy

import pytest

from wuwa_assistant.application.replacement_training import ReplacementTrainingService
from wuwa_assistant.scoring import UpgradeAction
from wuwa_assistant.storage.orm import CharacterRecord, ScoringProfileRecord

from .test_equipment_overview import _dump, _guide_equipment
from .test_equipment_overview import equipment as _equipment_fixture


@pytest.fixture
def equipment(tmp_path):
    yield from _equipment_fixture.__wrapped__(tmp_path)


def _payload(target):
    return {
        "name": target.piece.name,
        "level": 20,
        "cost": 1,
        "set_name": "雪落無聲之願",
        "main_stats": list(target.piece.main_stats),
        "sub_stats": [
            {"name": name, "value": value, "unit": "%", "confidence": 1, "slot_index": i}
            for i, (name, value) in enumerate(
                [("攻擊", 8.6), ("暴擊傷害", 16.2), ("暴擊", 6.9), ("共鳴效率", 8.4)], 1
            )
        ],
    }


@pytest.mark.parametrize("identity", ["same", "different", "missing"])
def test_identity_not_identical_name_or_score_selects_self_cultivation(equipment, identity):
    owner = _guide_equipment(equipment)
    service = ReplacementTrainingService(equipment.database.session_factory)
    target = service.target(equipment.data_set_id, owner, 1)
    payload = _payload(target)
    before = _dump(equipment.database)
    result = service.evaluate(
        payload,
        data_set_id=equipment.data_set_id,
        character_id=owner,
        slot_index=1,
        candidate_echo_id={
            "same": target.piece.echo_id,
            "different": "another-echo",
            "missing": None,
        }[identity],
    )
    assert result.evaluation.breakdown.score == pytest.approx(79.46)
    assert result.is_same_echo is (identity == "same")
    if identity == "same":
        assert result.conclusion == "建議升至 +25 並保留"
        assert result.evaluation.decision.action is UpgradeAction.QUALIFIED
        assert result.evaluation.decision.suggested_level == 25
        assert "同一顆" in result.reason
        assert "63,500" in result.reason
        assert result.evaluation.projection.improvement_target is None
        assert not result.target_comparable and result.delta is None
    else:
        assert result.target_comparable
        assert result.evaluation.projection.improvement_target == target.piece.score
    assert _dump(equipment.database) == before


@pytest.mark.parametrize(
    "case,expected",
    [
        ("review", "資料待補"),
        ("missing_stat", "資料待補"),
        ("wrong_set", "先檢查配裝"),
        ("wrong_main", "先檢查配裝"),
        ("missing_set", "配裝資料待補"),
        ("wrong_cost", "COST 不同"),
        ("maxed", "已滿級, 可保留"),
        ("weak", "建議停止追加強化"),
    ],
)
def test_same_echo_still_respects_fit_uncertainty_quality_and_max_level(equipment, case, expected):
    owner = _guide_equipment(equipment)
    service = ReplacementTrainingService(equipment.database.session_factory)
    target = service.target(equipment.data_set_id, owner, 1)
    payload = deepcopy(_payload(target))
    if case == "missing_stat":
        payload["sub_stats"].pop()
    if case == "wrong_set":
        payload["set_name"] = "輕雲出月"
    if case == "wrong_main":
        payload["main_stats"][0]["name"] = "生命"
    if case == "missing_set":
        payload["set_name"] = None
    if case == "wrong_cost":
        payload["cost"] = 3
    if case == "maxed":
        payload["level"] = 25
        payload["sub_stats"].append(
            {"name": "防禦", "value": 40, "unit": "", "confidence": 1, "slot_index": 5}
        )
    if case == "weak":
        payload["sub_stats"] = [
            {"name": name, "value": value, "unit": unit, "confidence": 1, "slot_index": i}
            for i, (name, value, unit) in enumerate(
                [("生命", 430, ""), ("防禦", 40, ""), ("生命", 6.4, "%"), ("防禦", 8.1, "%")], 1
            )
        ]
    result = service.evaluate(
        payload,
        data_set_id=equipment.data_set_id,
        character_id=owner,
        slot_index=1,
        candidate_echo_id=target.piece.echo_id,
        requires_review=case == "review",
    )
    assert result.is_same_echo and not result.target_comparable
    assert result.delta is None
    assert result.conclusion == expected


def test_self_cultivation_uses_custom_character_threshold(equipment):
    owner = _guide_equipment(equipment)
    service = ReplacementTrainingService(equipment.database.session_factory)
    target = service.target(equipment.data_set_id, owner, 1)
    with equipment.database.session_factory.begin() as session:
        session.get(ScoringProfileRecord, target.profile_id).passing_threshold = 80
    result = service.evaluate(
        _payload(target),
        data_set_id=equipment.data_set_id,
        character_id=owner,
        slot_index=1,
        candidate_echo_id=target.piece.echo_id,
    )
    assert result.evaluation.breakdown.score == 79.46
    assert result.evaluation.decision.action is not UpgradeAction.QUALIFIED
    assert result.conclusion != "建議升至 +25 並保留"


def test_self_identity_respects_recorded_slot_fit_confirmation(equipment):
    owner = _guide_equipment(equipment)
    service = ReplacementTrainingService(equipment.database.session_factory)
    target = service.target(equipment.data_set_id, owner, 1)
    with equipment.database.session_factory.begin() as session:
        session.get(CharacterRecord, owner).cultivation_goal_json = {
            "echo_slot_confirmations": {"1": {"echo_id": target.piece.echo_id, "main_stat": False}}
        }
    result = service.evaluate(
        _payload(target),
        data_set_id=equipment.data_set_id,
        character_id=owner,
        slot_index=1,
        candidate_echo_id=target.piece.echo_id,
    )
    assert result.is_same_echo
    assert result.conclusion == "先檢查配裝"
