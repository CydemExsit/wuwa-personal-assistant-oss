from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from wuwa_assistant.application.character_catalog import load_bundled_character_catalog
from wuwa_assistant.application.character_scoring import character_profile
from wuwa_assistant.application.character_stats import calculate_character_panel
from wuwa_assistant.application.characters import (
    CharacterCultivationService,
    CreateCharacter,
    ensure_recognized_character,
)
from wuwa_assistant.application.combat_build import CombatTeam, available_effects, evaluate_build
from wuwa_assistant.application.echo_history import EchoHistoryService
from wuwa_assistant.application.scoring_profiles import ScoringProfileService
from wuwa_assistant.scoring import ScoringContext, StatKey
from wuwa_assistant.scoring.character_guides import (
    apply_guide_context,
    guide_for_character,
    load_character_seed_definitions,
)
from wuwa_assistant.storage import AppPaths, create_database

from .test_character_stats import _stat, panel_fixture


def suoming_fixture():
    original = panel_fixture()
    slots = tuple(
        replace(
            slot,
            name="絕息魄" if slot.slot_index == 1 else f"測試種類{slot.slot_index}",
            set_name="銜夢照世之心",
            main_stats=tuple(
                {**stat, "name": "導電傷害加成"} if stat["name"] == "冷凝傷害加成" else stat
                for stat in slot.main_stats
            ),
        )
        for slot in original.echo_slots
    )
    # The active Echo can be COST3 while the loadout still contains one COST4.
    slots = (
        replace(slots[1], slot_index=1, name="絕息魄"),
        replace(slots[0], slot_index=2, name="測試種類2"),
        *slots[2:],
    )
    return replace(
        original,
        summary=replace(original.summary, name="鎖暝", element="導電", role="同奏副輸出"),
        weapon=replace(original.weapon, name="沉冥", weapon_type="迅刀"),
        echo_slots=slots,
    )


def test_usage_is_exact_and_separate_from_support_weights_and_missing_set_rate():
    guide = guide_for_character("鎖暝")
    assert guide and guide.usage
    assert guide_for_character("Suoming") == guide_for_character("锁暝") == guide
    assert guide_for_character("鎖明") is None
    assert guide.role == "同奏副輸出"
    assert tuple(p for _, p in guide.usage.substats) == (75.3, 72.4, 46.6, 42.7, 42.6)
    assert guide.usage.main_stats == (
        (4, "crit_damage", 77.4),
        (3, "electro_damage", 78.7),
        (1, "attack_pct", 99.5),
    )
    assert guide.usage.main_echoes == (("絕息魄", 55.2), ("共鳴迴響·天演溯心", 44.2))
    assert guide.usage.set_icons == (("銜夢照世之心 5件", None),)
    assert guide.sets_complete({"衔梦照世之心": 5})
    assert guide.sets_complete({"輕雲出月": 5})
    assert not guide.sets_complete({"銜夢照世之心": 3, "輕雲出月": 2})
    profile = guide.profile()
    assert profile.weight_for(StatKey.ENERGY_REGEN).weight == 0.8
    assert profile.weight_for(StatKey.BASIC_ATTACK_DAMAGE).weight == 0.4
    assert profile.weight_for(StatKey.RESONANCE_SKILL_DAMAGE).weight == 0
    for value in (119.9, 120, 130):
        adjusted, _ = apply_guide_context(
            profile, ScoringContext(current_totals={StatKey.ENERGY_REGEN: value})
        )
        assert adjusted.weight_for(StatKey.ENERGY_REGEN).weight == (0.8 if value < 120 else 0.1)
    overridden, _ = apply_guide_context(
        profile,
        ScoringContext(
            current_totals={StatKey.ENERGY_REGEN: 120}, conditions={"guide_energy_target": 125}
        ),
    )
    assert overridden == profile


def test_catalog_creation_recognition_auto_profile_and_restart_preserve_custom_choice(
    tmp_path: Path,
):
    paths = AppPaths.from_root(tmp_path / "suoming")
    entry = load_bundled_character_catalog().search("鎖暝")[0]
    assert (entry.element, entry.weapon_type, entry.rarity) == ("導電", "迅刀", 5)
    with create_database(paths) as db:
        dataset = EchoHistoryService(db.session_factory).ensure_active_primary_dataset()
        profiles = ScoringProfileService(db.session_factory)
        profiles.ensure_system_templates(load_character_seed_definitions())
        service = CharacterCultivationService(db.session_factory)
        character = service.create_character(
            data_set_id=dataset,
            command=CreateCharacter(name=entry.name, element=entry.element, role="同奏副輸出"),
        )
        automatic = character_profile(
            profiles, data_set_id=dataset, name=entry.name, explicit_profile_id=None
        )
        assert automatic.template_kind == "character_guide_1312"
        custom = profiles.clone_template(automatic.id, data_set_id=dataset, name="我的鎖暝")
        saved = custom.to_json_dict()
        with db.session_factory.begin() as session:
            owner, created = ensure_recognized_character(session, data_set_id=dataset, name="鎖 暝")
            assert owner.id == character.summary.id and not created
    with create_database(paths) as db:
        profiles = ScoringProfileService(db.session_factory)
        profiles.ensure_system_templates(load_character_seed_definitions())
        chosen = character_profile(
            profiles, data_set_id=dataset, name=entry.name, explicit_profile_id=custom.id
        )
        assert chosen.to_json_dict() == saved
        assert (
            len(
                CharacterCultivationService(db.session_factory).list_characters(data_set_id=dataset)
            )
            == 1
        )


