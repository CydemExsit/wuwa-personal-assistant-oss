"""A weak baseline must not make costly low-quality upgrades attractive."""

import json
from dataclasses import replace
from importlib.resources import files

import pytest

from wuwa_assistant.scoring import (
    DecisionInputs,
    EchoScoreBreakdown,
    EchoScoringEngine,
    EmbryoSupply,
    ObservedStat,
    ProjectionSummary,
    UpgradeAction,
    UpgradeDecisionEngine,
)
from wuwa_assistant.scoring.character_guides import guide_for_character
from wuwa_assistant.scoring.data import LEGACY_ROLL_VERSION


def _profile():
    guide = guide_for_character("洛瑟菈")
    assert guide is not None
    return guide.profile()


def _score(value=15):
    profile = _profile()
    return EchoScoreBreakdown(profile.profile_id, profile.version, value, None, (), 0, 1)


def _projection(level=22, target=25, *, outcomes=((15.0, 0.65), (60.0, 0.35))):
    return ProjectionSummary(
        assumptions_version="test-distribution",
        remaining_slots=5 - level // 5,
        outcome_count=len(outcomes),
        minimum_score=min(score for score, probability in outcomes),
        maximum_score=max(score for score, probability in outcomes),
        expected_score=sum(score * probability for score, probability in outcomes),
        probabilities={
            "usable": sum(probability for score, probability in outcomes if score >= 55),
            "passing": sum(probability for score, probability in outcomes if score >= 70),
        },
        total_probability=1,
        improvement_target=target,
        improvement_probability=sum(p for score, p in outcomes if score > target),
        expected_positive_gain=sum(max(0, score - target) * p for score, p in outcomes),
    )


def _decide(level=22, target=25, *, score=15, projection=None, **inputs):
    return UpgradeDecisionEngine().decide(
        _profile(),
        _score(score),
        projection or _projection(level, target),
        DecisionInputs(level, existing_gear_score=target, **inputs),
    )


def test_actual_screenshot_candidate_can_beat_weak_target_but_rarely_becomes_usable():
    profile = _profile()
    scorer = EchoScoringEngine()
    stats = (
        ObservedStat("生命", 510, "", 1),
        ObservedStat("共鳴技能傷害加成", 9.4, "%", 1),
        ObservedStat("防禦", 60, "", 1),
        ObservedStat("普攻傷害加成", 7.1, "%", 1),
    )
    score = scorer.score(profile, stats)
    projection = scorer.enumerate_remaining(profile, stats, improvement_target=25.04)
    result = UpgradeDecisionEngine().decide(
        profile, score, projection, DecisionInputs(22, existing_gear_score=25.04)
    )
    assert score.score == pytest.approx(14.93, abs=0.01)
    assert projection.expected_score == pytest.approx(23.8, abs=0.1)
    assert projection.maximum_score == pytest.approx(55.7, abs=0.1)
    assert projection.improvement_probability == pytest.approx(1 / 3, abs=1e-5)
    assert 0 < projection.probabilities["usable"] < 0.01
    assert result.action is UpgradeAction.STOP
    assert "weak_baseline_quality_probability_too_low" in result.reason_codes
    assert "55.0 分" in result.reason and "41,500" in result.reason
    assert result.probabilities["improve_existing"] == projection.improvement_probability


def test_beating_current_weak_gear_does_not_exempt_quality_screening():
    projection = _projection(outcomes=((31, 0.70), (40, 0.29), (70, 0.01)))
    assert projection.improvement_probability == pytest.approx(1)
    result = _decide(score=31, projection=projection)
    assert result.action is UpgradeAction.STOP
    assert "weak_baseline_quality_probability_too_low" in result.reason_codes


