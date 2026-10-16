from __future__ import annotations

from collections import Counter
from itertools import permutations

import pytest

from wuwa_assistant.application.character_catalog import load_bundled_character_catalog
from wuwa_assistant.application.equipment_analysis import guide_fit
from wuwa_assistant.scoring import ScoringContext, StatKey
from wuwa_assistant.scoring.character_guides import (
    _usage_reference,
    apply_guide_context,
    guide_for_character,
    load_character_guides,
)


@pytest.mark.parametrize(
    "name,index,substats,mains,sets",
    [
        ("琳奈", 1, (75.3, 72.8, 52.4, 43.0, 40.6), (60.1, 87.9, 98.8), (97.7,)),
        ("露西", 2, (None,) * 5, (None,) * 3, (None,)),
        ("蕾貝卡", 3, (None,) * 5, (None,) * 3, (None,)),
        ("嘉貝莉娜", 4, (84.3, 79.6, 45.5, 44.2, 43.2), (88.3, 94.5, 99.3), (71.9, 24.9)),
        ("露帕", 5, (81.4, 77.0, 46.9, 42.4, 42.0), (87.6, 85.3, 99.3), (99.7,)),
        ("卡提希婭", 6, (80.8, 79.5, 48.7, 43.0, 40.6), (52.9, 66.9, 97.7), (99.4,)),
        ("珂萊塔", 7, (84.3, 79.2, 42.5, 42.3, 41.5), (84.7, 87.2, 98.8), (99.6,)),
        ("守岸人", 8, (64.3, 47.5, 46.7, 40.9, 39.5), (48.6, 85.9, 92.6), (99.4,)),
        ("折枝", 9, (70.9, 65.4, 45.6, 39.7, 38.8), (77.1, 21.3), (58.5, 34.3)),
        ("維里奈", 10, (50.8, 44.0, 41.0, 40.4, 39.2), (82.8, 76.8, 86.1), (97.3,)),
    ],
)
def test_ten_supplied_references_preserve_observations_and_absence(
    name: str,
    index: int,
    substats: tuple[float | None, ...],
    mains: tuple[float | None, ...],
    sets: tuple[float | None, ...],
) -> None:
    guide = guide_for_character(name)
    assert guide is not None and guide.usage is not None
    usage = guide.usage
    assert usage.screenshot_index == index
    assert tuple(value for _, value in usage.substats) == substats
    assert tuple(value for _, _, value in usage.main_stats) == mains
    assert tuple(value for _, value in usage.set_icons) == sets
    assert all("件" in label and "圖示" not in label for label, _ in usage.set_icons)
    html = guide.html()
    if name in ("露西", "蕾貝卡"):
        assert usage.reference_kind == "preset"
        assert "預設推薦" in html and "未提供使用率" in html
        assert "0.0%" not in usage.html(guide.weights)
    else:
        assert all(f"{value:.1f}%" in html for value in substats if value is not None)


def test_roster_catalog_and_named_sets_cover_all_twenty() -> None:
    catalog = {entry.name for entry in load_bundled_character_catalog().entries}
    guides = load_character_guides()
    assert len(guides) == 20
    assert {guide.name for guide in guides} <= catalog
    for guide in guides:
        assert guide.usage is not None
        assert all("件" in label and "圖示" not in label for label, _ in guide.usage.set_icons)


def test_missing_main_rates_and_same_echo_different_set_are_preserved() -> None:
    zhezhi = guide_for_character("折枝")
    galbrena = guide_for_character("嘉貝莉娜")
    assert zhezhi and zhezhi.usage and galbrena and galbrena.usage
    assert {cost for cost, _, _ in zhezhi.usage.main_stats} == {4}
    assert zhezhi.main_keys(3)  # Separately attributed to the creator guides.
    assert len({name for name, _ in galbrena.usage.main_echoes}) == 2
    assert tuple(value for _, value in galbrena.usage.main_echoes) == (65.8, 25.8)


def test_preset_cannot_fabricate_percentages() -> None:
    with pytest.raises(ValueError, match="cannot claim"):
        _usage_reference(
            {"usage_reference": {"reference_kind": "preset", "substats_percent": {"crit_rate": 0}}}
        )


