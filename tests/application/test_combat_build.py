from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from wuwa_assistant.application.character_stats import load_panel_catalog
from wuwa_assistant.application.combat_build import (
    CombatTeam,
    available_effects,
    build_index,
    compare_builds,
    crit_preference,
    evaluate_build,
    find_compensation,
    load_combat_catalog,
    team_to_json,
    teams_from_goal,
    with_candidate,
)

from .test_character_stats import _stat, panel_fixture


def combat_fixture(*, target=140, secondary=145):
    detail = panel_fixture()
    teams = (
        CombatTeam("main", "主要", energy_target=target, confirmed=True),
        CombatTeam("other", "第二隊", energy_target=secondary, confirmed=True),
    )
    return replace(
        detail,
        cultivation_goal={
            **detail.cultivation_goal,
            "combat_teams": [team_to_json(t) for t in teams],
            "combat_primary_team": "main",
        },
    )


def test_all_required_teams_share_exact_equipment_and_primary_does_not_hide_er_gap():
    detail = combat_fixture(secondary=150)
    build = evaluate_build(detail)
    assert build.primary.sufficient is True
    assert build.teams[1].energy_gap == 4
    assert build.sufficient is False
    assert dict(build.teams[0].totals) == dict(build.teams[1].totals)


def test_er_surplus_has_exactly_zero_value_and_crossing_target_is_binary():
    detail = combat_fixture()
    before = evaluate_build(detail)
    piece = detail.echo_slots[4]
    more = replace(piece, sub_stats=(*piece.sub_stats[:4], _stat("共鳴效率", 12.4)))
    after = evaluate_build(with_candidate(detail, 5, more))
    assert compare_builds(before, after).primary_delta == 0
    absent = replace(piece, sub_stats=(*piece.sub_stats[:4], _stat("防禦", 11.8)))
    below = evaluate_build(with_candidate(detail, 5, absent))
    assert below.sufficient is False
    assert below.teams[1].energy_gap == pytest.approx(8.2)
    assert before.sufficient is True


def test_replacement_removes_old_once_and_same_echo_never_double_counts():
    detail = combat_fixture()
    identical = with_candidate(detail, 1, detail.echo_slots[0])
    assert evaluate_build(detail) == evaluate_build(identical)
    new = replace(
        detail.echo_slots[0],
        echo_id="new",
        sub_stats=(_stat("暴擊", 10.5), *detail.echo_slots[0].sub_stats[1:]),
    )
    after = evaluate_build(with_candidate(detail, 1, new))
    assert (
        dict(after.primary.totals)["crit_rate"]
        - dict(evaluate_build(detail).primary.totals)["crit_rate"]
        == 3
    )


def test_crit_overflow_zero_cd_uncapped_and_high_crit_preference_smooth():
    assert crit_preference(100, 300) == crit_preference(120, 300)
    assert crit_preference(98, 302) > crit_preference(99, 300)
    assert crit_preference(95, 600) > crit_preference(95, 500)
    assert crit_preference(89.9999, 280) < crit_preference(90.0001, 280)
    gains = [crit_preference(p + 1, 280) - crit_preference(p, 280) for p in (80, 90, 95, 98)]
    assert gains == sorted(gains, reverse=True)


def test_source_coverage_and_all_reviewed_chains_retain_auditable_facts():
    catalog = load_combat_catalog()
    assert len(catalog["effects"]) >= 90
    assert set(catalog["reviewed_characters"]) == set(load_panel_catalog()["characters"])
    for rule in catalog["effects"]:
        assert rule["source"]["url"].startswith("https://wiki.kurobbs.com/mc/item/")
        assert len(rule["source"]["content_sha256"]) == 64
        assert "非戰鬥" not in rule["condition"]
    for character in load_panel_catalog()["characters"].values():
        assert [r["level"] for r in character["resonance_chains"]] == list(range(1, 7))


def test_unconfirmed_missing_panel_target_and_wrong_dataset_stay_unknown():
    detail = combat_fixture()
    no_weapon = evaluate_build(replace(detail, weapon=None))
    assert no_weapon.sufficient is None
    assert no_weapon.primary.index is None
    goal = deepcopy(detail.cultivation_goal)
    goal["combat_teams"][0]["confirmed"] = False
    assert evaluate_build(replace(detail, cultivation_goal=goal)).sufficient is None


