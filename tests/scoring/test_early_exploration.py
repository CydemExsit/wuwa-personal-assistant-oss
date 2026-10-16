"""Investment decisions against the selected equipment, not generic grade labels."""

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
    ScoreThresholds,
    UpgradeAction,
    UpgradeDecisionEngine,
    load_scoring_catalog,
)
from wuwa_assistant.scoring.upgrade_costs import upgrade_cost_to_level


def _score(value=5.0):
    return EchoScoreBreakdown("generic_dps_v1", 3, value, None, (), 0, 1)


def _distribution(level=10, target=50.0, outcomes=((30.0, 0.2), (60.0, 0.6), (90.0, 0.2))):
    return ProjectionSummary(
        assumptions_version="test-distribution",
        remaining_slots=5 - level // 5,
        outcome_count=len(outcomes),
        minimum_score=min(s for s, p in outcomes),
        maximum_score=max(s for s, p in outcomes),
        expected_score=sum(s * p for s, p in outcomes),
        probabilities={"passing": 0},
        total_probability=1,
        improvement_target=target,
        improvement_probability=None
        if target is None
        else sum(p for s, p in outcomes if s > target),
        expected_positive_gain=None
        if target is None
        else sum(max(0, s - target) * p for s, p in outcomes),
    )


def _decision(level=10, target=50, *, projection=None, **input_kwargs):
    return UpgradeDecisionEngine().decide(
        load_scoring_catalog().profiles[0],
        _score(),
        projection or _distribution(level, target),
        DecisionInputs(level=level, existing_gear_score=target, **input_kwargs),
    )


@pytest.mark.parametrize("level", [5, 9, 10, 14])
def test_before_three_confirmed_slots_explores_through_fifteen_without_generic_rejection(level):
    # Even a strict generic passing label must not reject one/two known stats.
    projection = _distribution(level, None, outcomes=((5.0, 0.9), (10.0, 0.1)))
    result = _decision(level, None, projection=projection)
    assert result.action is UpgradeAction.CONTINUE
    assert result.suggested_level == 15
    assert "early_exploration_without_target" in result.reason_codes
    assert f"+{level} → +15" in result.reason
    assert result.to_dict()["suggested_level"] == 15


def test_three_confirmed_slots_is_real_reassessment_checkpoint():
    early = _decision(
        10, None, projection=_distribution(10, None, outcomes=((5.0, 0.9), (10.0, 0.1)))
    )
    reassessed = _decision(
        15, None, projection=_distribution(15, None, outcomes=((5.0, 0.9), (10.0, 0.1)))
    )
    assert early.action is UpgradeAction.CONTINUE
    assert reassessed.action is UpgradeAction.STOP


