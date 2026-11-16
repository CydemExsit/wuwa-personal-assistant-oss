from dataclasses import replace
from decimal import Decimal

import pytest

from wuwa_assistant.application.character_catalog import load_bundled_character_catalog
from wuwa_assistant.application.character_stats import calculate_character_panel
from wuwa_assistant.application.characters import (
    CharacterCultivationService,
    CreateCharacter,
    SkillDefinition,
    ensure_recognized_character,
)
from wuwa_assistant.application.combat_build import (
    CombatTeam,
    build_index,
    evaluate_build,
    team_to_json,
)
from wuwa_assistant.application.combat_projection import project_combat_substats
from wuwa_assistant.application.echo_history import EchoHistoryService
from wuwa_assistant.application.equipment_analysis import guide_fit
from wuwa_assistant.scoring.character_guides import guide_for_character
from wuwa_assistant.storage import AppPaths, create_database

from .test_character_stats import _stat, panel_fixture
from .test_suoming import suoming_fixture


def lily_fixture(main="生命", value=33):
    original = panel_fixture()
    slots = tuple(
        replace(
            s,
            set_name="茜染懷想之花",
            name=f"花種{s.slot_index}",
            main_stats=(
                _stat(main, value)
                if s.cost == 4
                else _stat("共鳴效率", 32)
                if s.slot_index == 2
                else _stat("衍射傷害加成", 30)
                if s.cost == 3
                else _stat("生命", 18),
                s.main_stats[1],
            ),
            sub_stats=(
                _stat("生命", 9.4),
                _stat("生命", 470, None),
                _stat("共鳴效率", 9.2),
                _stat("防禦", 8.1),
                _stat("防禦", 40, None),
            ),
        )
        for s in original.echo_slots
    )
    slots = (
        replace(slots[1], slot_index=1, name="解形煞"),
        replace(slots[0], slot_index=2),
        *slots[2:],
    )
    team = CombatTeam("main", "莉莉同奏", confirmed=True, damage_type="heavy_finisher")
    return replace(
        original,
        summary=replace(original.summary, id="lily", name="莉莉", element="衍射", role="同奏輔助"),
        weapon=replace(original.weapon, name="安魂"),
        echo_slots=slots,
        cultivation_goal={
            **original.cultivation_goal,
            "combat_teams": [team_to_json(team)],
            "combat_primary_team": "main",
        },
    )


def test_both_four_cost_mains_are_valid_and_panel_has_no_temporary_bonuses():
    guide = guide_for_character("莉莉")
    assert guide and guide == guide_for_character("Lily")
    assert guide.main_keys(4) == ("hp_pct", "healing_bonus")
    assert guide.energy_target == 260
    assert guide.usage and guide.usage.reference_kind == "creator"
    assert all(rate is None for _, rate in guide.usage.substats)
    panels = []
    for name, value in [("生命", 33), ("治療效果加成", 26.4)]:
        detail = lily_fixture(name, value)
        assert guide_fit(
            guide, cost=4, set_name="茜染懷想之花", main_stats=detail.echo_slots[1].main_stats
        )[:2] == (True, True)
        panel = calculate_character_panel(detail)
        assert panel.complete, panel.missing
        assert panel.value("energy_regen") == Decimal("265.040")
        assert panel.value("crit_rate") == 5
        assert panel.value("crit_damage") == 150
        assert (
            sum(
                c.value
                for c in panel.contributions
                if c.source.startswith("首位") and c.stat == "energy_regen"
            )
            == 10
        )
        panels.append(panel)
    assert panels[0].value("hp") > panels[1].value("hp")
    assert panels[1].value("healing_bonus") - panels[0].value("healing_bonus") == Decimal("26.4")


