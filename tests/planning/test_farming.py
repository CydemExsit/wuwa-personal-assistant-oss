from __future__ import annotations

from dataclasses import replace

import pytest

from wuwa_assistant.planning.farming import (
    FarmCharacter,
    FarmEcho,
    FarmRole,
    FarmZone,
    recommend_zones,
    resolve_role,
)

ZONES = (
    FarmZone("ab", "雙受益", "地區", ("A", "B")),
    FarmZone("ac", "單受益", "地區", ("A", "C")),
    FarmZone("cd", "其他", "地區", ("C", "D")),
)


def _character(
    identifier: str = "main", role: FarmRole = FarmRole.MAIN, score: float | None = 40
) -> FarmCharacter:
    return FarmCharacter(
        identifier,
        identifier,
        role,
        "測試角色職能",
        ("A",),
        (FarmEcho(1, 3, 25, score, "A", True, True),),
    )


def test_both_sets_sum_benefits_and_same_character_is_not_counted_twice() -> None:
    main = _character()
    secondary = replace(
        _character("secondary", FarmRole.SECONDARY),
        sets=("B",),
        echoes=(FarmEcho(1, 3, 25, 40, "B", True, True),),
    )
    result = recommend_zones((main, main, secondary), ZONES)
    first, second = result.recommendations[:2]
    assert first.zone.identifier == "ab"
    assert first.useful_sets == ("A", "B")
    assert first.points > second.points > 0
    assert len(first.benefits) == 2


def test_role_thresholds_keep_main_growth_reduce_secondary_and_stop_healer() -> None:
    def points(role: FarmRole, score: float) -> float:
        return (
            recommend_zones((_character(role=role, score=score),), ZONES).recommendations[0].points
        )

    assert points(FarmRole.MAIN, 40) > points(FarmRole.SECONDARY, 40)
    assert points(FarmRole.MAIN, 80) > points(FarmRole.MAIN, 90) > 0
    assert points(FarmRole.SECONDARY, 80) < points(FarmRole.SECONDARY, 60) / 10
    assert points(FarmRole.HEALER, 10) == 0


@pytest.mark.parametrize(
    "echo,notice",
    (
        (FarmEcho(1, 3, 25, None, "A", True, True), "分數待確認"),
        (FarmEcho(1, 3, 25, 0, "A", None, True), "套裝或主詞條待確認"),
        (FarmEcho(1, 3, 10, 10, "A", True, True), "強化潛力"),
        (FarmEcho(1, 4, 25, 10, "A", True, True), "無音區無法補齊"),
        (FarmEcho(1, None, None, None, None, None, None, False), "COST 或裝備位置待確認"),
    ),
)
def test_unknown_unfinished_and_cost4_do_not_become_farming_deficits(
    echo: FarmEcho, notice: str
) -> None:
    result = recommend_zones((replace(_character(), echoes=(echo,)),), ZONES)
    assert all(row.points == 0 for row in result.recommendations)
    assert any(notice in message for message in result.notices)


def test_known_wrong_main_is_actionable_even_if_score_is_unknown() -> None:
    echo = FarmEcho(1, 3, 0, None, "A", True, False)
    result = recommend_zones((replace(_character(role=FarmRole.HEALER), echoes=(echo,)),), ZONES)
    assert result.recommendations[0].points > 0
    assert "補套裝/主詞條" in result.recommendations[0].benefits[0].reason


def test_healer_energy_gap_is_required_but_unknown_energy_is_not_a_numeric_gap() -> None:
    healer = replace(_character(role=FarmRole.HEALER), energy_target=230, energy_current=200)
    result = recommend_zones((healer,), ZONES)
    assert result.recommendations[0].points > 0
    assert "必要共鳴效率" in result.recommendations[0].benefits[0].reason
    unknown = recommend_zones((replace(healer, energy_current=None),), ZONES)
    assert all(row.points == 0 for row in unknown.recommendations)
    assert any("建議共鳴效率 230%" in message for message in unknown.notices)
    met = recommend_zones((replace(healer, energy_current=240),), ZONES)
    assert all(row.points == 0 for row in met.recommendations)


def test_mixed_set_repair_targets_only_missing_count_without_dismantling_good_pieces() -> None:
    character = replace(
        _character(role=FarmRole.HEALER),
        sets=("A", "B", "C"),
        set_combinations=((("A", 1), ("B", 2), ("C", 2)),),
        echoes=(
            FarmEcho(1, 4, 25, 90, "A", True, True),
            FarmEcho(2, 3, 25, 90, "B", True, True),
            FarmEcho(3, 3, 25, 90, "C", True, True),
            FarmEcho(4, 1, 25, 20, "C", True, True),
            FarmEcho(5, 1, 25, 90, "C", True, True),
        ),
    )
    result = recommend_zones((character,), ZONES)
    assert result.recommendations[0].zone.identifier == "ab"
    assert len(result.recommendations[0].benefits) == 1
    assert result.recommendations[0].benefits[0].set_name == "B"
    assert "第 4 件" in result.recommendations[0].benefits[0].reason
    assert all(row.points == 0 for row in result.recommendations[1:])


def test_good_piece_keeps_its_set_instead_of_crediting_all_alternative_builds() -> None:
    character = replace(_character(), sets=("A", "C"))
    result = recommend_zones((character,), ZONES)
    assert next(row for row in result.recommendations if row.zone.identifier == "cd").points == 0


def test_custom_per_slot_targets_are_preserved_without_five_piece_assumption() -> None:
    character = replace(
        _character(role=FarmRole.HEALER),
        sets=("A", "B"),
        echoes=(FarmEcho(1, 3, 25, 10, "A", True, True, target_sets=("A",)),),
        check_set_combination=False,
    )
    assert all(row.points == 0 for row in recommend_zones((character,), ZONES).recommendations)


@pytest.mark.parametrize(
    "label,expected",
    (
        ("湮滅副輸出/效應輔助、生存治療", FarmRole.SECONDARY),
        ("生命治療/雙暴輔助", FarmRole.HEALER),
        ("冷凝輸出/解放、霜漸", FarmRole.MAIN),
        ("霜漸或聲骸輔助/普攻輸出", FarmRole.SECONDARY),
        ("待設定", FarmRole.UNKNOWN),
    ),
)
def test_functional_role_does_not_reduce_hybrid_damage_to_healer(
    label: str, expected: FarmRole
) -> None:
    assert resolve_role(label) is expected