def test_team_serialization_validates_duplicates_and_nonfinite_values():
    team = CombatTeam("t", "測試", ("a", "b"), 120, confirmed=True, outro_id="a")
    assert teams_from_goal({"combat_teams": [team_to_json(team)]}) == (team,)
    for target in (float("nan"), float("inf"), True, 99):
        with pytest.raises(ValueError):
            replace(team, energy_target=target)
    with pytest.raises(ValueError):
        replace(team, member_ids=("a", "a"))


def test_provider_rank_chain_mode_and_next_character_limits():
    detail = combat_fixture()
    provider = replace(
        detail, summary=replace(detail.summary, id="provider", name="琳奈", resonance_chain=2)
    )
    team = replace(
        teams_from_goal(detail.cultivation_goal)[0], member_ids=("provider",), outro_id="provider"
    )
    effects = available_effects(detail, team, {"provider": provider})
    assert any(dict(e.values).get("resonance_liberation_deepen") == 25 for e in effects)
    assert any(dict(e.values).get("deepen") == 25 for e in effects)
    assert not any(
        "延奏" in e.label
        for e in available_effects(detail, replace(team, outro_id=None), {"provider": provider})
    )


def test_exceptional_anchor_can_be_repaired_by_other_slot_without_stealing():
    detail = combat_fixture(target=145, secondary=145)
    old = detail.echo_slots[4]
    anchor = replace(
        old,
        echo_id="anchor",
        sub_stats=(
            _stat("暴擊", 10.5),
            _stat("暴擊傷害", 21),
            _stat("攻擊", 11.6),
            _stat("攻擊", 60, None),
            _stat("共鳴解放傷害加成", 11.6),
        ),
    )
    anchored = with_candidate(detail, 5, anchor)
    assert evaluate_build(anchored).sufficient is False
    # A real free piece replaces position4 and restores all required teams.
    repair = replace(
        detail.echo_slots[3],
        echo_id="free",
        name="另一種類",
        sub_stats=(*detail.echo_slots[3].sub_stats[:4], _stat("共鳴效率", 12.4)),
    )
    # Need an additional6 points, so this inventory alone cannot close the gap.
    assert find_compensation(detail, anchored, (repair,), {}, 5) == ()
    teams = [replace(t, energy_target=139) for t in teams_from_goal(detail.cultivation_goal)]
    detail = replace(
        detail,
        cultivation_goal={
            **detail.cultivation_goal,
            "combat_teams": [team_to_json(t) for t in teams],
        },
    )
    plans = find_compensation(detail, with_candidate(detail, 5, anchor), (repair,), {}, 5)
    assert plans and plans[0].replacements[0][0] == 4
    assert plans[0].comparison.after.sufficient is True
    assert all(p != 5 for p, _, _ in plans[0].replacements)


def test_compensation_can_need_all_four_remaining_slots_and_never_duplicate_inventory():
    detail = combat_fixture(target=149.2, secondary=149.2)
    old = detail.echo_slots[4]
    old = replace(old, sub_stats=(*old.sub_stats[:4], _stat("共鳴效率", 12.4)))
    detail = with_candidate(detail, 5, old)
    anchor = replace(
        old,
        echo_id="anchor",
        sub_stats=(
            _stat("暴擊", 10.5),
            _stat("暴擊傷害", 21),
            _stat("攻擊", 11.6),
            _stat("攻擊", 60, None),
            _stat("共鳴技能傷害加成", 11.6),
        ),
    )
    inventory = tuple(
        replace(
            s,
            echo_id=f"free-{s.slot_index}",
            name=s.name if s.slot_index == 1 else f"新種類{s.slot_index}",
            sub_stats=(*s.sub_stats[:4], _stat("共鳴效率", 12.4)),
        )
        for s in detail.echo_slots[:4]
    )
    plans = find_compensation(detail, with_candidate(detail, 5, anchor), inventory, {}, 5)
    assert plans and len(plans[0].replacements) == 4
    assert len(set(i for _, i, _ in plans[0].replacements)) == 4
    assert plans[0].comparison.after.sufficient is True


