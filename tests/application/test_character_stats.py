from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import pytest

from wuwa_assistant.application.character_stats import (
    ascension_options,
    calculate_character_panel,
    format_stat,
    load_panel_catalog,
    panel_input_fingerprint,
)
from wuwa_assistant.application.characters import (
    CharacterDetail,
    CharacterSummary,
    EchoSlotSnapshot,
    WeaponSnapshot,
)


def _stat(name: str, value: float, unit: str | None = "%") -> dict:
    return {"name": name, "value": value, "unit": unit, "confidence": 1.0}


def panel_fixture() -> CharacterDetail:
    """Synthetic equipment with a hand-worked full panel, no account data."""
    summary = CharacterSummary(
        id="character",
        data_set_id="dataset",
        name="珂萊塔",
        element="冷凝",
        role="主輸出",
        is_candidate=True,
        priority=1,
        level=90,
        resonance_chain=0,
        scoring_profile_id=None,
        scoring_profile_name=None,
        completed_skill_count=0,
        skill_count=5,
        completed_small_stat_nodes=10,
        equipped_echo_count=5,
        archived_at=None,
    )
    main = (
        (_stat("暴擊", 22), _stat("攻擊", 150, None)),
        (_stat("冷凝傷害加成", 30), _stat("攻擊", 100, None)),
        (_stat("攻擊", 30), _stat("攻擊", 100, None)),
        (_stat("攻擊", 18), _stat("生命", 2280, None)),
        (_stat("攻擊", 18), _stat("生命", 2280, None)),
    )
    sub = (
        _stat("暴擊", 7.5),
        _stat("暴擊傷害", 15),
        _stat("攻擊", 9.4),
        _stat("攻擊", 40, None),
        _stat("共鳴效率", 9.2),
    )
    slots = tuple(
        EchoSlotSnapshot(
            slot_index=i,
            echo_id=f"echo-{i}",
            name="異構武裝" if i == 1 else f"種類{i}",
            set_name="凌冽決斷之心",
            cost=cost,
            level=25,
            score=None,
            final_status="completed",
            main_stats=main[i - 1],
            sub_stats=sub,
        )
        for i, cost in enumerate((4, 3, 3, 1, 1), start=1)
    )
    return CharacterDetail(
        summary=summary,
        cultivation_goal={"panel_config": {"node_layout": 1}},
        small_stat_nodes=(True,) * 10,
        notes=None,
        weapon=WeaponSnapshot("weapon", "死與舞", "佩槍", 90, 1, None),
        skills=(),
        echo_slots=slots,
        teams=(),
        created_at="",
        updated_at="",
    )


def test_hand_worked_non_combat_panel_and_source_breakdown() -> None:
    panel = calculate_character_panel(panel_fixture())
    assert panel.complete, panel.missing
    # Integer bases462+500; effective ratios11.96% nodes,11.99% weapon,
    # 30%+18%+18% main,5*9.39% sub:136.90%. Flat ATK550.
    assert panel.value("attack") == Decimal("2828.978")
    assert panel.display("attack") == "2828"
    assert dict(panel.bases) == {"hp": 12450, "attack": 962, "defense": 1197}
    assert panel.value("hp") == 17010
    assert panel.value("defense") == 1197
    assert panel.value("crit_rate") == Decimal("72.5")
    assert panel.value("crit_damage") == 297
    assert panel.value("energy_regen") == 146
    assert panel.value("glacio_damage") == 30
    assert panel.value("resonance_skill_damage") == 12
    hidden = {c.stat: c.value for c in panel.contributions if not c.in_panel}
    assert hidden == {"glacio_damage": 12, "resonance_skill_damage": 12}
    assert "武器常駐被動" in panel.details()
    assert "本配置尚未逐項實測" in panel.details()
    assert "不顯示於角色面板" in panel.details()
    assert "武器基礎攻擊 +500" in panel.details()
    assert "暴擊傷害 +150%" in panel.details()
    assert len(panel.node_labels) == 10


def test_weapon_rank_changes_only_constant_passive() -> None:
    detail = panel_fixture()
    assert detail.weapon is not None
    old = calculate_character_panel(detail)
    new = calculate_character_panel(
        replace(detail, weapon=replace(detail.weapon, resonance_rank=5))
    )
    assert new.value("attack") - old.value("attack") == Decimal("115.44")
    assert new.value("crit_damage") == old.value("crit_damage")
    assert new.value("resonance_skill_damage") == old.value("resonance_skill_damage")


