from __future__ import annotations

from dataclasses import replace

import pytest

from wuwa_assistant.planning.farming import (
    FarmCharacter,
    FarmEcho,
    FarmRole,
    FarmZone,
    recommend_zones,
)

ZONES = (
    FarmZone("a", "甲", "地區", ("A", "unused")),
    FarmZone("b", "乙", "地區", ("B", "unused")),
    FarmZone("ab", "雙受益", "地區", ("A", "B")),
)


def character(
    identifier: str,
    set_name: str,
    *,
    score: float = 40,
    count: int = 1,
    priority: int = 100,
    role: FarmRole = FarmRole.MAIN,
    team: int | None = None,
) -> FarmCharacter:
    return FarmCharacter(
        identifier,
        identifier,
        role,
        "職能",
        (set_name,),
        tuple(FarmEcho(slot, 3, 25, score, set_name, True, True) for slot in range(1, count + 1)),
        priority=priority,
        best_team_priority=team,
    )


def by_zone(*characters: FarmCharacter) -> dict:
    return {row.zone.identifier: row for row in recommend_zones(characters, ZONES).recommendations}


def test_character_priority_changes_real_rank_and_equal_values_stay_equal() -> None:
    low = character("low", "A", priority=100)
    high = character("high", "B", priority=1)
    rows = by_zone(low, high)
    assert rows["b"].points > rows["a"].points
    assert rows["b"].benefits[0].importance_multiplier == pytest.approx(1.6)
    equal = by_zone(low, replace(high, priority=100))
    assert equal["a"].points == equal["b"].points
    assert all(b.importance_multiplier == 1 for row in equal.values() for b in row.benefits)


def test_team_importance_counts_once_and_never_uses_team_slot_as_role() -> None:
    primary = character("primary", "A", team=1)
    secondary = character("secondary", "B", team=100)
    rows = by_zone(primary, primary, secondary)
    assert rows["a"].points > rows["b"].points
    assert rows["a"].character_count == rows["a"].echo_count == 1
    assert rows["a"].benefits[0].importance_multiplier <= 1.265 + 1e-9
    assert (
        by_zone(replace(primary, best_team_priority=None), secondary)["a"].points < rows["a"].points
    )


def test_more_characters_and_pieces_help_but_one_character_has_diminishing_returns() -> None:
    one = character("one", "A", count=2)
    two = character("two", "B")
    three = character("three", "B")
    rows = by_zone(one, two, three)
    assert rows["a"].character_count == 1 and rows["a"].echo_count == 2
    assert rows["b"].character_count == 2 and rows["b"].echo_count == 2
    assert rows["b"].points > rows["a"].points
    single = by_zone(replace(one, echoes=one.echoes[:1]))["a"]
    assert single.points < rows["a"].points < 2 * single.points
    assert rows["b"].coverage_multiplier > rows["a"].coverage_multiplier


def test_current_scores_and_structural_repairs_remain_distinct() -> None:
    rows = by_zone(character("weak", "A", score=20), character("strong", "B", score=80))
    assert rows["a"].points > rows["b"].points
    wrong = character("healer", "B", role=FarmRole.HEALER, score=90)
    wrong = replace(
        wrong, echoes=(replace(wrong.echoes[0], main_valid=False, level=0, score=None),)
    )
    fixed = replace(wrong, echoes=(replace(wrong.echoes[0], main_valid=True, level=25, score=10),))
    assert by_zone(wrong)["b"].points > 0
    assert by_zone(fixed)["b"].points == 0


def test_passing_secondary_headcount_cannot_swamp_a_real_main_deficit() -> None:
    many = tuple(character(str(i), "B", score=80, role=FarmRole.SECONDARY) for i in range(20))
    rows = by_zone(character("main", "A", score=40), *many)
    assert rows["b"].character_count == 20
    assert rows["b"].effective_character_count < 1
    assert rows["b"].coverage_multiplier == 1
    assert rows["a"].points > 10 * rows["b"].points


def test_alternative_sets_share_one_piece_and_duplicate_slots_do_not_inflate_counts() -> None:
    one = character("main", "A")
    echo = replace(one.echoes[0], target_sets=("A", "A", "B"))
    one = replace(one, sets=("A", "B"), echoes=(echo, echo), check_set_combination=False)
    rows = by_zone(one, one)
    both = rows["ab"]
    assert both.character_count == both.echo_count == 1
    assert len(both.benefits) == 2
    assert both.weighted_base_points == pytest.approx(
        rows["a"].weighted_base_points + rows["b"].weighted_base_points
    )
    assert both.dual_set_multiplier == pytest.approx(1.1)
    assert rows["a"].dual_set_multiplier == 1


def test_balanced_dual_set_use_is_bounded_and_more_need_never_lowers_priority() -> None:
    previous = 0.0
    for score in range(100, -1, -5):
        rows = by_zone(character("main", "A", score=40), character("other", "B", score=score))
        both = rows["ab"]
        assert 1 <= both.dual_set_multiplier <= 1.1 + 1e-9
        assert both.points >= previous
        assert both.points == pytest.approx(
            both.weighted_base_points * both.coverage_multiplier * both.dual_set_multiplier,
            abs=0.000051,
        )
        assert sum(b.points for b in both.benefits) == pytest.approx(both.weighted_base_points)
        previous = both.points


def test_mixed_set_slot_rank_exchange_cannot_lower_priority_when_a_piece_gets_worse() -> None:
    previous = 0.0
    for tenths in range(420, 379, -1):
        mixed = replace(
            character("mixed", "A"),
            sets=("A", "B"),
            check_set_combination=False,
            echoes=(
                FarmEcho(1, 1, 25, 40, "A", True, True),
                FarmEcho(2, 1, 25, tenths / 10, "B", True, True),
                FarmEcho(3, 3, 25, 20, "B", True, True),
            ),
        )
        both = by_zone(mixed)["ab"]
        assert both.points >= previous
        assert both.character_count == 1 and both.echo_count == 3
        assert 1 <= both.dual_set_multiplier <= 1.1 + 1e-9
        previous = both.points


def test_order_is_deterministic_and_unknown_or_unfinished_pieces_add_no_coverage() -> None:
    a = character("a", "A", count=3)
    b = character("b", "B", count=2)
    unknown = replace(a, identifier="unknown", echoes=(replace(a.echoes[0], score=None),))
    unfinished = replace(a, identifier="unfinished", echoes=(replace(a.echoes[0], level=15),))
    baseline = by_zone(a, b)
    reversed_rows = by_zone(replace(b, echoes=tuple(reversed(b.echoes))), a, unknown, unfinished)
    for key, row in baseline.items():
        assert reversed_rows[key].points == row.points
        assert reversed_rows[key].echo_count == row.echo_count
        assert reversed_rows[key].coverage_multiplier == row.coverage_multiplier