def test_catalog_and_damage_first_defaults_survive_restart_and_explicit_skills(tmp_path):
    entry = load_bundled_character_catalog().search("莉莉")[0]
    assert (entry.element, entry.weapon_type, entry.rarity) == ("衍射", "佩槍", 5)
    paths = AppPaths.from_root(tmp_path)
    with create_database(paths) as db:
        dataset = EchoHistoryService(db.session_factory).ensure_active_primary_dataset()
        service = CharacterCultivationService(db.session_factory)
        detail = service.create_character(data_set_id=dataset, command=CreateCharacter(name="莉莉"))
        assert [(s.skill_key, s.target_level) for s in detail.skills][:2] == [
            ("intro_skill", 10),
            ("forte_circuit", 10),
        ]
        assert all(s.target_level == 6 for s in detail.skills[2:])
        custom = tuple(
            SkillDefinition(s.skill_key, s.name, s.skill_type, False, 2) for s in detail.skills
        )
        other = service.create_character(
            data_set_id=dataset, command=CreateCharacter(name="其他角色", skills=custom)
        )
        assert all(s.target_level == 2 for s in other.skills)
    with create_database(paths) as db:
        with db.session_factory.begin() as session:
            existing, created = ensure_recognized_character(
                session, data_set_id=dataset, name="莉 莉"
            )
            assert existing.id == detail.summary.id and not created
        reopened = CharacterCultivationService(db.session_factory).get_character(
            detail.summary.id, data_set_id=dataset
        )
        assert reopened.skills == detail.skills


@pytest.mark.parametrize(
    "scope,cr,cd",
    [
        ("heavy_finisher", 85, 310),
        ("intro_skill", 85, 310),
        ("heavy_attack", 5, 150),
        ("resonance_liberation", 5, 150),
        ("echo", 15, 170),
    ],
)
def test_er_conversions_apply_only_to_the_selected_window(scope, cr, cd):
    detail = lily_fixture()
    team = CombatTeam("main", "窗口", confirmed=True, damage_type=scope)
    detail = replace(
        detail, cultivation_goal={**detail.cultivation_goal, "combat_teams": [team_to_json(team)]}
    )
    result = evaluate_build(detail).primary
    assert result and not result.missing
    assert dict(result.totals)["crit_rate"] == cr
    assert dict(result.totals)["crit_damage"] == cd
    assert result.energy_target == 260
    assert not any("夢芳" in e.label for e in result.effects)


def test_support_depends_on_provider_er_layers_rank_receiver_and_exclusions():
    provider = lily_fixture()
    receiver = suoming_fixture()
    hsin = replace(
        receiver,
        summary=replace(receiver.summary, id="hsin", name="心"),
        weapon=replace(receiver.weapon, name="玉闕玄華", weapon_type="音感仪"),
    )
    team = CombatTeam(
        "main", "同奏", ("lily", "hsin"), confirmed=True, damage_type="resonance_skill"
    )
    receiver = replace(
        receiver,
        cultivation_goal={
            **receiver.cultivation_goal,
            "combat_primary_team": "main",
            "combat_teams": [team_to_json(team)],
        },
    )
    roster = {"lily": provider, "hsin": hsin}
    result = evaluate_build(receiver, roster).primary
    assert result and not result.missing, result.missing
    effects = {e.identifier.split(":", 1)[1]: dict(e.values) for e in result.effects}
    assert effects["conversion-attack_pct"] == {"attack_pct": 21}  # 2 responders + Hsin node5
    assert effects["lily-dream-atk"] == {"attack_pct": 28}
    assert effects["lily-flower-atk"] == {"attack_pct": 10}
    assert effects["lily-flower-ensemble"] == {"attack_pct": 15}
    assert effects["lily-weapon-atk"] == {"attack_pct": 16}
    assert not any(k.endswith("-echo") for k in effects if k.startswith("conversion"))
    c2 = replace(provider, summary=replace(provider.summary, resonance_chain=2))
    changed = evaluate_build(receiver, {**roster, "lily": c2}).primary
    assert dict(changed.totals)["crit_rate"] - dict(result.totals)["crit_rate"] == 8
    team = replace(team, excluded_effects=("lily:lily-dream-atk",))
    receiver = replace(
        receiver,
        cultivation_goal={**receiver.cultivation_goal, "combat_teams": [team_to_json(team)]},
    )
    excluded = evaluate_build(receiver, roster).primary
    assert dict(result.totals)["attack"] - dict(excluded.totals)["attack"] == pytest.approx(
        float(dict(calculate_character_panel(receiver).bases)["attack"]) * 0.28
    )