@pytest.mark.parametrize("name,chain", [("愛彌斯", 3), ("維里奈", 2)])
def test_reviewed_chains_do_not_add_triggered_bonuses_to_baseline(name, chain) -> None:
    detail = panel_fixture()
    detail = replace(
        detail,
        summary=replace(detail.summary, name=name),
        weapon=WeaponSnapshot(
            "weapon", "永遠的啟明星" if name == "愛彌斯" else "漪瀾浮錄", "", 90, 1, None
        ),
    )
    before = calculate_character_panel(detail)
    after = calculate_character_panel(
        replace(detail, summary=replace(detail.summary, resonance_chain=chain))
    )
    assert after.complete, after.missing
    assert after.totals == before.totals
    if name == "愛彌斯":
        assert any(
            c.stat == "crit_damage" and c.value == 60 and not c.in_panel
            for c in after.contributions
        )
        assert "條件未觸發" in after.details()
    else:
        assert "協奏" in after.details()


def test_unreviewed_chain_stays_incomplete() -> None:
    detail = panel_fixture()
    panel = calculate_character_panel(
        replace(detail, summary=replace(detail.summary, resonance_chain=1))
    )
    assert not panel.complete
    assert any("共鳴鏈1" in item for item in panel.missing)


def test_raw_weapon_hp_precision_and_public_card_truncation() -> None:
    # WeaponConf21020056 stores0.16050000488758087, not the16.0% card display.
    detail = panel_fixture()
    assert detail.weapon is not None
    panel = calculate_character_panel(
        replace(
            detail,
            summary=replace(detail.summary, name="卡提希婭"),
            weapon=replace(detail.weapon, name="不屈命定之冠"),
        )
    )
    # HP14800*(1+72.22% weapon+11.99% passive)+2*(266+621) nodes+4560.
    assert panel.value("hp") == Decimal("33597.08")
    crown = next(c for c in panel.contributions if c.stat == "hp_pct" and "Lv.90" in c.source)
    assert crown.value == Decimal("72.22")
    assert format_stat("hp_pct", crown.value) == "72.2%"
    # Public level50 ATK/crit weapon secondary shows23.5%, not23.6%.
    assert format_stat("crit_rate", Decimal("23.5552")) == "23.5%"


@pytest.mark.parametrize("level", [20, 40, 50, 60, 70, 80])
def test_ascension_boundary_requires_explicit_before_or_after(level: int) -> None:
    detail = panel_fixture()
    detail = replace(detail, summary=replace(detail.summary, level=level))
    unknown = calculate_character_panel(detail)
    assert unknown.value("hp") is None
    before, after = ascension_options(level)
    values = []
    for phase in (before, after):
        result = calculate_character_panel(
            replace(
                detail,
                cultivation_goal={"panel_config": {"character_ascension": phase, "node_layout": 1}},
            )
        )
        assert result.complete
        values.append(result.value("hp"))
    assert values[1] > values[0]


def test_all_supported_levels_have_complete_published_growth_rows() -> None:
    detail = panel_fixture()
    assert detail.weapon is not None
    for level in range(1, 91):
        for phase in ascension_options(level):
            result = calculate_character_panel(
                replace(
                    detail,
                    summary=replace(detail.summary, level=level),
                    weapon=replace(detail.weapon, level=level),
                    cultivation_goal={
                        "panel_config": {
                            "character_ascension": phase,
                            "weapon_ascension": phase,
                            "node_layout": 1,
                        }
                    },
                )
            )
            assert result.complete, (level, phase, result.missing)


