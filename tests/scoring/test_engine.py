from __future__ import annotations

from dataclasses import replace

import pytest

from wuwa_assistant.parsers.lexicon import (
    calibrated_stat_name_confidence,
    match_stat_name,
)
from wuwa_assistant.scoring import (
    EchoScoringEngine,
    ObservedStat,
    ScoringContext,
    StatKey,
    canonical_stat_key,
    load_scoring_catalog,
)


def _stat(name: str, value: float, unit: str = "%", confidence: float = 0.99) -> ObservedStat:
    return ObservedStat(name=name, value=value, unit=unit, confidence=confidence)


def test_exact_mapping_distinguishes_flat_and_percent_attack() -> None:
    assert canonical_stat_key("攻擊", "%") is StatKey.ATTACK_PCT
    assert canonical_stat_key("攻擊百分比", "%") is StatKey.ATTACK_PCT
    assert canonical_stat_key("攻擊", None) is StatKey.FLAT_ATTACK
    assert canonical_stat_key("攻擊力", "%") is None
    assert canonical_stat_key(" 攻擊 ", "%") is StatKey.ATTACK_PCT


def test_score_is_reproducible_and_high_roll_beats_low_roll() -> None:
    catalog = load_scoring_catalog()
    profile = catalog.profile("generic_dps_v1")
    engine = EchoScoringEngine(catalog.rolls)

    low = engine.score(profile, (_stat("暴擊", 6.3), _stat("攻擊", 6.4)))
    high = engine.score(profile, (_stat("暴擊", 10.5), _stat("攻擊", 11.6)))
    repeat = engine.score(profile, (_stat("暴擊", 10.5), _stat("攻擊", 11.6)))

    assert low.score is not None
    assert high.score is not None
    assert high.score > low.score
    assert high == repeat
    assert low.lines[0].roll_tier.key == "low"
    assert high.lines[0].roll_tier.key == "top"


def test_unknown_low_confidence_and_unlisted_roll_require_confirmation() -> None:
    catalog = load_scoring_catalog()
    profile = catalog.profile("generic_dps_v1")
    engine = EchoScoringEngine(catalog.rolls, minimum_confidence=0.85)

    unknown = engine.score(profile, (_stat("攻擊力", 10.1),))
    low_confidence = engine.score(profile, (_stat("暴擊", 7.5, confidence=0.60),))
    invalid_roll = engine.score(profile, (_stat("暴擊", 7.4),))

    assert unknown.score is None and unknown.unknown_stats == ("攻擊力",)
    assert low_confidence.score is None and low_confidence.low_confidence_stats == ("暴擊",)
    assert invalid_roll.score is None and invalid_roll.invalid_rolls == ("暴擊:7.4%",)
    assert unknown.requires_manual_confirmation
    assert low_confidence.requires_manual_confirmation
    assert invalid_roll.requires_manual_confirmation


def test_measured_decorated_name_can_score_but_fuzzy_name_stays_blocked() -> None:
    catalog = load_scoring_catalog()
    profile = catalog.profile("generic_dps_v1")
    engine = EchoScoringEngine(catalog.rolls, minimum_confidence=0.85)

    measured = match_stat_name("+ 攻擊")
    fuzzy = match_stat_name("錯攻擊")
    assert measured is not None and fuzzy is not None
    measured_confidence = calibrated_stat_name_confidence(
        measured,
        ocr_confidence=0.75990,
        value_confidence=0.99999,
    )
    fuzzy_confidence = calibrated_stat_name_confidence(
        fuzzy,
        ocr_confidence=0.99,
        value_confidence=0.99999,
    )

    accepted = engine.score(profile, (_stat("攻擊", 40, "", measured_confidence),))
    blocked = engine.score(profile, (_stat("攻擊", 40, "", fuzzy_confidence),))

    assert accepted.score is not None
    assert accepted.low_confidence_stats == ()
    assert blocked.score is None
    assert blocked.low_confidence_stats == ("攻擊",)


def test_crit_balance_favors_the_deficient_side() -> None:
    catalog = load_scoring_catalog()
    profile = catalog.profile("generic_dps_v1")
    engine = EchoScoringEngine(catalog.rolls)
    stats = (_stat("暴擊", 8.7), _stat("暴擊傷害", 17.4))

    too_much_damage = engine.score(
        profile,
        stats,
        ScoringContext(panel_crit_rate=50.0, panel_crit_damage=250.0),
    )
    too_much_rate = engine.score(
        profile,
        stats,
        ScoringContext(panel_crit_rate=80.0, panel_crit_damage=180.0),
    )

    assert too_much_damage.lines[0].crit_balance_multiplier > 1
    assert too_much_damage.lines[1].crit_balance_multiplier < 1
    assert too_much_rate.lines[0].crit_balance_multiplier < 1
    assert too_much_rate.lines[1].crit_balance_multiplier > 1