def test_noncombat_panel_keeps_permanent_chains_but_excludes_rotation_buffs():
    detail = suoming_fixture()
    panel = calculate_character_panel(detail)
    assert panel.complete, panel.missing
    assert panel.value("crit_rate") == Decimal("96.8")
    assert panel.value("electro_damage") == 40  # Main30 + unconditional two-piece10.
    chain2 = calculate_character_panel(
        replace(detail, summary=replace(detail.summary, resonance_chain=2))
    )
    assert chain2.value("crit_damage") - panel.value("crit_damage") == 40
    chain4 = calculate_character_panel(
        replace(detail, summary=replace(detail.summary, resonance_chain=4))
    )
    assert chain4.value("attack") - chain2.value("attack") == dict(panel.bases)["attack"] / 5
    assert all("禁鎖契主" not in c.source for c in panel.contributions)


@pytest.mark.parametrize("rank,self_bonus,team_bonus", [(1, 30, 24), (5, 60, 48)])
def test_support_buffs_follow_rank_intro_nodes_outro_recipient_and_modes(
    rank, self_bonus, team_bonus
):
    detail = suoming_fixture()
    provider = replace(
        detail,
        summary=replace(detail.summary, id="support", resonance_chain=2),
        weapon=replace(detail.weapon, resonance_rank=rank),
    )
    hsin = replace(
        detail,
        summary=replace(detail.summary, id="hsin", name="心"),
        weapon=replace(detail.weapon, name="玉闕玄華", weapon_type="音感儀"),
    )
    team = CombatTeam(
        "main", "同奏隊", ("support",), damage_type="resonance_skill", outro_id="support"
    )
    effects = available_effects(hsin, team, {"support": provider})
    values = {e.identifier.split(":")[1]: dict(e.values) for e in effects}
    assert values["suoming-pact"] == {"electro_damage": 70}
    assert values["suoming-chain2"] == {"crit_damage": 28}
    assert values["suoming-outro-skill"] == {"resonance_skill_deepen": 25}
    assert values["suoming-phantom"] == {"electro_damage": 12}
    assert values["suoming-weapon-team"] == {"electro_damage": team_bonus}
    assert "suoming-rain" not in values and "suoming-weapon-self" not in values
    own = available_effects(provider, replace(team, member_ids=("hsin",)), {"hsin": hsin})
    assert next(dict(e.values) for e in own if e.identifier.endswith(":suoming-weapon-self")) == {
        "electro_damage": self_bonus
    }
    assert next(dict(e.values) for e in own if e.identifier.endswith(":suoming-rain")) == {
        "electro_damage": 50
    }
    disabled = available_effects(
        hsin, replace(team, outro_id=None, modes=(("hsin", "電磁"),)), {"support": provider}
    )
    assert all("延奏" not in e.label and "沉契凝鎖" not in e.label for e in disabled)
    electro = available_effects(
        hsin, replace(team, modes=(("hsin", "電磁"),)), {"support": provider}
    )
    assert not any(e.identifier.endswith(":suoming-outro-skill") for e in electro)
    assert next(dict(e.values) for e in electro if e.identifier.endswith(":suoming-chain2")) == {
        "crit_damage": 10
    }
    unequipped = replace(provider, weapon=None, small_stat_nodes=(False,) * 10)
    missing = available_effects(hsin, team, {"support": unequipped})
    assert not any(
        "suoming-weapon" in e.identifier or "suoming-pact" in e.identifier for e in missing
    )
    # Hsin's second inherent and sixth chain each add one response stack.
    locked = replace(
        hsin, small_stat_nodes=(*hsin.small_stat_nodes[:5], False, *hsin.small_stat_nodes[6:])
    )
    without_inherent = available_effects(locked, team, {"support": provider})
    assert next(
        dict(e.values) for e in without_inherent if e.identifier.endswith(":suoming-chain2")
    ) == {"crit_damage": 22}
    sixth = replace(hsin, summary=replace(hsin.summary, resonance_chain=6))
    full = available_effects(sixth, team, {"support": provider})
    assert next(dict(e.values) for e in full if e.identifier.endswith(":suoming-chain2")) == {
        "crit_damage": 34
    }


def test_build_uses_basic_attack_and_er_is_a_constraint_after_target():
    detail = suoming_fixture()
    detail = replace(
        detail,
        cultivation_goal={
            **detail.cultivation_goal,
            "combat_primary_team": "main",
            "combat_teams": [
                {"id": "main", "name": "同奏", "confirmed": True, "damage_type": "basic_attack"}
            ],
        },
    )
    baseline = evaluate_build(detail)
    assert baseline.primary.energy_target == 120 and baseline.sufficient
    piece = detail.echo_slots[-1]
    more = replace(piece, sub_stats=(*piece.sub_stats[:-1], _stat("共鳴效率", 12.4)))
    changed = replace(detail, echo_slots=(*detail.echo_slots[:-1], more))
    assert evaluate_build(changed).primary.index == baseline.primary.index