@pytest.mark.parametrize(
    "change",
    [
        "missing_weapon",
        "unknown_weapon",
        "unknown_character",
        "missing_slot",
        "missing_fixed",
        "missing_substat",
        "low_confidence",
        "unknown_stat",
        "nan",
        "bool",
        "bad_unit",
        "unknown_set",
        "unknown_main",
        "duplicate_echo",
        "too_much_cost",
        "nonzero_chain",
        "incompatible_weapon",
        "duplicate_substat",
        "invalid_roll",
    ],
)
def test_incomplete_inputs_never_become_a_complete_panel(change: str) -> None:
    detail = panel_fixture()
    slot = detail.echo_slots[0]
    assert detail.weapon is not None
    if change == "missing_weapon":
        detail = replace(detail, weapon=None)
    elif change == "unknown_weapon":
        detail = replace(detail, weapon=replace(detail.weapon, name="未指定武器"))
    elif change == "incompatible_weapon":
        detail = replace(detail, weapon=replace(detail.weapon, name="曇切"))
    elif change == "unknown_character":
        detail = replace(detail, summary=replace(detail.summary, name="未知角色"))
    elif change == "nonzero_chain":
        detail = replace(detail, summary=replace(detail.summary, resonance_chain=1))
    else:
        if change == "missing_slot":
            slot = replace(slot, echo_id=None)
        elif change == "missing_fixed":
            slot = replace(slot, main_stats=slot.main_stats[:1])
        elif change == "missing_substat":
            slot = replace(slot, sub_stats=slot.sub_stats[:4])
        elif change == "duplicate_substat":
            slot = replace(slot, sub_stats=(slot.sub_stats[1], *slot.sub_stats[1:]))
        elif change == "unknown_set":
            slot = replace(slot, set_name="未知合鳴")
        elif change == "unknown_main":
            slot = replace(slot, name="未知首位聲骸")
        elif change == "duplicate_echo":
            slot = replace(slot, echo_id=detail.echo_slots[1].echo_id)
        elif change == "too_much_cost":
            detail = replace(
                detail, echo_slots=tuple(replace(s, cost=4) for s in detail.echo_slots)
            )
            slot = detail.echo_slots[0]
        else:
            altered = dict(slot.sub_stats[0])
            if change == "low_confidence":
                altered["confidence"] = 0.5
            elif change == "unknown_stat":
                altered["name"] = "未知詞條"
            elif change == "nan":
                altered["value"] = float("nan")
            elif change == "bool":
                altered["value"] = True
            elif change == "bad_unit":
                altered["unit"] = "?"
            elif change == "invalid_roll":
                altered["value"] = 99.9
            slot = replace(slot, sub_stats=(altered, *slot.sub_stats[1:]))
        detail = replace(detail, echo_slots=(slot, *detail.echo_slots[1:]))
    panel = calculate_character_panel(detail)
    assert panel.missing
    assert panel.value("crit_rate") is None
    assert panel.display("attack") == "待補資料"
    assert "已知項目小計" in panel.details()


def test_inherent_energy_does_not_enter_non_combat_character_sheet() -> None:
    detail = panel_fixture()
    assert detail.weapon is not None
    moning = calculate_character_panel(
        replace(
            detail,
            summary=replace(detail.summary, name="莫寧"),
            weapon=replace(detail.weapon, name="曇切"),
        )
    )
    shorekeeper = calculate_character_panel(
        replace(
            detail,
            summary=replace(detail.summary, name="守岸人"),
            weapon=replace(detail.weapon, name="存幀"),
        )
    )
    assert moning.value("energy_regen") == 146
    assert next(c for c in moning.contributions if "藍圖" in c.source).in_panel is False
    assert shorekeeper.value("energy_regen") == 146


def test_main_echo_character_bonus_and_cosmetic_variant() -> None:
    detail = panel_fixture()
    slot = replace(detail.echo_slots[0], name="共鳴迴響·芙露德莉斯")
    detail = replace(
        detail,
        summary=replace(detail.summary, name="卡提希婭"),
        weapon=replace(detail.weapon, name="不屈命定之冠"),
        echo_slots=(slot, *detail.echo_slots[1:]),
    )
    panel = calculate_character_panel(detail)
    assert panel.value("aero_damage") == 0
    assert sum(c.value for c in panel.contributions if not c.in_panel) == 20
    detail = replace(
        detail,
        summary=replace(detail.summary, name="珂萊塔"),
        weapon=replace(detail.weapon, name="死與舞"),
    )
    assert calculate_character_panel(detail).value("aero_damage") == 0
    slot = replace(slot, name="異相·爐芯機骸")
    assert (
        calculate_character_panel(replace(detail, echo_slots=(slot, *detail.echo_slots[1:]))).value(
            "energy_regen"
        )
        == 146
    )