def test_candidate_with_good_upside_can_continue_despite_low_mean_and_passing_chance():
    projection = _projection()
    assert projection.expected_score < _profile().thresholds.usable
    assert projection.probabilities["passing"] == 0
    result = _decide(projection=projection)
    assert result.action is UpgradeAction.CONTINUE
    assert result.suggested_level == 25
    assert "weak_baseline_quality_probability_sufficient" in result.reason_codes
    assert "replacement_probability_sufficient" in result.reason_codes
    assert "replacement_gain_sufficient" in result.reason_codes


@pytest.mark.parametrize("level", [5, 9, 10, 14])
def test_quality_floor_does_not_reject_early_bounded_exploration(level):
    projection = _projection(level, outcomes=((10, 0.7), (35, 0.3)))
    result = _decide(level, projection=projection)
    assert projection.probabilities["usable"] == 0
    assert result.action is UpgradeAction.CONTINUE
    assert result.suggested_level == 15
    assert "weak_baseline_quality_considered" not in result.reason_codes


@pytest.mark.parametrize("level", [15, 19, 20, 22, 24])
def test_quality_screen_starts_at_three_slots_and_uses_exact_remaining_cost(level):
    engine = UpgradeDecisionEngine()
    inputs = DecisionInputs(level, existing_gear_score=25)
    required = engine._required_continue_chance(inputs, 15, _profile())
    projection = _projection(level)
    exact = _decide(level, projection=replace(projection, probabilities={"usable": required}))
    below = _decide(
        level, projection=replace(projection, probabilities={"usable": required - 1e-6})
    )
    assert exact.action is UpgradeAction.CONTINUE
    assert below.action is UpgradeAction.STOP
    assert "weak_baseline_quality_probability_too_low" in below.reason_codes


def test_quality_probability_reflects_supply_and_future_cost_not_sunk_materials():
    # 32% is adequate at +22, but not +20 or with abundant alternatives.
    projection = _projection(outcomes=((15, 0.68), (60, 0.32)))
    assert _decide(20, projection=projection).action is UpgradeAction.STOP
    assert _decide(22, projection=projection).action is UpgradeAction.CONTINUE
    results = [
        _decide(22, projection=projection, embryo_supply=supply).action
        for supply in (EmbryoSupply.SCARCE, EmbryoSupply.NORMAL, EmbryoSupply.ABUNDANT)
    ]
    assert results == [UpgradeAction.CONTINUE, UpgradeAction.CONTINUE, UpgradeAction.STOP]


@pytest.mark.parametrize("level", [15, 20, 22])
def test_unreachable_quality_stops_even_when_improvement_is_easy(level):
    projection = _projection(level, outcomes=((30, 0.5), (40, 0.5)))
    result = _decide(level, projection=projection)
    assert result.action is UpgradeAction.STOP
    assert "weak_baseline_quality_unreachable" in result.reason_codes


@pytest.mark.parametrize("chance", [None, -0.01, 1.01, float("nan")])
def test_missing_or_invalid_quality_probability_is_not_invented(chance):
    probabilities = {} if chance is None else {"usable": chance}
    result = _decide(projection=replace(_projection(), probabilities=probabilities))
    assert result.action is UpgradeAction.MANUAL_CONFIRMATION
    assert result.reason_codes == ("weak_baseline_quality_probability_unavailable",)
    assert result.requires_manual_confirmation


@pytest.mark.parametrize("target,score", [(55, 15), (60, 15), (25, 55), (25, 60)])
def test_strong_baseline_or_already_usable_candidate_needs_no_extra_quality_probability(
    target, score
):
    projection = replace(
        _projection(target=target, outcomes=((65, 0.5), (80, 0.5))), probabilities={}
    )
    result = _decide(target=target, score=score, projection=projection)
    assert result.action is UpgradeAction.CONTINUE
    assert "weak_baseline_quality_considered" not in result.reason_codes


@pytest.mark.parametrize("field", ["improvement_probability", "expected_positive_gain"])
def test_sufficient_quality_does_not_bypass_target_probability_or_gain(field):
    result = _decide(projection=replace(_projection(), **{field: 0.001}))
    assert result.action is UpgradeAction.STOP
    assert "weak_baseline_quality_probability_sufficient" in result.reason_codes
    assert (
        "replacement_probability_too_low"
        if field == "improvement_probability"
        else "replacement_gain_too_small"
    ) in result.reason_codes


