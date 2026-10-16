from __future__ import annotations

import pytest

from wuwa_assistant.scoring import (
    DecisionInputs,
    EchoScoreBreakdown,
    EchoScoringEngine,
    EmbryoSupply,
    ObservedStat,
    ProjectionSummary,
    ScoreBand,
    ScoringContext,
    UpgradeAction,
    UpgradeDecisionEngine,
    load_scoring_catalog,
)


def _breakdown(
    score: float | None,
    *,
    manual: bool = False,
) -> EchoScoreBreakdown:
    return EchoScoreBreakdown(
        profile_id="generic_dps_v1",
        profile_version=2,
        score=score,
        band=ScoreBand.TRANSITIONAL if score is not None else None,
        lines=(),
        earned_weight_points=0.0,
        maximum_weight_points=1.0,
        requires_manual_confirmation=manual,
    )


def _projection(
    minimum: float | None,
    maximum: float | None,
    passing: float,
    *,
    manual: bool = False,
    remaining: int = 2,
) -> ProjectionSummary:
    return ProjectionSummary(
        assumptions_version="echo-rolls-2026.08-v1",
        remaining_slots=remaining,
        outcome_count=4 if not manual else 0,
        minimum_score=minimum,
        maximum_score=maximum,
        expected_score=maximum,
        probabilities={"passing": passing},
        total_probability=1.0 if not manual else 0.0,
        requires_manual_confirmation=manual,
    )


def test_manual_confirmation_wins_for_uncertain_data() -> None:
    profile = load_scoring_catalog().profile("generic_dps_v1")
    result = UpgradeDecisionEngine().decide(
        profile,
        _breakdown(None, manual=True),
        _projection(None, None, 0.0, manual=True),
        DecisionInputs(level=10),
    )

    assert result.action is UpgradeAction.MANUAL_CONFIRMATION
    assert result.requires_manual_confirmation
    assert result.assumptions_version == (
        "roll:echo-rolls-2026.08-v1|decision:upgrade-decision-policy-2026.10-v6"
    )


def test_max_level_and_qualified_actions() -> None:
    profile = load_scoring_catalog().profile("generic_dps_v1")
    engine = UpgradeDecisionEngine()

    maxed = engine.decide(
        profile,
        _breakdown(90.0),
        _projection(90.0, 90.0, 1.0, remaining=0),
        DecisionInputs(level=25),
    )
    qualified = engine.decide(
        profile,
        _breakdown(72.0),
        _projection(75.0, 95.0, 1.0),
        DecisionInputs(level=15),
    )

    assert maxed.action is UpgradeAction.MAXED
    assert qualified.action is UpgradeAction.QUALIFIED


@pytest.mark.parametrize("level", [5, 10, 15, 20, 24])
def test_already_qualified_self_cultivation_has_explicit_full_level_checkpoint(level):
    profile = load_scoring_catalog().profile("generic_dps_v1")
    result = UpgradeDecisionEngine().decide(
        profile,
        _breakdown(72),
        _projection(72, 95, 1, remaining=5 - level // 5),
        DecisionInputs(level=level),
    )
    assert result.action is UpgradeAction.QUALIFIED
    assert result.suggested_level == 25


def test_existing_gear_ceiling_prevents_wasted_upgrade() -> None:
    profile = load_scoring_catalog().profile("generic_dps_v1")
    result = UpgradeDecisionEngine().decide(
        profile,
        _breakdown(62.0),
        _projection(65.0, 79.0, 0.3),
        DecisionInputs(level=15, existing_gear_score=80.0, replacement_margin=1.0),
    )

    assert result.action is UpgradeAction.CANNOT_SURPASS
    assert "cannot_reach_replacement_target" in result.reason_codes


def test_continue_respects_breadth_first_gap_and_scarce_candidates() -> None:
    profile = load_scoring_catalog().profile("generic_dps_v1")
    result = UpgradeDecisionEngine().decide(
        profile,
        _breakdown(50.0),
        _projection(52.0, 82.0, 0.06, remaining=3),
        DecisionInputs(
            level=10,
            character_already_qualified=False,
            breadth_first_progression=True,
            embryo_supply=EmbryoSupply.SCARCE,
            same_type_candidates=1,
        ),
    )

    assert result.action is UpgradeAction.CONTINUE
    assert "early_exploration_without_target" in result.reason_codes
    assert result.suggested_level == 15


def test_stop_when_passing_is_unreachable_or_probability_too_low() -> None:
    profile = load_scoring_catalog().profile("generic_dps_v1")
    engine = UpgradeDecisionEngine()

    unreachable = engine.decide(
        profile,
        _breakdown(45.0),
        _projection(48.0, 64.0, 0.0, remaining=1),
        DecisionInputs(level=20),
    )
    low_probability = engine.decide(
        profile,
        _breakdown(50.0),
        _projection(52.0, 80.0, 0.03),
        DecisionInputs(
            level=15,
            character_already_qualified=True,
            embryo_supply=EmbryoSupply.ABUNDANT,
            same_type_candidates=10,
        ),
    )

    assert unreachable.action is UpgradeAction.STOP
    assert unreachable.reason_codes == ("passing_unreachable",)
    assert low_probability.action is UpgradeAction.STOP
    assert low_probability.reason_codes == ("passing_probability_too_low",)


def test_legacy_calibrated_actual_shaped_upgrade_cases() -> None:
    catalog = load_scoring_catalog()
    profile = catalog.profile("generic_dps_v1")
    from dataclasses import replace

    from wuwa_assistant.scoring.data import LEGACY_ROLL_VERSION

    profile = replace(profile, roll_data_version=LEGACY_ROLL_VERSION)
    scorer = EchoScoringEngine(catalog.rolls)
    decision_engine = UpgradeDecisionEngine()
    context = ScoringContext(conditions={"uses_resonance_skill": True})

    def stat(name: str, value: float, unit: str = "%") -> ObservedStat:
        return ObservedStat(name=name, value=value, unit=unit, confidence=0.99)

    strong_plus_ten = (
        stat("攻擊", 11.6),
        stat("共鳴技能傷害加成", 11.6),
    )
    weak_plus_ten = (
        stat("生命", 320, ""),
        stat("防禦", 40, ""),
    )
    bad_plus_fifteen = (
        stat("生命", 320, ""),
        stat("防禦", 40, ""),
        stat("生命", 6.4),
    )

    def decide(stats: tuple[ObservedStat, ...], level: int) -> UpgradeAction:
        breakdown = scorer.score(profile, stats, context)
        projection = scorer.enumerate_remaining(profile, stats, context)
        return decision_engine.decide(
            profile,
            breakdown,
            projection,
            DecisionInputs(level=level),
        ).action

    assert decide(strong_plus_ten, 10) is UpgradeAction.CONTINUE
    assert decide(weak_plus_ten, 10) is UpgradeAction.CONTINUE
    assert decide(bad_plus_fifteen, 15) is UpgradeAction.STOP


def test_between_node_level_uses_its_tuning_band() -> None:
    profile = load_scoring_catalog().profile("generic_dps_v1")
    result = UpgradeDecisionEngine().decide(
        profile,
        _breakdown(60.0),
        _projection(62.0, 80.0, 0.2, remaining=3),
        DecisionInputs(level=12),
    )

    assert result.action is not UpgradeAction.MANUAL_CONFIRMATION
    assert "+15" in result.reason