def test_sonata_distinct_species_and_multiple_thresholds() -> None:
    detail = panel_fixture()
    slots = tuple(replace(s, set_name="高天共奏之曲") for s in detail.echo_slots)
    full = calculate_character_panel(replace(detail, echo_slots=slots))
    assert full.value("energy_regen") == 156
    assert full.value("coordinated_damage") == 80
    slots = tuple(replace(s, name="異構武裝") for s in slots)
    duplicate_species = calculate_character_panel(replace(detail, echo_slots=slots))
    assert duplicate_species.value("energy_regen") == 146
    assert duplicate_species.value("coordinated_damage") == 0


def test_partial_legacy_nodes_need_named_layout_confirmation() -> None:
    detail = replace(panel_fixture(), cultivation_goal={}, small_stat_nodes=(True,) + (False,) * 9)
    panel = calculate_character_panel(detail)
    assert any("舊節點" in m for m in panel.missing)
    confirmed = replace(detail, cultivation_goal={"panel_config": {"node_layout": 1}})
    assert calculate_character_panel(confirmed).complete


def test_source_coverage_and_anomalies_are_explicit() -> None:
    data = load_panel_catalog()
    assert len(data["characters"]) == 20
    assert len(data["weapons"]) == 17
    assert len(data["sets"]) == 20
    for kind in ("characters", "weapons", "sets", "main_echoes"):
        for item in data[kind].values():
            assert item["source"]["url"].startswith("https://wiki.kurobbs.com/mc/item/")
            assert len(item["source"]["content_sha256"]) == 64
    assert len(data["source_anomalies"]) == 4
    assert data["characters"]["莫寧"]["base"] == [1230, 23, 111]


@pytest.mark.parametrize(
    ("name", "role", "attack", "secondary", "expected"),
    [
        ("奇幻變奏", "守岸人", 337, "energy_regen", "197.8%"),
        ("異響空靈", "莫寧", 337, "energy_regen", "197.8%"),
        ("相位漣漪", "琳奈", 587, "crit_damage", "273.6%"),
        ("漪瀾浮錄", "維里奈", 500, "energy_regen", "162.0%"),
        ("停駐之煙", "蕾貝卡", 587, "energy_regen", "162.0%"),
        ("凋亡頻移", "露帕", 462, "energy_regen", "146.0%"),
    ],
)
def test_new_weapon_base_secondary_and_only_unconditional_passives(
    name: str, role: str, attack: int, secondary: str, expected: str
) -> None:
    detail = panel_fixture()
    panel = calculate_character_panel(
        replace(
            detail,
            summary=replace(detail.summary, name=role),
            small_stat_nodes=(False,) * 4 + (True,) * 2 + (False,) * 4,
            weapon=replace(detail.weapon, name=name, resonance_rank=2),
        )
    )
    assert panel.complete, panel.missing
    assert next(c.value for c in panel.contributions if c.stat == "weapon_attack") == attack
    assert panel.display(secondary) == expected
    if name in {"奇幻變奏", "異響空靈", "凋亡頻移"}:
        assert not any("武器常駐被動" in c.source for c in panel.contributions)


def test_game_comparison_is_bound_to_measured_inputs_and_values() -> None:
    detail = panel_fixture()
    # A synthetic observation; real account records remain local and private.
    observed = {
        "source": "user_game_panel",
        "measurement_method": "user_supplied_ocr",
        "input_sha256": panel_input_fingerprint(detail),
        "bases": {"hp": 12450, "attack": 962, "defense": 1197},
        "values": {
            "hp": 17010,
            "attack": 2828,
            "defense": 1197,
            "crit_rate": 72.5,
            "crit_damage": 297,
            "energy_regen": 146,
        },
    }
    saved = replace(
        detail, cultivation_goal={**detail.cultivation_goal, "panel_observation": observed}
    )
    assert calculate_character_panel(saved).game_verified
    assert "已與登記的遊戲面板逐項一致" in calculate_character_panel(saved).details()
    assert "影像辨識結果" in calculate_character_panel(saved).details()
    assert not calculate_character_panel(
        replace(saved, small_stat_nodes=(False,) * 10)
    ).game_verified
    assert not calculate_character_panel(
        replace(saved, weapon=replace(saved.weapon, resonance_rank=5))
    ).game_verified
    wrong = {**observed, "values": {**observed["values"], "hp": 12345}}
    mismatched = calculate_character_panel(
        replace(saved, cultivation_goal={**saved.cultivation_goal, "panel_observation": wrong})
    )
    assert not mismatched.game_verified
    assert mismatched.comparison_differences == ("生命值: 遊戲12345 / 計算17010",)
    assert "仍有1項差異" in mismatched.details()