@pytest.mark.parametrize("main,value", [("生命", 33), ("治療效果加成", 26.4)])
@pytest.mark.parametrize("scope", ["heavy_finisher", "intro_skill", "echo"])
def test_both_mains_have_finite_projection_and_healing_is_valued(main, value, scope):
    detail = lily_fixture(main, value)
    team = CombatTeam("main", "預測", confirmed=True, damage_type=scope)
    detail = replace(
        detail, cultivation_goal={**detail.cultivation_goal, "combat_teams": [team_to_json(team)]}
    )
    before = evaluate_build(detail)
    piece = replace(
        detail.echo_slots[-1],
        level=20,
        sub_stats=detail.echo_slots[-1].sub_stats[:4],
    )
    anchored = replace(detail, echo_slots=(*detail.echo_slots[:-1], piece))
    projection = project_combat_substats(before, evaluate_build(anchored), anchored, 5)
    assert projection and projection.outcome_count > 0
    assert 0 <= projection.probability_improve <= 1
    totals = dict(before.primary.totals)
    assert build_index(
        detail, {**totals, "healing_bonus": totals["healing_bonus"] + 20}
    ) > build_index(detail, totals)
    assert build_index(
        detail, {**totals, "energy_regen": totals["energy_regen"] + 20}
    ) == build_index(detail, totals)


