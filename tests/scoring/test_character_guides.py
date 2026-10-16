from __future__ import annotations

from wuwa_assistant.application.character_scoring import character_context
from wuwa_assistant.scoring import EchoScoringEngine, ObservedStat, ScoringContext, StatKey
from wuwa_assistant.scoring.character_guides import (
    apply_guide_context,
    guide_for_character,
    load_character_guides,
    load_character_seed_definitions,
)


def test_guides_are_sourced_offline_priorities_not_official_coefficients() -> None:
    guides = load_character_guides()
    assert len(guides) == 20
    assert len(load_character_seed_definitions()) == 20
    for guide in guides:
        assert len(guide.sources) == 2
        assert all(
            s.author and s.url.startswith("https://mcguide.kurogames.com/") for s in guide.sources
        )
        profile = guide.profile()
        assert profile.provenance is not None and not profile.provenance.is_official
        assert not profile.is_default
        assert set(w.stat for w in profile.weights) == set(StatKey)
        assert "不是攻略的傷害係數" in profile.description
        assert "不會自動下載遊戲內使用率" in guide.html()


def test_hsin_presets_have_no_fabricated_rates_and_prioritize_skill_damage() -> None:
    guide = guide_for_character("心")
    assert guide is not None and guide.role_id == "1311"
    assert guide_for_character("Hsin") == guide
    assert guide.element == "導電"
    assert guide.energy_target == 120
    assert guide.usage is not None and guide.usage.reference_kind == "preset"
    assert all(rate is None for _, rate in guide.usage.substats)
    assert guide.allowed_sets == ("銜夢照世之心",)
    assert "electro_damage" in guide.main_keys(3)
    assert "共鳴迴響·天演溯心" in guide.html()
    profile = guide.profile()
    assert (
        profile.weight_for(StatKey.RESONANCE_SKILL_DAMAGE).weight
        > profile.weight_for(StatKey.RESONANCE_LIBERATION_DAMAGE).weight
    )


def test_aliases_are_exact_and_do_not_assign_base_character_variant() -> None:
    guide = guide_for_character("秧秧・玄翎")
    assert guide is not None and guide.role_id == "1610"
    assert guide_for_character("Yangyang: Xuanling") == guide
    assert guide_for_character("秧秧") is None
    assert guide_for_character("不認識的新角色") is None


def test_different_roles_value_the_same_stat_differently() -> None:
    healer = guide_for_character("莫寧")
    dps = guide_for_character("緋雪")
    assert healer is not None and dps is not None
    scorer = EchoScoringEngine()
    defense = [ObservedStat("防禦", 14.7, "%", 1.0)]
    crit = [ObservedStat("暴擊", 10.5, "%", 1.0)]
    assert (
        scorer.score(healer.profile(), defense).score > scorer.score(dps.profile(), defense).score
    )
    assert scorer.score(dps.profile(), crit).score > scorer.score(healer.profile(), crit).score


def test_efficiency_uses_whole_panel_and_respects_override_without_double_count() -> None:
    guide = guide_for_character("達妮婭")
    assert guide is not None
    profile = guide.profile()
    unknown, notes = apply_guide_context(profile, ScoringContext())
    assert unknown is profile and "建議共鳴效率" in notes[0]
    below, _ = apply_guide_context(
        profile, character_context({"scoring_panel_energy_regen": 114.9})
    )
    assert below is profile
    reached, _ = apply_guide_context(
        profile, character_context({"scoring_panel_energy_regen": 115})
    )
    assert reached.weight_for(StatKey.ENERGY_REGEN).weight == 0.1
    custom, notes = apply_guide_context(
        profile,
        character_context(
            {
                "scoring_panel_energy_regen": 120,
                "scoring_energy_target": 130,
            }
        ),
    )
    assert custom is profile and "自訂目標 130%" in notes[0]
    assert profile.weight_for(StatKey.ENERGY_REGEN).weight == 0.8


def test_invalid_panel_is_unknown_and_efficiency_adjustment_renormalizes() -> None:
    for invalid in [None, True, "260", float("nan"), -1, 99, 1001]:
        assert character_context({"scoring_panel_energy_regen": invalid}).current_totals == {}
    guide = guide_for_character("穗穗")
    assert guide is not None
    adjusted, _ = apply_guide_context(
        guide.profile(), ScoringContext(current_totals={StatKey.ENERGY_REGEN: 260})
    )
    stats = [
        ObservedStat("生命", 11.6, "%", 1),
        ObservedStat("暴擊傷害", 21.0, "%", 1),
        ObservedStat("共鳴效率", 12.4, "%", 1),
    ]
    result = EchoScoringEngine().score(adjusted, stats)
    assert 90 < result.score < 100
    assert 1.4 < result.maximum_weight_points < 1.5
    assert adjusted.weight_for(StatKey.CRIT_RATE).weight == 0.4


def test_equipment_references_use_character_cost_and_preserve_profile_version() -> None:
    from wuwa_assistant.application.equipment_analysis import guide_fit

    for guide in load_character_guides():
        assert guide.element and guide.allowed_sets
        assert guide.main_keys(1) and guide.main_keys(3) and guide.main_keys(4)
        assert guide.profile_version == 4
    guide = guide_for_character("莫寧")
    assert guide.element == "熱熔"
    assert guide.main_keys(3) == ("energy_regen",)
    assert guide_fit(
        guide,
        cost=3,
        set_name=guide.allowed_sets[0],
        main_stats=({"name": "共鳴效率", "value": 32, "unit": "%", "stat_kind": "primary"},),
    )[:2] == (True, True)
    assert (
        guide_fit(
            guide,
            cost=3,
            set_name=guide.allowed_sets[0],
            main_stats=(
                {"name": "共鳴效率", "unit": "%", "stat_kind": "primary", "confidence": 0.4},
            ),
        )[1]
        is None
    )