def test_character_initial_tune_properties_use_published_base_properties() -> None:
    data = load_panel_catalog()["characters"]
    assert data["莫寧"]["initial_properties"]["tune_break_boost"] == 10
    assert data["蕾貝卡"]["initial_properties"]["tune_break_boost"] == 10
    assert data["珂萊塔"]["initial_properties"]["tune_break_boost"] == 0
    assert format_stat("tune_break_boost", Decimal(10)) == "10"
    for entry in data.values():
        assert entry["initial_properties"]["tune_strain_accumulation"] == 100
        assert len(entry["initial_properties_source"]["sha256"]) == 64


@pytest.mark.parametrize(
    ("name", "roll", "stat", "expected"),
    [
        ("生命", 10.9, "hp", "13807.05"),
        ("攻擊", 7.1, "attack", "1450.7010"),
        ("防禦", 9.0, "defense", "1304.6103"),
        ("生命", 6.4, "hp", "13245.555"),
        ("生命", 11.6, "hp", "13894.2"),
        ("攻擊", 10.9, "attack", "1487.257"),
        ("防禦", 10.9, "defense", "1327.473"),
    ],
)
def test_substat_precision_uses_origin_and_preserves_raw_roll(
    name: str, roll: float, stat: str, expected: str
) -> None:
    # One synthetic C4 Echo; flat ATK150+40. Separate effective rolls are
    # HP10.90%, ATK7.10%, DEF8.99%; none are taken from the observed total.
    detail = panel_fixture()
    slot = replace(
        detail.echo_slots[0],
        sub_stats=(
            _stat("暴擊", 7.5),
            _stat("暴擊傷害", 15),
            _stat("攻擊", 40, None),
            _stat("共鳴效率", 9.2),
            _stat(name, roll),
        ),
    )
    detail = replace(detail, echo_slots=(slot,))
    fingerprint = panel_input_fingerprint(detail)
    panel = calculate_character_panel(detail)
    assert panel.complete, panel.missing
    assert panel.value(stat) == Decimal(expected)
    assert slot.sub_stats[-1]["value"] == roll
    assert panel_input_fingerprint(detail) == fingerprint
    # Main ATK18% remains on its own conversion path; converting it as a
    # substat would lose0.01 percentage points per C1 Echo.
    full = calculate_character_panel(panel_fixture())
    main = [
        c.value for c in full.contributions if c.stat == "attack_pct" and "主/次詞條" in c.source
    ]
    assert main == [Decimal(30), Decimal(18), Decimal(18)]


def test_hp_nodes_truncate_each_node_and_explain_the_original_percentage(monkeypatch) -> None:
    # Synthetic level1 base1013. Two1.8% and two4.2% nodes give18+42+18+42,
    # not floor(1013*12%)=121. No weapon HP and no Echoes in this case.
    catalog = deepcopy(load_panel_catalog())
    catalog["characters"]["守岸人"]["base"][0] = 1013
    monkeypatch.setattr(
        "wuwa_assistant.application.character_stats.load_panel_catalog", lambda: catalog
    )
    detail = panel_fixture()
    detail = replace(
        detail,
        summary=replace(detail.summary, name="守岸人", level=1),
        weapon=replace(detail.weapon, name="奇幻變奏", level=1),
        echo_slots=(),
    )
    panel = calculate_character_panel(detail)
    assert panel.complete, panel.missing
    assert panel.value("hp") == 1133
    nodes = [c for c in panel.contributions if c.input_percent is not None]
    assert [c.value for c in nodes] == [18, 42, 18, 42]
    assert [c.input_percent for c in nodes] == [Decimal("1.8"), Decimal("4.2")] * 2
    assert all(c.stat == "flat_hp" for c in nodes)
    assert "生命加成1.8%,逐節點取整" in panel.details()