def test_required_energy_context_is_visible_and_threshold_changes_weight() -> None:
    catalog = load_scoring_catalog()
    profile = catalog.profile("energy_requirement_v1")
    engine = EchoScoringEngine(catalog.rolls)
    energy = (_stat("共鳴效率", 12.4),)

    missing = engine.score(profile, energy)
    below = engine.score(
        profile,
        energy,
        ScoringContext(current_totals={StatKey.ENERGY_REGEN: 100.0}),
    )
    reached = engine.score(
        profile,
        energy,
        ScoringContext(current_totals={StatKey.ENERGY_REGEN: 120.0}),
    )

    assert missing.score is not None
    assert missing.requires_manual_confirmation
    assert missing.missing_context == ("current_totals.energy_regen",)
    assert below.score is not None and reached.score is not None
    assert below.score > reached.score
    assert below.lines[0].threshold_multiplier == 1.2
    assert reached.lines[0].threshold_multiplier == 0.75


def test_conditional_damage_weight_is_data_driven() -> None:
    catalog = load_scoring_catalog()
    profile = catalog.profile("generic_dps_v1")
    engine = EchoScoringEngine(catalog.rolls)
    stat = (_stat("共鳴技能傷害加成", 10.1),)

    inactive = engine.score(profile, stat)
    active = engine.score(
        profile,
        stat,
        ScoringContext(conditions={"uses_resonance_skill": True}),
    )

    assert inactive.score is not None and active.score is not None
    assert active.score > inactive.score
    assert active.lines[0].base_weight == 0.55
    assert active.lines[0].conditional_multiplier == 1.5
    assert active.lines[0].base_weight * active.lines[0].conditional_multiplier == pytest.approx(
        0.825
    )


def test_opened_slot_quality_normalizes_only_the_scored_stage_and_serializes() -> None:
    catalog = load_scoring_catalog()
    profile = catalog.profile("generic_dps_v1")
    engine = EchoScoringEngine(catalog.rolls)

    strong_plus_ten = engine.score(
        profile,
        (_stat("暴擊", 10.5), _stat("攻擊", 11.6)),
    )
    empty = engine.score(profile, ())
    uncertain = engine.score(profile, (_stat("攻擊", 11.6, confidence=0.40),))

    assert strong_plus_ten.score == 58.97
    assert strong_plus_ten.opened_slot_quality_score == 91.0
    assert strong_plus_ten.to_dict()["opened_slot_quality_score"] == 91.0
    assert empty.opened_slot_quality_score is None
    assert uncertain.score is None
    assert uncertain.opened_slot_quality_score is None


def test_remaining_slot_enumeration_is_deterministic_and_probabilities_ordered() -> None:
    catalog = load_scoring_catalog()
    profile = catalog.profile("generic_dps_v1")
    engine = EchoScoringEngine(catalog.rolls)
    stats = (
        _stat("暴擊", 7.5),
        _stat("暴擊傷害", 15.0),
        _stat("攻擊", 10.1),
        _stat("共鳴效率", 10.0),
    )

    first = engine.enumerate_remaining(profile, stats)
    second = engine.enumerate_remaining(profile, stats)

    assert first == second
    assert first.remaining_slots == 1
    assert first.outcome_count > 1
    assert first.total_probability == 1.0
    assert first.minimum_score is not None
    assert first.expected_score is not None
    assert first.maximum_score is not None
    assert first.minimum_score <= first.expected_score <= first.maximum_score
    assert (
        first.probabilities["usable"]
        >= first.probabilities["passing"]
        >= first.probabilities["perfect"]
        >= first.probabilities["exceptional"]
    )


def test_enumeration_refuses_uncertain_input_instead_of_guessing() -> None:
    catalog = load_scoring_catalog()
    profile = catalog.profile("generic_dps_v1")
    engine = EchoScoringEngine(catalog.rolls)

    projection = engine.enumerate_remaining(profile, (_stat("未知詞條", 9.4),))

    assert projection.requires_manual_confirmation
    assert projection.outcome_count == 0
    assert projection.expected_score is None


def test_zero_value_profile_stats_remain_possible_projection_outcomes() -> None:
    catalog = load_scoring_catalog()
    profile = catalog.profile("generic_dps_v1")
    stats = (
        _stat("暴擊", 7.5),
        _stat("暴擊傷害", 15.0),
        _stat("攻擊", 10.1),
        _stat("共鳴效率", 10.0),
    )
    zero_profile = replace(
        profile,
        weights=tuple(
            replace(weight, weight=0.0) if weight.stat is StatKey.FLAT_HP else weight
            for weight in profile.weights
        ),
    )
    tiny_profile = replace(
        profile,
        weights=tuple(
            replace(weight, weight=1e-9) if weight.stat is StatKey.FLAT_HP else weight
            for weight in profile.weights
        ),
    )
    engine = EchoScoringEngine(catalog.rolls)

    zero = engine.enumerate_remaining(zero_profile, stats)
    tiny = engine.enumerate_remaining(tiny_profile, stats)

    assert zero.outcome_count == tiny.outcome_count
    assert zero.total_probability == tiny.total_probability == 1.0