@pytest.mark.parametrize("main,value", [("生命", 33), ("治療效果加成", 26.4)])
@pytest.mark.parametrize("scope", ["heavy_finisher", "intro_skill", "echo"])
def test_two_roll_forecast_matches_independent_full_recalculation(monkeypatch, main, value, scope):
    from itertools import combinations

    from wuwa_assistant.application import combat_projection as module
    from wuwa_assistant.application.combat_build import with_candidate
    from wuwa_assistant.scoring import StatKey, load_roll_data
    from wuwa_assistant.scoring.engine import canonical_stat_key

    detail = lily_fixture(main, value)
    team = CombatTeam("main", "預測", confirmed=True, damage_type=scope)
    detail = replace(
        detail, cultivation_goal={**detail.cultivation_goal, "combat_teams": [team_to_json(team)]}
    )
    piece = replace(
        detail.echo_slots[-1],
        level=15,
        sub_stats=(
            detail.echo_slots[-1].sub_stats[0],
            detail.echo_slots[-1].sub_stats[1],
            detail.echo_slots[-1].sub_stats[3],
        ),
    )
    anchored = with_candidate(detail, 5, piece)
    weights = {"hp_pct": 0.8, "crit_rate": 1, "crit_damage": 1}
    before = evaluate_build(detail, weights=weights)
    after = evaluate_build(anchored, weights=weights)
    rolls = load_roll_data()
    frozen = replace(
        rolls,
        roll_values={k: (v[0],) for k, v in rolls.roll_values.items()},
        roll_probabilities={k: (1.0,) for k in rolls.roll_values},
    )
    monkeypatch.setattr(module, "load_roll_data", lambda: frozen)
    result = project_combat_substats(before, after, anchored, 5)
    names = {
        StatKey.CRIT_RATE: ("暴擊", "%"),
        StatKey.CRIT_DAMAGE: ("暴擊傷害", "%"),
        StatKey.ATTACK_PCT: ("攻擊", "%"),
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
    used = {canonical_stat_key(s["name"], s["unit"]) for s in piece.sub_stats}
    deltas = []
    sufficient = 0
    for keys in combinations([k for k in frozen.roll_values if k not in used], 2):
        stats = tuple(_stat(names[k][0], frozen.roll_values[k][0], names[k][1]) for k in keys)
        finished = replace(piece, level=25, sub_stats=(*piece.sub_stats, *stats))
        assessed = evaluate_build(with_candidate(detail, 5, finished), weights=weights)
        if assessed.sufficient:
            sufficient += 1
            deltas.append(assessed.primary.index - before.primary.index)
    assert result.outcome_count == 45
    assert result.probability_sufficient == pytest.approx(sufficient / 45)
    assert result.probability_improve == pytest.approx(sum(v > 1e-9 for v in deltas) / 45)
    assert result.maximum_delta == pytest.approx(max(deltas))


def test_production_replacement_service_predicts_both_mains_without_equipping(tmp_path):
    from copy import deepcopy

    from wuwa_assistant.application.characters import UpsertWeapon
    from wuwa_assistant.application.replacement_training import ReplacementTrainingService
    from wuwa_assistant.scoring.character_guides import load_character_seed_definitions
    from wuwa_assistant.storage.orm import EchoObservationRecord, EchoRecord

    from .test_equipment_overview import _dump, _echo
    from .test_equipment_overview import equipment as fixture

    generator = fixture.__wrapped__(tmp_path)
    equipment = next(generator)
    try:
        example = lily_fixture()
        equipment.profiles.ensure_system_templates(load_character_seed_definitions())
        service = CharacterCultivationService(equipment.database.session_factory)
        owner = service.create_character(
            data_set_id=equipment.data_set_id,
            command=CreateCharacter(
                "莉莉",
                element="衍射",
                level=90,
                small_stat_nodes=(True,) * 10,
                cultivation_goal=example.cultivation_goal,
            ),
        )
        service.upsert_weapon(
            owner.summary.id,
            data_set_id=equipment.data_set_id,
            command=UpsertWeapon("安魂", level=90),
        )
        for part in example.echo_slots:
            identifier, observation_id = _echo(
                equipment,
                owner.summary.id,
                part.slot_index,
                name=part.name,
                stats=list(part.sub_stats),
            )
            with equipment.database.session_factory.begin() as session:
                echo = session.get(EchoRecord, identifier)
                echo.cost, echo.set_name = part.cost, part.set_name
                echo.main_stats_json = list(part.main_stats)
                observation = session.get(EchoObservationRecord, observation_id)
                observation.cost, observation.main_stats_json = part.cost, list(part.main_stats)
                payload = {
                    **observation.effective_payload_json,
                    "cost": part.cost,
                    "set_name": part.set_name,
                    "main_stats": list(part.main_stats),
                    "sub_stats": list(part.sub_stats),
                }
                observation.effective_payload_json = deepcopy(payload)
                observation.raw_payload_json = deepcopy(payload)
        replacement = ReplacementTrainingService(equipment.database.session_factory)
        before = _dump(equipment.database)
        for name, value in [("生命", 33), ("治療效果加成", 26.4)]:
            part = example.echo_slots[1]
            result = replacement.evaluate(
                {
                    "name": part.name,
                    "set_name": part.set_name,
                    "cost": 4,
                    "level": 15,
                    "main_stats": [_stat(name, value), part.main_stats[1]],
                    "sub_stats": list(part.sub_stats[:3]),
                },
                data_set_id=equipment.data_set_id,
                character_id=owner.summary.id,
                slot_index=2,
            )
            assert result.quality_advice_available
            assert result.target_comparable
            assert result.evaluation.projection is not None
            assert result.combat_projection is not None
            assert not result.combat.after.primary.missing
        assert _dump(equipment.database) == before
    finally:
        generator.close()


def test_finisher_inherits_heavy_attack_buffs_but_enhanced_intro_does_not():
    provider = panel_fixture()
    provider = replace(
        provider,
        summary=replace(provider.summary, id="rebecca", name="蕾貝卡"),
        weapon=replace(provider.weapon, name="停駐之煙"),
    )
    values = {}
    for scope in ("heavy_attack", "heavy_finisher", "intro_skill"):
        detail = lily_fixture()
        team = CombatTeam(
            "main", "延奏", ("rebecca",), confirmed=True, damage_type=scope, outro_id="rebecca"
        )
        detail = replace(
            detail,
            cultivation_goal={**detail.cultivation_goal, "combat_teams": [team_to_json(team)]},
        )
        result = evaluate_build(detail, {"rebecca": provider}).primary
        assert not result.missing, result.missing
        values[scope] = dict(result.totals)
    assert values["heavy_finisher"]["deepen"] == values["heavy_attack"]["deepen"]
    assert values["heavy_finisher"]["deepen"] - values["intro_skill"]["deepen"] == 35
    assert values["heavy_finisher"]["crit_rate"] - values["heavy_attack"]["crit_rate"] == 80
