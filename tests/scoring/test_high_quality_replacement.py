from dataclasses import replace

import pytest

from wuwa_assistant.scoring import (
    DecisionInputs,
    EmbryoSupply,
    ScoreThresholds,
    UpgradeAction,
    UpgradeDecisionEngine,
    load_decision_policy,
    load_scoring_catalog,
)

from .test_decision import _breakdown, _projection


def _decide(score=75.0, baseline=85.0, passing=70.0, chance=0.20, gain=0.40, **changes):
    profile = replace(
        load_scoring_catalog().profiles[0], thresholds=ScoreThresholds(55, passing, 95, 100)
    )
    projection = replace(
        _projection(score, 99, 1, remaining=1),
        improvement_target=baseline,
        improvement_probability=chance,
        expected_positive_gain=gain,
        **changes,
    )
    return UpgradeDecisionEngine().decide(
        profile,
        _breakdown(score),
        projection,
        DecisionInputs(level=20, existing_gear_score=baseline),
    )


@pytest.mark.parametrize(
    "score,baseline,passing,relaxed",
    [
        (75, 85, 70, True),  # Inclusive ten-point gap.
        (74.99, 85, 70, False),
        (85, 85, 70, True),
        (85.01, 85, 70, False),  # Already better uses ordinary replacement rules.
        (70, 80, 70, True),
        (69.99, 75, 70, False),
        (69, 69, 70, False),
        (75, 85, 80, False),  # Actual character threshold, not a fixed 70.
        (80, 90, 80, True),
    ],
)
def test_only_current_high_quality_scores_within_ten_points_relax(
    score, baseline, passing, relaxed
):
    result = _decide(score, baseline, passing)
    assert ("high_quality_close_target_relaxation" in result.reason_codes) is relaxed
    assert result.action is (UpgradeAction.CONTINUE if relaxed else UpgradeAction.STOP)
    if relaxed:
        assert result.suggested_level == 25
        assert "門檻減半" in result.reason


@pytest.mark.parametrize(
    "chance,gain,expected",
    [
        (0.175, 0.375, UpgradeAction.CONTINUE),
        (0.1749, 0.375, UpgradeAction.STOP),
        (0.175, 0.3749, UpgradeAction.STOP),
        (0, 0, UpgradeAction.STOP),
    ],
)
def test_both_cost_adjusted_requirements_are_halved_but_not_waived(chance, gain, expected):
    assert _decide(chance=chance, gain=gain).action is expected


@pytest.mark.parametrize("supply", list(EmbryoSupply))
@pytest.mark.parametrize("level", [15, 20, 22, 24])
def test_relaxation_preserves_remaining_cost_and_supply_adjustments(supply, level):
    profile = load_scoring_catalog().profiles[0]
    inputs = DecisionInputs(level=level, existing_gear_score=85, embryo_supply=supply)
    engine = UpgradeDecisionEngine()
    chance = engine._required_continue_chance(inputs, 75, profile) / 2
    gain = engine._required_expected_gain(inputs) / 2
    projection = replace(
        _projection(75, 99, 1, remaining=5 - level // 5),
        improvement_target=85,
        improvement_probability=chance,
        expected_positive_gain=gain,
    )
    result = engine.decide(profile, _breakdown(75), projection, inputs)
    assert result.action is UpgradeAction.CONTINUE
    assert result.suggested_level == (level // 5 + 1) * 5
    assert (
        engine.decide(
            profile,
            _breakdown(75),
            replace(projection, expected_positive_gain=gain - 0.0001),
            inputs,
        ).action
        is UpgradeAction.STOP
    )


def test_ceiling_and_missing_probability_still_block_high_quality_candidates():
    assert _decide(maximum_score=85).action is UpgradeAction.CANNOT_SURPASS
    assert _decide(chance=None).action is UpgradeAction.MANUAL_CONFIRMATION
    assert _decide(requires_manual_confirmation=True).action is UpgradeAction.MANUAL_CONFIRMATION
    assert _decide(remaining_slots=2).action is UpgradeAction.MANUAL_CONFIRMATION


def test_high_quality_policy_is_versioned():
    policy = load_decision_policy()
    assert policy.high_quality_gap == 10
    assert policy.high_quality_factor == 0.5


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"threshold": "usable"},
        {"threshold": "passing", "maximum_gap": float("nan"), "requirements_factor": 0.5},
        {"threshold": "passing", "maximum_gap": 10, "requirements_factor": 0},
    ],
)
def test_invalid_relaxation_policy_rejected(monkeypatch, value):
    from wuwa_assistant.scoring import data

    raw = data._load_json_resource("decision-policy.v6.json")
    raw["high_quality_close_target"] = value
    monkeypatch.setattr(data, "_load_json_resource", lambda _: raw)
    with pytest.raises(ValueError, match="high-quality"):
        data.load_decision_policy()
