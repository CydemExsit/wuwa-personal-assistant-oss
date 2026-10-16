from __future__ import annotations

import pytest

from wuwa_assistant.scoring import ScoringContext, StatKey
from wuwa_assistant.scoring.character_guides import (
    _usage_percent,
    apply_guide_context,
    guide_for_character,
)


@pytest.mark.parametrize(
    "name,index,substats,main_percentages",
    [
        ("洛瑟菈", 1, (78.1, 75.9, 43.8, 42.9), (61.1, 88.1, 99.1)),
        ("秧秧·玄翎", 2, (81.6, 80.5, 45.9, 44.3, 43.1), (83.8, 89.8, 99.3)),
        ("穗穗", 3, (71.2, 50.5, 48.1, 41.4, 37.8), (56.2, 94.1, 98.0)),
        ("清宵", 4, (81.3, 80.1, 46.2, 46.0, 35.9), (68.1, 69.4, 99.4)),
        ("緋雪", 5, (83.0, 82.6, 47.1, 47.1, 45.7), (83.3, 83.0, 98.8)),
        ("莫寧", 6, (70.6, 53.5, 48.3, 42.8, 42.4), (58.6, 86.7, 98.5)),
        ("愛彌斯", 7, (84.4, 84.0, 49.8, 48.4, 46.1), (86.4, 77.7, 99.2)),
        ("達妮婭", 8, (76.1, 73.6, 51.3, 44.1, 42.7), (64.5, 86.5, 99.0)),
        ("千咲", 9, (77.8, 76.9, 53.3, 44.4, 43.1), (71.8, 83.6, 99.1)),
    ],
)
def test_supplied_usage_stays_separate_from_weights_and_visible_per_character(
    name: str, index: int, substats: tuple[float, ...], main_percentages: tuple[float, ...]
) -> None:
    guide = guide_for_character(name)
    assert guide is not None and guide.usage is not None
    usage = guide.usage
    assert usage.screenshot_index == index
    assert tuple(value for _, value in usage.substats) == substats
    assert tuple(value for _, _, value in usage.main_stats) == main_percentages
    assert tuple(cost for cost, _, _ in usage.main_stats) == (4, 3, 1)
    assert all(0 <= weight.weight <= 1 for weight in guide.weights)
    html = guide.html()
    assert "遊戲內使用率" in html and "基礎權重" in html
    assert "未顯示不表示零使用率" in html
    assert guide.weight_rationale in html
    assert all(f"{percent:.1f}%" in html for percent in (*substats, *main_percentages))
    assert all(source.url.replace("&", "&amp;") in html for source in guide.sources)


def test_support_stats_and_partial_screenshot_are_not_mistaken_for_damage_or_zero_usage() -> None:
    suisui = guide_for_character("穗穗")
    assert suisui is not None and suisui.usage is not None
    assert dict(suisui.usage.substats)[StatKey.FLAT_HP] == 37.8
    assert suisui.usage.main_stats == (
        (4, "healing_bonus", 56.2),
        (3, "energy_regen", 94.1),
        (1, "hp_pct", 98.0),
    )
    assert "使用者補充" in suisui.usage.notes
    qingxiao = guide_for_character("清宵")
    assert qingxiao is not None and qingxiao.usage is not None
    assert StatKey.HEAVY_ATTACK_DAMAGE not in dict(qingxiao.usage.substats)
    assert qingxiao.profile().weight_for(StatKey.HEAVY_ATTACK_DAMAGE).weight > 0
    luosela = guide_for_character("洛瑟菈")
    assert luosela is not None and luosela.usage is not None
    assert luosela.usage.main_echoes[0] == ("格洛獁圖", 80.0)
    assert luosela.energy_target is None
    assert luosela.profile().weight_for(StatKey.ENERGY_REGEN).weight == 0


def test_updated_role_priorities_keep_conditional_energy_and_secondary_attack_bounded() -> None:
    mornye = guide_for_character("莫寧")
    aemeath = guide_for_character("愛彌斯")
    chisa = guide_for_character("Chisa")
    assert mornye is not None and aemeath is not None and chisa is not None
    support = mornye.profile()
    assert support.weight_for(StatKey.DEFENSE_PCT).weight == 0.8
    assert support.weight_for(StatKey.CRIT_RATE).weight == 0.4
    assert support.weight_for(StatKey.RESONANCE_LIBERATION_DAMAGE).weight == 0.6
    assert aemeath.profile().weight_for(StatKey.FLAT_ATTACK).weight == 0.25
    for guide in (aemeath, chisa):
        before = guide.profile()
        assert before.weight_for(StatKey.ENERGY_REGEN).weight == 0.8
        after, notes = apply_guide_context(
            before, ScoringContext(current_totals={StatKey.ENERGY_REGEN: guide.energy_target})
        )
        assert after.weight_for(StatKey.ENERGY_REGEN).weight == 0.1 and notes
        assert after.weight_for(StatKey.ATTACK_PCT) == before.weight_for(StatKey.ATTACK_PCT)
    assert chisa.usage is not None
    assert tuple(p for _, p in chisa.usage.set_icons) == (60.1, 29.1)
    assert chisa.profile().weight_for(StatKey.HP_PCT).weight == 0


@pytest.mark.parametrize("value", [True, -1, 101, float("nan"), float("inf"), "50"])
def test_invalid_usage_percentage_fails_closed(value: object) -> None:
    with pytest.raises(ValueError, match="usage percentage"):
        _usage_percent(value)