@pytest.mark.parametrize("level", [5, 10, 15, 20])
def test_missing_unlocked_stats_never_become_future_potential(level):
    incomplete = replace(_distribution(level), remaining_slots=6 - level // 5)
    result = _decision(level, projection=incomplete)
    assert result.action is UpgradeAction.MANUAL_CONFIRMATION
    assert result.reason_codes == ("unconfirmed_unlocked_slots",)
    assert result.suggested_level is None


def test_target_branch_is_independent_of_passing_grades_and_qualified_flags():
    profile = load_scoring_catalog().profiles[0]
    projection = _distribution(15, 60)
    actions = []
    for thresholds in (ScoreThresholds(50, 60, 70, 80), ScoreThresholds(50, 80, 90, 99)):
        for qualified, breadth in ((False, True), (True, False), (True, True)):
            result = UpgradeDecisionEngine().decide(
                replace(profile, thresholds=thresholds),
                _score(40),
                projection,
                DecisionInputs(
                    level=15,
                    existing_gear_score=60,
                    character_already_qualified=qualified,
                    breadth_first_progression=breadth,
                ),
            )
            actions.append((result.action, result.reason_codes))
    assert len(set(actions)) == 1


def test_higher_selected_target_never_promotes_same_score_distribution():
    targets = [40, 60, 80, 89, 90]
    results = [_decision(target=t) for t in targets]
    assert [r.action for r in results] == [
        UpgradeAction.CONTINUE,
        UpgradeAction.CONTINUE,
        UpgradeAction.CONTINUE,
        UpgradeAction.STOP,
        UpgradeAction.CANNOT_SURPASS,
    ]
    assert "replacement_gain_too_small" in results[-2].reason_codes
    assert "replacement_probability_sufficient" in results[-2].reason_codes


def test_early_extreme_target_can_stop_without_blanket_three_slot_rule():
    extreme = _decision(5, 89.99, projection=_distribution(5, 89.99))
    impossible = _decision(5, 90, projection=_distribution(5, 90))
    assert extreme.action is UpgradeAction.STOP
    assert "replacement_gain_too_small" in extreme.reason_codes
    assert impossible.action is UpgradeAction.CANNOT_SURPASS


def test_cost_falls_with_level_progress_so_only_future_materials_are_charged():
    # Identical two known rolls/distribution, only EXP already paid differs.
    projection = replace(
        _distribution(10), improvement_probability=0.04, expected_positive_gain=0.15
    )
    early = _decision(10, projection=projection)
    later = _decision(14, projection=projection)
    assert early.action is UpgradeAction.STOP
    assert later.action is UpgradeAction.CONTINUE
    assert "23,100" in early.reason
    assert "5,700" in later.reason
    engine = UpgradeDecisionEngine()
    assert engine._required_expected_gain(DecisionInputs(14, 50)) < engine._required_expected_gain(
        DecisionInputs(10, 50)
    )


def test_supply_changes_exploration_risk_in_monotonic_direction():
    projection = replace(
        _distribution(), improvement_probability=0.055, expected_positive_gain=0.32
    )
    results = [
        _decision(projection=projection, embryo_supply=s)
        for s in (EmbryoSupply.SCARCE, EmbryoSupply.NORMAL, EmbryoSupply.ABUNDANT)
    ]
    assert [r.action for r in results] == [
        UpgradeAction.CONTINUE,
        UpgradeAction.CONTINUE,
        UpgradeAction.STOP,
    ]
    assert all("+10 → +15" in r.reason for r in results)


@pytest.mark.parametrize(
    "chance,gain,expected",
    [
        (0.02, 0.05, UpgradeAction.STOP),
        (0.8, 0.01, UpgradeAction.STOP),
        (0.5, 1.0, UpgradeAction.CONTINUE),
    ],
)
def test_expensive_last_node_needs_both_probability_and_meaningful_gain(chance, gain, expected):
    p = replace(
        _distribution(20, 70.68), improvement_probability=chance, expected_positive_gain=gain
    )
    r = _decision(20, 70.68, projection=p)
    assert r.action is expected
    assert "63,500" in r.reason


def test_actual_weak_two_roll_candidate_gets_exploration_for_attainable_target():
    profile = load_scoring_catalog().profiles[0]
    scorer = EchoScoringEngine()
    stats = (ObservedStat("生命", 320, "", 1), ObservedStat("防禦", 40, "", 1))
    score = scorer.score(profile, stats)
    decisions = []
    for target in (30, 50, 70, 90):
        projection = scorer.enumerate_remaining(profile, stats, improvement_target=target)
        decisions.append(
            UpgradeDecisionEngine().decide(
                profile, score, projection, DecisionInputs(10, existing_gear_score=target)
            )
        )
    assert [d.action for d in decisions] == [
        UpgradeAction.CONTINUE,
        UpgradeAction.CONTINUE,
        UpgradeAction.STOP,
        UpgradeAction.STOP,
    ]
    assert decisions[0].suggested_level == decisions[1].suggested_level == 15


def test_three_weak_rolls_need_usable_potential_beyond_improving_weak_baseline():
    profile = load_scoring_catalog().profiles[0]
    scorer = EchoScoringEngine()
    stats = (
        ObservedStat("生命", 320, "", 1),
        ObservedStat("防禦", 40, "", 1),
        ObservedStat("生命", 6.4, "%", 1),
    )
    projection = scorer.enumerate_remaining(profile, stats, improvement_target=30)
    assert projection.probabilities["passing"] < 0.01
    result = UpgradeDecisionEngine().decide(
        profile,
        scorer.score(profile, stats),
        projection,
        DecisionInputs(15, existing_gear_score=30),
    )
    assert result.action is UpgradeAction.STOP
    assert "weak_baseline_quality_probability_too_low" in result.reason_codes
    assert result.suggested_level is None


def test_fifteen_exploration_cost_is_exact_and_old_policy_remains_bundled():
    cost = upgrade_cost_to_level(0, 15)
    assert (cost.experience, cost.tuners, cost.shell_credits) == (39600, 30, 9960)
    assert cost.exp_fraction_of_full == pytest.approx(39600 / 142600)
    old = json.loads(
        files("wuwa_assistant.data.scoring")
        .joinpath("decision-policy.v3.json")
        .read_text(encoding="utf-8")
    )
    assert old["data_version"] == "upgrade-decision-policy-2026.09-v3"
    assert "early_exploration" not in old


def test_actual_single_low_hp_roll_is_not_rejected_before_exploration():
    profile = load_scoring_catalog().profiles[0]
    scorer = EchoScoringEngine()
    stats = (ObservedStat("生命", 320, "", 1),)
    score = scorer.score(profile, stats)
    projection = scorer.enumerate_remaining(profile, stats, improvement_target=50)
    result = UpgradeDecisionEngine().decide(
        profile, score, projection, DecisionInputs(5, existing_gear_score=50)
    )
    assert score.score < 1
    assert projection.improvement_probability > 0.4
    assert result.action is UpgradeAction.CONTINUE
    assert result.suggested_level == 15
    assert "35,200" in result.reason


def test_probability_and_gain_boundaries_each_control_target_decision():
    profile = load_scoring_catalog().profiles[0]
    engine = UpgradeDecisionEngine()
    inputs = DecisionInputs(10, existing_gear_score=50)
    chance = engine._required_continue_chance(inputs, 5, profile)
    gain = engine._required_expected_gain(inputs)
    projection = replace(
        _distribution(), improvement_probability=chance, expected_positive_gain=gain
    )
    exact = _decision(projection=projection)
    less_chance = _decision(projection=replace(projection, improvement_probability=chance - 1e-6))
    less_gain = _decision(projection=replace(projection, expected_positive_gain=gain - 1e-6))
    assert exact.action is UpgradeAction.CONTINUE
    assert less_chance.action is less_gain.action is UpgradeAction.STOP
    assert "replacement_probability_too_low" in less_chance.reason_codes
    assert "replacement_gain_too_small" in less_gain.reason_codes
