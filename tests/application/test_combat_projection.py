from dataclasses import replace

import pytest

from wuwa_assistant.application.combat_build import evaluate_build, with_candidate
from wuwa_assistant.application.combat_projection import project_combat_substats
from wuwa_assistant.scoring import StatKey, load_roll_data

from .test_character_stats import _stat
from .test_combat_build import combat_fixture


def test_exact_one_roll_joint_feasibility_and_gain_match_hand_probability():
    detail = combat_fixture(target=145, secondary=145)
    piece = replace(
        detail.echo_slots[4],
        echo_id="candidate",
        level=20,
        sub_stats=detail.echo_slots[4].sub_stats[:4],
    )
    anchored = with_candidate(detail, 5, piece)
    before, after = evaluate_build(detail), evaluate_build(anchored)
    result = project_combat_substats(before, after, anchored, 5)
    assert result is not None
    rolls = load_roll_data()
    # Of9 remaining stat types only ER can close the8.2pp gap. Values8.4+ suffice.
    expected = (
        sum(
            p
            for v, p in zip(
                rolls.roll_values[StatKey.ENERGY_REGEN],
                rolls.probabilities_for(StatKey.ENERGY_REGEN),
                strict=True,
            )
            if v >= 8.2
        )
        / 9
    )
    assert result.probability_sufficient == pytest.approx(expected)
    assert result.probability_improve <= result.probability_sufficient
    assert "主詞條維持目前值" in result.scope


def test_two_roll_projection_matches_scalar_recalculation_for_deterministic_rolls(monkeypatch):
    from itertools import combinations
    from math import comb

    from wuwa_assistant.application import combat_projection as module

    detail = combat_fixture(target=130, secondary=135)
    piece = replace(
        detail.echo_slots[4],
        echo_id="candidate",
        level=15,
        sub_stats=detail.echo_slots[4].sub_stats[:3],
    )
    anchored = with_candidate(detail, 5, piece)
    rolls = load_roll_data()
    frozen = replace(
        rolls,
        roll_values={k: (v[0],) for k, v in rolls.roll_values.items()},
        roll_probabilities={k: (1.0,) for k in rolls.roll_values},
    )
    monkeypatch.setattr(module, "load_roll_data", lambda: frozen)
    before, after = evaluate_build(detail), evaluate_build(anchored)
    result = project_combat_substats(before, after, anchored, 5)
    used = {StatKey.CRIT_RATE, StatKey.CRIT_DAMAGE, StatKey.ATTACK_PCT}
    names = {
        StatKey.FLAT_ATTACK: ("攻擊", None),
        StatKey.HP_PCT: ("生命", "%"),
        StatKey.FLAT_HP: ("生命", None),
        StatKey.DEFENSE_PCT: ("防禦", "%"),
        StatKey.FLAT_DEFENSE: ("防禦", None),
        StatKey.ENERGY_REGEN: ("共鳴效率", "%"),
        StatKey.BASIC_ATTACK_DAMAGE: ("普攻傷害加成", "%"),
        StatKey.HEAVY_ATTACK_DAMAGE: ("重擊傷害加成", "%"),
        StatKey.RESONANCE_SKILL_DAMAGE: ("共鳴技能傷害加成", "%"),
        StatKey.RESONANCE_LIBERATION_DAMAGE: ("共鳴解放傷害加成", "%"),
    }
    sufficient = improved = 0
    for keys in combinations([k for k in frozen.roll_values if k not in used], 2):
        stats = tuple(_stat(names[k][0], frozen.roll_values[k][0], names[k][1]) for k in keys)
        completed = replace(piece, level=25, sub_stats=(*piece.sub_stats, *stats))
        assessed = evaluate_build(with_candidate(detail, 5, completed))
        sufficient += assessed.sufficient is True
        improved += (
            assessed.sufficient is True and assessed.primary.index > before.primary.index + 1e-9
        )
    assert result.outcome_count == comb(10, 2)
    assert result.probability_sufficient == pytest.approx(sufficient / comb(10, 2))
    assert result.probability_improve == pytest.approx(improved / comb(10, 2))


def test_exploration_and_unknown_outcomes_have_no_fabricated_battle_probability():
    detail = combat_fixture()
    assert (
        project_combat_substats(evaluate_build(detail), evaluate_build(detail), detail, 5) is None
    )
    piece = replace(detail.echo_slots[4], level=10, sub_stats=detail.echo_slots[4].sub_stats[:2])
    anchored = with_candidate(detail, 5, piece)
    assert (
        project_combat_substats(evaluate_build(detail), evaluate_build(anchored), anchored, 5)
        is None
    )