def test_shorekeeper_buffs_use_real_provider_energy_and_never_assume_caps():
    detail = combat_fixture(target=140, secondary=140)
    provider = replace(
        panel_fixture(),
        summary=replace(detail.summary, id="shore", name="守岸人"),
        weapon=replace(detail.weapon, name="奇幻變奏"),
    )
    team = CombatTeam("main", "主隊", ("shore",), 140, confirmed=True)
    detail = replace(
        detail, cultivation_goal={**detail.cultivation_goal, "combat_teams": [team_to_json(team)]}
    )
    build = evaluate_build(detail, {"shore": provider})
    converted = [e for e in build.primary.effects if "充能轉換" in e.label]
    assert len(converted) == 2
    rate = next(dict(e.values)["crit_rate"] for e in converted if "crit_rate" in dict(e.values))
    assert 0 < rate < 12.5
    # Same provider's own settings must not recursively change this team's stats.
    provider = replace(provider, cultivation_goal={**provider.cultivation_goal, "combat_teams": []})
    assert evaluate_build(detail, {"shore": provider}).primary.totals == build.primary.totals


def test_aimisi_modes_stack_only_compatible_actors_and_chain_replaces_stack_requirement():
    detail = replace(
        combat_fixture(),
        summary=replace(panel_fixture().summary, name="愛彌斯"),
        weapon=replace(panel_fixture().weapon, name="永遠的啟明星"),
    )
    team = CombatTeam(
        "main", "主隊", energy_target=120, confirmed=True, modes=((detail.summary.id, "聚爆"),)
    )
    effects = available_effects(detail, team, {})
    own = next(e for e in effects if "星與星之間" in e.label)
    assert dict(own.values)["crit_damage"] == 30
    full = replace(detail, summary=replace(detail.summary, resonance_chain=3))
    assert (
        dict(next(e for e in available_effects(full, team, {}) if "星與星之間" in e.label).values)[
            "crit_damage"
        ]
        == 60
    )


def test_guaranteed_crit_window_has_zero_rate_gain_but_damage_still_matters():
    detail = replace(
        combat_fixture(),
        summary=replace(panel_fixture().summary, name="守岸人"),
        weapon=replace(panel_fixture().weapon, name="奇幻變奏"),
    )
    build = evaluate_build(detail)
    values = dict(build.primary.totals)
    assert values["guaranteed_crit"] == 1
    before = build_index(detail, values)
    assert build_index(detail, {**values, "crit_rate": values["crit_rate"] + 10.5}) == before
    assert build_index(detail, {**values, "crit_damage": values["crit_damage"] + 21}) > before


def test_scoped_crit_damage_never_leaks_into_gabriella_liberation():
    detail = replace(
        combat_fixture(),
        summary=replace(panel_fixture().summary, name="嘉貝莉娜", resonance_chain=1),
    )
    liberation = CombatTeam("main", "主隊", energy_target=120, confirmed=True)
    heavy = replace(liberation, damage_type="heavy_attack")
    assert not any("餘火滿層" in e.label for e in available_effects(detail, liberation, {}))
    assert any("餘火滿層" in e.label for e in available_effects(detail, heavy, {}))


def test_feasibility_precedes_output_and_primary_ties_allow_secondary_improvement():
    detail = combat_fixture(target=149, secondary=149)
    before = evaluate_build(detail)
    piece = replace(
        detail.echo_slots[4],
        echo_id="replacement",
        sub_stats=(
            _stat("暴擊", 6.3),
            _stat("暴擊傷害", 12.6),
            _stat("攻擊", 6.4),
            _stat("攻擊", 30, None),
            _stat("共鳴效率", 12.4),
        ),
    )
    lower = evaluate_build(with_candidate(detail, 5, piece))
    restored = compare_builds(before, lower)
    assert before.sufficient is False and lower.sufficient is True
    assert restored.primary_delta < 0 and restored.improves
    before = evaluate_build(combat_fixture())
    secondary = replace(
        before, teams=(before.teams[0], replace(before.teams[1], index=before.teams[1].index + 0.1))
    )
    assert compare_builds(before, secondary).primary_delta == 0
    assert compare_builds(before, secondary).improves
    weaker_primary = replace(
        secondary,
        teams=(
            replace(secondary.teams[0], index=secondary.teams[0].index - 0.1),
            secondary.teams[1],
        ),
    )
    assert not compare_builds(before, weaker_primary).improves