@pytest.mark.parametrize("name,element_set", [("露西", "浮星祛暗"), ("蕾貝卡", "徹空冥雷")])
def test_one_plus_two_plus_two_requires_each_effect(name: str, element_set: str) -> None:
    guide = guide_for_character(name)
    assert guide is not None
    assert guide.sets_complete({"碎夢亡鬼之魘": 1, "不絕餘音": 2, element_set: 2})
    assert not guide.sets_complete({"不絕餘音": 3, element_set: 2})
    assert not guide.sets_complete({"碎夢亡鬼之魘": 1, "不絕餘音": 3, element_set: 1})
    assert not guide.sets_complete({"碎夢亡鬼之魘": 5})


@pytest.mark.parametrize("name,element_set", [("露西", "浮星祛暗"), ("蕾貝卡", "徹空冥雷")])
@pytest.mark.parametrize("single_set", ["碎夢亡鬼之魘", "碎夢亡鬼之魔"])
def test_exact_legacy_set_alias_matches_all_slot_orders(
    name: str, element_set: str, single_set: str
) -> None:
    guide = guide_for_character(name)
    assert guide is not None
    # Official set group 32 is 碎梦亡鬼之魇; older local OCR read its final
    # character as 魔. Accept this exact alias without changing observed text.
    assert guide.canonical_set_name(single_set) == "碎夢亡鬼之魘"
    for order in set(permutations((single_set, element_set, element_set, "不絕餘音", "不絕餘音"))):
        assert guide.sets_complete(Counter(order))
    assert guide_fit(guide, cost=4, set_name=single_set, main_stats=())[0] is True


@pytest.mark.parametrize("name,element_set", [("露西", "浮星祛暗"), ("蕾貝卡", "徹空冥雷")])
def test_legacy_set_alias_preserves_counts_and_unknown_names(name: str, element_set: str) -> None:
    guide = guide_for_character(name)
    assert guide is not None
    for counts in (
        {"碎夢亡鬼之魔": 1, "不絕餘音": 3, element_set: 1},
        {"碎夢亡鬼之魔": 1, "不絕餘音": 1, element_set: 3},
        {"碎夢亡鬼之魔": 2, "不絕餘音": 1, element_set: 2},
        {"碎夢亡鬼之魔": 1, "不絕餘音": 2, "未知套裝": 2},
        {"碎夢亡鬼之魂": 1, "不絕餘音": 2, element_set: 2},
        {"碎夢": 1, "不絕餘音": 2, element_set: 2},
        {"不絕餘音": 2, element_set: 2},
    ):
        for order in permutations(counts.items()):
            assert not guide.sets_complete(dict(order))
    assert guide.canonical_set_name("碎夢亡鬼之魂") == "碎夢亡鬼之魂"
    assert guide_fit(guide, cost=4, set_name=None, main_stats=())[0] is None


def test_support_mechanics_override_popularity_and_energy_is_conditional() -> None:
    shore = guide_for_character("守岸人")
    verina = guide_for_character("維里奈")
    cartethyia = guide_for_character("卡提希婭")
    assert shore and verina and cartethyia
    assert (
        shore.profile().weight_for(StatKey.CRIT_RATE).weight
        < shore.profile().weight_for(StatKey.CRIT_DAMAGE).weight
    )
    assert "必定暴擊" in shore.caveat and "250%" in shore.caveat
    assert (
        verina.profile().weight_for(StatKey.ATTACK_PCT).weight
        > verina.profile().weight_for(StatKey.HP_PCT).weight
    )
    assert cartethyia.profile().weight_for(StatKey.ATTACK_PCT).weight == 0
    assert cartethyia.profile().weight_for(StatKey.HP_PCT).weight > 0
    profile = shore.profile()
    adjusted, _ = apply_guide_context(
        profile,
        ScoringContext(
            current_totals={StatKey.ENERGY_REGEN: 230},
            conditions={"guide_energy_target": 250},
        ),
    )
    assert adjusted is profile
    assert "2鏈" in verina.caveat and "230%" in verina.caveat


def test_corrected_midnight_veil_name_retains_legacy_reference_alias() -> None:
    guide = guide_for_character("千咲")
    assert guide is not None
    assert "幽夜隱匿之帷" in guide.allowed_sets
    assert guide.sets_complete({"命理崩毀之弦": 3, "幽夜隱跡": 2})
    assert guide.sets_complete({"命理崩毀之弦": 3, "幽夜隱匿之帷": 2})
