from dataclasses import replace

import pytest

from wuwa_assistant.scoring import (
    DecisionInputs,
    EchoScoringEngine,
    ObservedStat,
    UpgradeAction,
    UpgradeDecisionEngine,
    load_scoring_catalog,
)
from wuwa_assistant.scoring.character_guides import STAT_LABELS
from wuwa_assistant.scoring.data import unit_for_stat
from wuwa_assistant.scoring.models import StatKey


def _four_stats():
    return tuple(
        ObservedStat(name, value, unit, 1)
        for name, value, unit in (
            ("攻擊", 7.9, "%"),
            ("攻擊", 50, ""),
            ("暴擊", 8.1, "%"),
            ("生命", 430, ""),
        )
    )


def test_replacement_probability_and_gain_match_independent_last_roll_enumeration():
    catalog = load_scoring_catalog()
    profile = catalog.profiles[0]
    engine = EchoScoringEngine()
    stats = _four_stats()
    outcomes = []
    labels = {
        StatKey.CRIT_DAMAGE: "暴擊傷害",
        StatKey.HP_PCT: "生命",
        StatKey.DEFENSE_PCT: "防禦",
        StatKey.FLAT_DEFENSE: "防禦",
        StatKey.BASIC_ATTACK_DAMAGE: "普攻傷害加成",
        StatKey.HEAVY_ATTACK_DAMAGE: "重擊傷害加成",
        StatKey.RESONANCE_SKILL_DAMAGE: "共鳴技能傷害加成",
        StatKey.RESONANCE_LIBERATION_DAMAGE: "共鳴解放傷害加成",
    }
    used = {StatKey.ATTACK_PCT, StatKey.FLAT_ATTACK, StatKey.CRIT_RATE, StatKey.FLAT_HP}
    for stat in set(StatKey) - used:
        for value, probability in zip(
            catalog.rolls.values_for(stat), catalog.rolls.probabilities_for(stat), strict=True
        ):
            score = engine.score(
                profile,
                (
                    *stats,
                    ObservedStat(
                        labels.get(stat, STAT_LABELS[stat]), value, unit_for_stat(stat), 1
                    ),
                ),
            ).score
            assert score is not None
            outcomes.append((score, probability / 9))
    # Equal scores do not improve existing equipment; mass must use strict >.
    baseline = outcomes[0][0]
    result = engine.enumerate_remaining(profile, stats, improvement_target=baseline)
    assert result.improvement_probability == pytest.approx(
        sum(p for s, p in outcomes if s > baseline), abs=1e-6
    )
    assert result.expected_positive_gain == pytest.approx(
        sum(max(0, s - baseline) * p for s, p in outcomes), abs=1e-4
    )
    assert result.expected_score == pytest.approx(round(sum(s * p for s, p in outcomes), 2))
    impossible = engine.enumerate_remaining(profile, stats, improvement_target=101)
    assert impossible.improvement_probability == impossible.expected_positive_gain == 0


def test_last_expensive_node_rejects_screenshot_shaped_long_shot():
    profile = load_scoring_catalog().profiles[0]
    scorer = EchoScoringEngine()
    score = replace(scorer.score(profile, _four_stats()), score=38.79)
    projection = replace(
        scorer.enumerate_remaining(profile, _four_stats(), improvement_target=70.68),
        minimum_score=38.79,
        maximum_score=76.83,
        expected_score=47.4,
        improvement_probability=0.02,
        expected_positive_gain=0.07,
    )
    decision = UpgradeDecisionEngine().decide(
        profile,
        score,
        projection,
        DecisionInputs(level=20, existing_gear_score=70.68, breadth_first_progression=False),
    )
    assert decision.action is UpgradeAction.STOP
    assert "replacement_probability_too_low" in decision.reason_codes
    assert "2.0%" in decision.reason and "47.40" in decision.reason
    assert "63,500" in decision.reason and "8,350" in decision.reason


def test_already_generically_qualified_candidate_does_not_override_stronger_baseline():
    profile = load_scoring_catalog().profiles[0]
    scorer = EchoScoringEngine()
    score = replace(scorer.score(profile, _four_stats()), score=72)
    projection = replace(
        scorer.enumerate_remaining(profile, _four_stats(), improvement_target=90),
        maximum_score=96,
        improvement_probability=0.01,
        expected_positive_gain=0.02,
    )
    decision = UpgradeDecisionEngine().decide(
        profile, score, projection, DecisionInputs(level=22, existing_gear_score=90)
    )
    assert decision.action is UpgradeAction.STOP
    assert "41,500" in decision.reason


def test_same_score_distribution_charges_only_remaining_cost_at_exact_level():
    profile = load_scoring_catalog().profiles[0]
    engine = UpgradeDecisionEngine()
    start = engine._required_continue_chance(DecisionInputs(level=20), 40, profile)
    later = engine._required_continue_chance(DecisionInputs(level=24), 40, profile)
    assert later < start