def test_uncertain_or_missing_unlocked_stats_still_block_and_maxed_echo_never_upgrades():
    engine = UpgradeDecisionEngine()
    profile = _profile()
    projection = _projection()
    inputs = DecisionInputs(22, existing_gear_score=25)
    for score, projected in (
        (_score(None), projection),
        (replace(_score(), requires_manual_confirmation=True), projection),
        (_score(), replace(projection, requires_manual_confirmation=True)),
        (_score(), replace(projection, remaining_slots=2)),
    ):
        result = engine.decide(profile, score, projected, inputs)
        assert result.action is UpgradeAction.MANUAL_CONFIRMATION
        assert "weak_baseline_quality_considered" not in result.reason_codes
    maxed = _decide(25, projection=_projection(25, outcomes=((15, 1),)))
    assert maxed.action is UpgradeAction.MAXED
    assert maxed.suggested_level is None


@pytest.mark.parametrize("version", [None, LEGACY_ROLL_VERSION])
def test_both_probability_models_provide_quality_floor_probability(version):
    profile = _profile()
    if version is not None:
        profile = replace(profile, roll_data_version=version)
    scorer = EchoScoringEngine()
    stats = tuple(
        ObservedStat(name, value, unit, 1)
        for name, value, unit in (
            ("生命", 510, ""),
            ("共鳴技能傷害加成", 9.4, "%"),
            ("防禦", 60, ""),
            ("普攻傷害加成", 7.1, "%"),
        )
    )
    projection = scorer.enumerate_remaining(profile, stats, improvement_target=25)
    assert "usable" in projection.probabilities
    result = UpgradeDecisionEngine().decide(
        profile, scorer.score(profile, stats), projection, DecisionInputs(22, 25)
    )
    assert result.action is not UpgradeAction.MANUAL_CONFIRMATION


def test_previous_policy_stays_bundled_without_retroactive_quality_rule():
    old = json.loads(
        files("wuwa_assistant.data.scoring")
        .joinpath("decision-policy.v4.json")
        .read_text(encoding="utf-8")
    )
    assert old["data_version"] == "upgrade-decision-policy-2026.09-v4"
    assert "weak_baseline_quality" not in old


def test_quality_floor_follows_editable_profile_instead_of_hard_coded_grade():
    profile = _profile()
    relaxed = replace(profile, thresholds=replace(profile.thresholds, usable=40))
    projection = replace(
        _projection(15, outcomes=((15, 0.7), (45, 0.3))), probabilities={"usable": 0.3}
    )
    engine = UpgradeDecisionEngine()
    inputs = DecisionInputs(15, existing_gear_score=25)
    result = engine.decide(relaxed, _score(), projection, inputs)
    assert result.action is UpgradeAction.CONTINUE
    assert "可用 40.0 分" in result.reason
    result = engine.decide(profile, _score(), projection, inputs)
    assert result.action is UpgradeAction.STOP
    assert "weak_baseline_quality_unreachable" in result.reason_codes


@pytest.mark.parametrize(
    "quality",
    [
        None,
        {},
        {"minimum_level": 10, "threshold": "usable"},
        {"minimum_level": 15, "threshold": "passing"},
    ],
)
def test_quality_policy_rejects_early_or_passing_threshold_configuration(monkeypatch, quality):
    from wuwa_assistant.scoring import data

    raw = json.loads(
        files("wuwa_assistant.data.scoring")
        .joinpath("decision-policy.v6.json")
        .read_text(encoding="utf-8")
    )
    raw["weak_baseline_quality"] = quality
    monkeypatch.setattr(data, "_load_json_resource", lambda name: raw)
    with pytest.raises(ValueError, match="weak-baseline quality"):
        data.load_decision_policy()
