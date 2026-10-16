from __future__ import annotations

from dataclasses import replace
from itertools import combinations, pairwise, product

import pytest

from wuwa_assistant.scoring import EchoScoringEngine, ObservedStat, StatKey, load_scoring_catalog
from wuwa_assistant.scoring.calibration import probability_reference
from wuwa_assistant.scoring.character_guides import guide_for_character
from wuwa_assistant.scoring.data import LEGACY_ROLL_VERSION, load_roll_data


def test_reference_has_headroom_instead_of_saturating_all_high_quality_gear() -> None:
    from wuwa_assistant.scoring.calibration import score_points

    assert score_points(2, 2, 3) == 90
    assert score_points(2.1, 2, 3) == 91
    assert score_points(2.9, 2, 3) == 99
    assert score_points(3, 2, 3) == 100
    scores = [score_points(i / 100, 2, 3) for i in range(301)]
    assert all(a < b for a, b in pairwise(scores))


def test_disclosed_probabilities_cover_all_types_and_rounding_only() -> None:
    data = load_roll_data()
    assert data.probability_assumption.is_official
    assert set(data.roll_probabilities) == set(StatKey)
    for stat in StatKey:
        probabilities = data.probabilities_for(stat)
        assert len(probabilities) == len(data.roll_values[stat])
        assert sum(probabilities) == pytest.approx(1)
    assert sum(data.probabilities_for(StatKey.CRIT_RATE)[:3]) == pytest.approx(0.7, abs=1e-6)
    assert data.probabilities_for(StatKey.FLAT_ATTACK)[1] == pytest.approx(0.524272)
    assert data.probabilities_for(StatKey.ENERGY_REGEN)[-1] == pytest.approx(0.029126, abs=1e-7)


def test_reference_distribution_matches_independent_subset_enumeration() -> None:
    options = tuple(((i * 0.05, 0.8), (i * 0.15, 0.2)) for i in range(1, 7))
    outcomes = []
    for selected in combinations(options, 5):
        for rows in product(*selected):
            probability = 1 / 6
            for _, mass in rows:
                probability *= mass
            outcomes.append((sum(value for value, _ in rows), probability))
    mass = 0.0
    for points, probability in sorted(outcomes):
        mass += probability
        if mass >= 0.99:
            expected = points
            break
    assert probability_reference(options) == pytest.approx(expected, abs=0.001)
    assert probability_reference(
        tuple(tuple((p * 1000, m) for p, m in row) for row in options)
    ) == pytest.approx(expected * 1000, abs=1)


def test_legacy_custom_profile_remains_reproducible_and_new_reference_is_practical() -> None:
    profile = load_scoring_catalog().profiles[0]
    legacy = replace(profile, roll_data_version=LEGACY_ROLL_VERSION)
    stats = tuple(
        ObservedStat(n, v, u, 0.99)
        for n, v, u in (
            ("暴擊", 7.5, "%"),
            ("暴擊傷害", 15, "%"),
            ("攻擊", 7.9, "%"),
            ("生命", 390, ""),
            ("防禦", 50, ""),
        )
    )
    current = EchoScoringEngine()
    old = EchoScoringEngine(load_roll_data(LEGACY_ROLL_VERSION))
    assert current.score(legacy, stats) == old.score(legacy, stats)
    assert current.enumerate_remaining(legacy, stats) == old.enumerate_remaining(legacy, stats)
    assert current.score(profile, stats).score > old.score(legacy, stats).score
    assert current.score(profile, stats).score >= 60


def test_one_remaining_slot_uses_conditional_official_value_mass() -> None:
    data = load_roll_data()
    profile = load_scoring_catalog().profiles[0]
    engine = EchoScoringEngine()
    stats = tuple(
        ObservedStat(n, v, u, 0.99)
        for n, v, u in (
            ("攻擊", 7.9, "%"),
            ("生命", 390, ""),
            ("防禦", 50, ""),
            ("生命", 7.9, "%"),
        )
    )
    current = engine.score(profile, stats)
    used = {StatKey.ATTACK_PCT, StatKey.FLAT_HP, StatKey.FLAT_DEFENSE, StatKey.HP_PCT}
    expected = 0.0
    from wuwa_assistant.scoring.character_guides import STAT_LABELS
    from wuwa_assistant.scoring.data import unit_for_stat

    for stat in set(StatKey) - used:
        name = STAT_LABELS[stat]
        name = {
            StatKey.FLAT_ATTACK: "攻擊",
            StatKey.DEFENSE_PCT: "防禦",
            StatKey.BASIC_ATTACK_DAMAGE: "普攻傷害加成",
            StatKey.HEAVY_ATTACK_DAMAGE: "重擊傷害加成",
            StatKey.RESONANCE_SKILL_DAMAGE: "共鳴技能傷害加成",
            StatKey.RESONANCE_LIBERATION_DAMAGE: "共鳴解放傷害加成",
        }.get(stat, name)
        for value, probability in zip(
            data.roll_values[stat], data.probabilities_for(stat), strict=True
        ):
            result = engine.score(
                profile, (*stats, ObservedStat(name, value, unit_for_stat(stat), 0.99))
            )
            assert result.score is not None
            expected += result.score * probability / 9
    projection = engine.enumerate_remaining(profile, stats)
    assert projection.expected_score == pytest.approx(round(expected, 2))
    assert projection.total_probability == 1
    assert projection.minimum_score >= current.score


def test_suisui_usage_reference_includes_all_five_observed_popular_stats() -> None:
    guide = guide_for_character("穗穗")
    assert guide is not None
    assert guide.tiers == tuple(
        (s,)
        for s in (
            StatKey.ENERGY_REGEN,
            StatKey.HP_PCT,
            StatKey.CRIT_DAMAGE,
            StatKey.CRIT_RATE,
            StatKey.FLAT_HP,
        )
    )
    assert "94.1%" in guide.html() and "71.2%" in guide.html()
    assert guide.profile().weight_for(StatKey.CRIT_RATE).weight == 0.4
    assert guide.profile().weight_for(StatKey.FLAT_HP).weight == 0.25
