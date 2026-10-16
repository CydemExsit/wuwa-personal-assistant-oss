from copy import deepcopy

from wuwa_assistant.application.observation_review import (
    hold_reviewed_score,
    observation_review_issues,
)
from wuwa_assistant.application.scoring_workflow import observed_stats_from_payload
from wuwa_assistant.scoring.models import EchoScoreBreakdown


def payload():
    def stat(name, value, unit):
        return dict(
            name=name,
            value=value,
            unit=unit,
            confidence=0.99,
            name_confidence=0.99,
            value_confidence=0.99,
        )

    return dict(
        name="岩蛛S4型",
        level=25,
        cost=1,
        confidence=dict(name=0.99, level=0.99, cost=0.99, page_type=0.99),
        main_stats=[stat("攻擊", 18.0, "%"), stat("生命", 2280, "")],
        sub_stats=[
            stat("防禦", 10.9, "%"),
            stat("共鳴技能傷害加成", 11.6, "%"),
            stat("暴擊", 8.1, "%"),
            stat("暴擊傷害", 18.6, "%"),
            stat("重擊傷害加成", 6.4, "%"),
        ],
    )


def test_missing_identity_does_not_falsify_good_substat_confidence():
    value = payload()
    value["name"] = None
    before = deepcopy(value)
    issues = observation_review_issues(value, requires_review=True)
    assert [i.field_path for i in issues] == ["/name"]
    assert all(
        s.confidence == 0.99 for s in observed_stats_from_payload(value, requires_review=True)
    )
    assert value == before


def test_confirming_one_field_does_not_confirm_another_or_mutate_raw_confidence():
    value = payload()
    value["sub_stats"][0].update(value_confidence=0.4, confidence=0.4)
    value["sub_stats"][2].update(name_confidence=0.3, confidence=0.3)
    assert {i.field_path for i in observation_review_issues(value)} == {
        "/sub_stats/0/value",
        "/sub_stats/2/name",
    }
    value["manual_confirmed_fields"] = ["/sub_stats/0/value"]
    assert {i.field_path for i in observation_review_issues(value)} == {"/sub_stats/2/name"}
    stats = observed_stats_from_payload(value)
    assert stats[0].confidence == 0.99 and stats[2].confidence == 0.3
    assert value["sub_stats"][0]["confidence"] == 0.4


def test_generic_confidence_conflict_and_invalid_rolls_remain_actionable():
    value = payload()
    value["sub_stats"][0]["confidence"] = 0.4
    paths = {i.field_path for i in observation_review_issues(value)}
    assert paths == {"/sub_stats/0/name", "/sub_stats/0/value"}
    value["manual_confirmed_fields"] = ["/sub_stats/0/name", "/sub_stats/0/value"]
    value["sub_stats"][0]["value"] = 999
    issues = observation_review_issues(value)
    assert len(issues) == 1 and "已知檔位" in issues[0].reason


def test_custom_confidence_threshold_and_missing_count_have_exact_reasons():
    value = payload()
    value["sub_stats"][3]["value_confidence"] = 0.98
    issues = observation_review_issues(value, minimum_confidence=0.99)
    assert [i.field_path for i in issues] == ["/sub_stats/3/value"]
    value["sub_stats"].pop()
    issues = observation_review_issues(value)
    assert [i.field_path for i in issues] == ["/sub_stats"]
    assert "應有 5 條" in issues[0].reason


def test_optional_evidence_names_the_blocker_and_cannot_be_cleared_by_other_confirmation():
    value = payload()
    value["confidence"].update(set_name=0.4, equipped_slot_index=0.5, is_equipped=None)
    value["manual_confirmed_fields"] = ["/name"]
    issues = observation_review_issues(value)
    assert {i.field_path for i in issues} == {"/set_name", "/equipped_slot_index"}
    assert "重新辨識" in issues[1].label
    value["manual_confirmed_fields"].append("/set_name")
    assert [i.field_path for i in observation_review_issues(value)] == ["/equipped_slot_index"]


def test_scoring_configuration_failure_is_distinct_from_ocr_review():
    breakdown = EchoScoreBreakdown(
        profile_id="custom",
        profile_version=1,
        score=None,
        band=None,
        lines=(),
        earned_weight_points=0,
        maximum_weight_points=1,
        missing_context=("panel_crit_rate",),
        unknown_stats=("profile_missing_weight:crit_damage",),
        requires_manual_confirmation=True,
    )
    result = hold_reviewed_score(breakdown, payload(), requires_review=False)
    assert result.score is None
    assert observation_review_issues(payload()) == ()
    assert any("角色面板暴擊" in note for note in result.notes)
    assert any("缺少詞條權重: crit_damage" in note for note in result.notes)
    assert all("辨識信心" not in note for note in result.notes)
