"""Offline, non-combat panel arithmetic with explicit provenance and unknown inputs.

Percentages use percentage points (5 means 5%). Base properties are integers.
HP/ATK/DEF precision depends on the source of a contribution. Skill HP nodes
become separately truncated flat HP; Echo main/substat ratios have distinct paths.
This is a measured model, not an assertion of official engine implementation.
This module does not change Echo scores or claim that a calculated panel was observed.
"""

from __future__ import annotations

import json
import struct
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from functools import lru_cache
from hashlib import sha256
from importlib.resources import files
from math import isfinite
from typing import TYPE_CHECKING, Any, Literal

from wuwa_assistant.models.names import canonical_character_name
from wuwa_assistant.scoring.data import load_roll_data

if TYPE_CHECKING:
    from .characters import CharacterDetail

STAT_LABELS = {
    "hp": "生命值",
    "attack": "攻擊力",
    "defense": "防禦力",
    "crit_rate": "暴擊",
    "crit_damage": "暴擊傷害",
    "energy_regen": "共鳴效率",
    "healing_bonus": "治療效果加成",
    "aero_damage": "氣動傷害加成",
    "fusion_damage": "熱熔傷害加成",
    "glacio_damage": "冷凝傷害加成",
    "electro_damage": "導電傷害加成",
    "spectro_damage": "衍射傷害加成",
    "havoc_damage": "湮滅傷害加成",
    "physical_damage": "物理傷害加成",
    "basic_attack_damage": "普攻傷害加成",
    "heavy_attack_damage": "重擊傷害加成",
    "resonance_skill_damage": "共鳴技能傷害加成",
    "resonance_liberation_damage": "共鳴解放傷害加成",
    "echo_damage": "聲骸技能傷害加成",
    "coordinated_damage": "協同攻擊傷害加成",
    "tune_break_boost": "諧度破壞增幅",
    "tune_strain_accumulation": "偏諧值累積效率",
}
CORE_STATS = tuple(STAT_LABELS)[:6]
ELEMENT_STATS = tuple(STAT_LABELS)[7:13]
MINIMUM_PANEL_CONFIDENCE = 0.85  # Same acceptance floor as the existing Echo scorer.
_ATTRIBUTE_NAMES = {
    "暴擊": "crit_rate",
    "暴擊率": "crit_rate",
    "暴擊傷害": "crit_damage",
    "共鳴效率": "energy_regen",
    "治療效果加成": "healing_bonus",
    "普攻傷害加成": "basic_attack_damage",
    "重擊傷害加成": "heavy_attack_damage",
    "共鳴技能傷害加成": "resonance_skill_damage",
    "共鳴解放傷害加成": "resonance_liberation_damage",
    **{label: key for key, label in STAT_LABELS.items() if key.endswith("_damage")},
}
_FLAT_NAMES = {
    "生命": "flat_hp",
    "生命值": "flat_hp",
    "攻擊": "flat_attack",
    "攻擊力": "flat_attack",
    "防禦": "flat_defense",
    "防禦力": "flat_defense",
}
_PCT_NAMES = {
    "生命": "hp_pct",
    "生命值": "hp_pct",
    "攻擊": "attack_pct",
    "攻擊力": "attack_pct",
    "防禦": "defense_pct",
    "防禦力": "defense_pct",
}
_EXTRA_LABELS = {
    "hp_pct": "生命加成",
    "attack_pct": "攻擊加成",
    "defense_pct": "防禦加成",
    "flat_hp": "固定生命",
    "flat_attack": "固定攻擊",
    "flat_defense": "固定防禦",
    "weapon_attack": "武器基礎攻擊",
    "base_hp": "角色基礎生命",
    "base_attack": "角色基礎攻擊",
    "base_defense": "角色基礎防禦",
}


@dataclass(frozen=True, slots=True)
class StatContribution:
    source: str
    stat: str
    value: Decimal
    reference: str | None = None
    in_panel: bool = True
    input_percent: Decimal | None = None


@dataclass(frozen=True, slots=True)
class CharacterPanel:
    """Known subtotal remains auditable when any input prevents a complete panel."""

    totals: tuple[tuple[str, Decimal], ...]
    contributions: tuple[StatContribution, ...]
    missing: tuple[str, ...]
    notes: tuple[str, ...]
    node_labels: tuple[str, ...] = ()
    source_version: str = ""
    bases: tuple[tuple[str, Decimal], ...] = ()
    game_verified: bool = False
    comparison_differences: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.missing

    def value(self, key: str) -> Decimal | None:
        return dict(self.totals).get(key) if self.complete else None

    def display(self, key: str) -> str:
        value = self.value(key)
        if value is None:
            return "待補資料"
        return format_stat(key, value)

    def details(self) -> str:
        status = (
            "已與登記的遊戲面板逐項一致"
            if self.game_verified
            else f"已登記遊戲面板;仍有{len(self.comparison_differences)}項差異"
            if self.comparison_differences
            else "計算完成;本配置尚未逐項實測"
            if self.complete
            else "資料不足,尚無完整面板"
        )
        lines = [
            status,
            "範圍:非戰鬥常態;排除臨時/觸發增益。",
            "依已儲存資料計算,編輯後請儲存角色。",
        ]
        if self.missing:
            lines += ["", "待補資料:", *(f"• {m}" for m in self.missing)]
        if self.comparison_differences:
            lines += ["", "遊戲對照差異:", *self.comparison_differences]
        lines += ["", "完整計算值" if self.complete else "已知項目小計(不是完整面板)"]
        lines += [
            f"{STAT_LABELS[k]}:{format_stat(k, v)}(未截位 {v.normalize():f})"
            for k, v in self.totals
        ]
        if self.bases:
            lines += ["", "基礎值 + 額外加成"]
            totals = dict(self.totals)
            lines += [f"{STAT_LABELS[k]}:{v} + {int(totals[k]) - int(v)}" for k, v in self.bases]
        lines += ["", "來源明細(保留有效計算精度)"]
        for item in self.contributions:
            label = STAT_LABELS.get(item.stat, _EXTRA_LABELS.get(item.stat, item.stat))
            unit = (
                ""
                if item.stat.startswith(("base_", "flat_"))
                or item.stat in {"weapon_attack", "tune_break_boost"}
                else "%"
            )
            scope = "(不顯示於角色面板)" if not item.in_panel else ""
            conversion = (
                f"(生命加成{item.input_percent.normalize():f}%,逐節點取整)"
                if item.input_percent is not None
                else ""
            )
            lines.append(
                f"{item.source} → {label} +{item.value.normalize():f}{unit}{conversion}{scope}"
            )
        lines += [
            "",
            "計算方式:",
            "生命/防禦=角色基礎 *(1+百分比加成)+固定值。",
            "攻擊=(角色基礎攻擊+武器基礎攻擊)*(1+攻擊加成)+固定攻擊。",
            "角色/武器成長後基礎值各自取整,再相加。",
            "生命/攻擊/防禦百分比以單精度比率轉整數萬分位後加算。",
            "聲骸主/次詞條與副詞條分別換算;生命屬性節點逐筆轉固定生命。",
            "雙暴/效率與傷害百分比採加算;暴擊面板不截在100%。",
            "最終整數向下截位;百分比顯示一位小數。首位聲骸/固有技能被動另列。",
            "",
            *self.notes,
            "",
            f"數值資料版本:{self.source_version}",
            "來源:",
        ]
        refs = dict.fromkeys(c.reference for c in self.contributions if c.reference)
        lines.extend(str(ref) for ref in refs)
        return "\n".join(lines)


def format_stat(key: str, value: Decimal) -> str:
    if key in {"hp", "attack", "defense", "tune_break_boost"}:
        return str(value.quantize(Decimal(1), rounding=ROUND_DOWN))
    return f"{value.quantize(Decimal('0.1'), rounding=ROUND_DOWN)}%"


def _float32(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", value))[0]


def _ratio_percent(value: Decimal, growth: Decimal = Decimal(1)) -> Decimal:
    """Empirical ratio quantization being checked against recorded game panels.

    Only HP/ATK/DEF multipliers use this path. Crit/ER/damage properties already
    use integer percentage points in the source. Growing a weapon happens before
    conversion to integer basis points; never grow its truncated display card.
    """
    raw = _float32(float(value / 100))
    points = Decimal.from_float(raw) * growth * 10000
    return points.quantize(Decimal(1), rounding=ROUND_DOWN) / 100


def _echo_substat_percent(stat: str, value: Decimal) -> Decimal:
    """Convert a displayed roll independently of main stats and weapon growth.

    Keep the multiply back to basis points in float32, too: promoting to
    Decimal before that multiply drops some rolls by a whole basis point.
    HP/DEF use the basis-point scaling path; ATK starts with a direct ratio.
    Both paths are shared across roles, with no calibrated per-roll constants.
    """
    if stat == "attack_pct":
        ratio = _float32(float(value / 100))
    else:
        ratio = _float32(_float32(float(value * 100)) * _float32(0.0001))
    points = Decimal.from_float(_float32(ratio * 10000))
    return points.quantize(Decimal(1), rounding=ROUND_DOWN) / 100


def panel_input_fingerprint(detail: CharacterDetail) -> str:
    """Bind an explicit game observation to the equipment and settings it measured.

    Exclude scores, notes and timestamps: changing a score is not a stat change.
    Keep observed stat metadata, skill levels and slot identities so replacement,
    re-recognition and edits conservatively require another comparison.
    """
    weapon = detail.weapon
    inputs = {
        "name": canonical_character_name(detail.summary.name),
        "level": detail.summary.level,
        "chain": detail.summary.resonance_chain,
        "config": detail.cultivation_goal.get("panel_config", {}),
        "nodes": detail.small_stat_nodes,
        "skills": sorted((s.skill_key, s.current_level) for s in detail.skills),
        "weapon": (weapon.id, weapon.name, weapon.level, weapon.resonance_rank) if weapon else None,
        "echoes": [
            (
                s.slot_index,
                s.echo_id,
                s.name,
                s.set_name,
                s.cost,
                s.level,
                s.main_stats,
                s.sub_stats,
            )
            for s in sorted(detail.echo_slots, key=lambda s: s.slot_index)
        ],
    }
    encoded = json.dumps(inputs, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _observation_comparison(
    detail: CharacterDetail, totals: dict[str, Decimal], bases: dict[str, Decimal]
) -> tuple[str, ...] | None:
    observed = detail.cultivation_goal.get("panel_observation")
    if (
        not isinstance(observed, dict)
        or observed.get("source") != "user_game_panel"
        or observed.get("input_sha256") != panel_input_fingerprint(detail)
    ):
        return None
    values = observed.get("values")
    base_values = observed.get("bases")
    if not isinstance(values, dict) or not set(CORE_STATS).issubset(values):
        return None
    if not isinstance(base_values, dict) or set(base_values) != set(bases):
        return None
    differences = []
    for key, raw in {**values, **{f"base_{k}": v for k, v in base_values.items()}}.items():
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not isfinite(raw):
            return None
        value = Decimal(str(raw))
        if key.startswith("base_"):
            stat = key[5:]
            if bases.get(stat) != value:
                differences.append(f"{STAT_LABELS[stat]}基礎值: 遊戲{value} / 計算{bases[stat]}")
        elif key not in totals:
            return None
        elif format_stat(key, totals[key]) != format_stat(key, value):
            expected = format_stat(key, value)
            actual = format_stat(key, totals[key])
            differences.append(f"{STAT_LABELS[key]}: 遊戲{expected} / 計算{actual}")
    return tuple(differences)


@lru_cache(maxsize=1)
def load_panel_catalog() -> dict[str, Any]:
    """Read the bundled numeric snapshot only; runtime never contacts a website."""
    path = files("wuwa_assistant.data.characters").joinpath("panel-stats.v1.json")
    result: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if result.get("schema_version") != 1:
        raise ValueError("unsupported character panel catalog")
    return result


def ascension_options(level: int) -> tuple[int, ...]:
    return tuple(r[1] for r in load_panel_catalog()["role_growth"] if r[0] == level)


def _phase(level: int, requested: object) -> int | None:
    options = ascension_options(level)
    if requested is None:
        return options[0] if len(options) == 1 else None
    if type(requested) is int and requested in options:
        return requested
    return None


def _lookup(section: dict[str, Any], name: str) -> tuple[str, dict[str, Any]] | None:
    for key, entry in section.items():
        if name == key or name in entry.get("aliases", ()):
            return key, entry
    return None


def _echo_name(name: str) -> str:
    # 異相 is a cosmetic appearance. Never strip the mechanically distinct 夢魘 prefix.
    return name.removeprefix("異相·").removeprefix("異相・")


@lru_cache(maxsize=1)
def _substat_rolls() -> dict[str, tuple[Decimal, ...]]:
    return {
        key.value: tuple(Decimal(str(v)) for v in values)
        for key, values in load_roll_data().roll_values.items()
    }


def calculate_character_panel(detail: CharacterDetail) -> CharacterPanel:
    catalog = load_panel_catalog()
    missing: list[str] = []
    notes = [
        "計算模型僅代表已對照配置;其他配置仍待實測。不自動套用至聲骸評分。",
        "數值含公開成長倍率;WIKI 個別突破表的矛盾未作為計算輸入。",
        "遊戲對照前請清除觸發加成;脫離戰鬥不一定會清除所有已累積效果。",
    ]
    observed = detail.cultivation_goal.get("panel_observation")
    if isinstance(observed, dict) and observed.get("measurement_method") == "user_supplied_ocr":
        notes.append("登記面板來自使用者提供的影像辨識結果;有疑點時需核對遊戲原值。")
    contributions: list[StatContribution] = []
    sums: dict[str, Decimal] = defaultdict(Decimal)

    def add(
        label: str,
        effects: dict[str, Any],
        reference: str | None = None,
        *,
        growth: Decimal = Decimal(1),
        off_panel: bool = False,
        kind: Literal["default", "attribute_node", "echo_substat"] = "default",
    ) -> None:
        for stat, value in effects.items():
            if stat == "all_element_damage":
                add(
                    label,
                    dict.fromkeys(ELEMENT_STATS, value),
                    reference,
                    growth=growth,
                    off_panel=off_panel,
                    kind=kind,
                )
                continue
            amount = Decimal(str(value)) * growth
            input_percent = None
            if stat in {"hp_pct", "attack_pct", "defense_pct"}:
                if kind == "attribute_node" and stat == "hp_pct":
                    input_percent = Decimal(str(value))
                    ratio = Decimal.from_float(_float32(float(input_percent / 100)))
                    amount = (sums["base_hp"] * ratio).quantize(Decimal(1), rounding=ROUND_DOWN)
                    stat = "flat_hp"
                elif kind == "echo_substat":
                    amount = _echo_substat_percent(stat, Decimal(str(value)))
                else:
                    amount = _ratio_percent(Decimal(str(value)), growth)
            in_panel = not off_panel
            if in_panel:
                sums[stat] += amount
            contributions.append(
                StatContribution(label, stat, amount, reference, in_panel, input_percent)
            )

    name = canonical_character_name(detail.summary.name)
    character = _lookup(catalog["characters"], name)
    config = detail.cultivation_goal.get("panel_config", {})
    if not isinstance(config, dict):
        config = {}
        missing.append("角色面板設定格式不正確,請重新儲存")
    nodes: tuple[str, ...] = ()
    phase = _phase(detail.summary.level, config.get("character_ascension"))
    if character is None:
        missing.append(f"角色 {name} 尚無已校對基礎數值")
    else:
        _, entry = character
        ref = entry["source"]["url"]
        nodes = tuple(n["label"] for n in entry["nodes"])
        if phase is None:
            missing.append(f"角色等級 {detail.summary.level} 的突破階段未確認或不相符")
        else:
            growth = next(
                r for r in catalog["role_growth"] if r[:2] == [detail.summary.level, phase]
            )
            for stat, base, ratio in zip(
                ("hp", "attack", "defense"), entry["base"], growth[2:], strict=True
            ):
                value = (Decimal(str(base)) * Decimal(ratio) / 10000).quantize(
                    Decimal(1), rounding=ROUND_DOWN
                )
                add(
                    f"{name} Lv.{detail.summary.level}・{phase}階突破", {f"base_{stat}": value}, ref
                )
            notes.append(f"等級成長倍率:{catalog['growth_source']['url']}")
        add("角色初始面板", entry["initial_properties"], entry["initial_properties_source"]["url"])
        for unlocked, node in zip(detail.small_stat_nodes, entry["nodes"], strict=True):
            if unlocked:
                add(
                    f"技能節點・{node['label']}",
                    node["effects"],
                    ref,
                    off_panel=node["kind"] == "inherent",
                    kind="attribute_node" if node["kind"] == "attribute" else "default",
                )
        if (
            any(detail.small_stat_nodes)
            and not all(detail.small_stat_nodes)
            and config.get("node_layout") != 1
        ):
            missing.append("舊節點只有編號,請核對節點名稱後儲存以確認對應")
        if detail.summary.resonance_chain:
            missing.append("非零共鳴鏈的常駐效果尚待逐項校對")

    weapon = detail.weapon
    if weapon is None or weapon.name == "未指定武器":
        missing.append("目前裝備的武器名稱未登記")
    else:
        found = _lookup(catalog["weapons"], weapon.name)
        wp = _phase(weapon.level, config.get("weapon_ascension"))
        if found is None:
            missing.append(f"武器 {weapon.name} 尚無已校對數值")
        elif wp is None:
            missing.append(f"武器等級 {weapon.level} 的突破階段未確認或不相符")
        elif not 1 <= weapon.resonance_rank <= 5:
            missing.append("武器諧振階級需為1至5")
        else:
            _, entry = found
            if character is not None and character[1]["weapon_type"] != entry["weapon_type"]:
                missing.append(f"武器 {weapon.name} 的類型不適用於 {name}")
            ref = entry["source"]["url"]
            if "raw_property_source" in entry:
                notes.append(f"武器未截位屬性來源: {entry['raw_property_source']['url']}")
            for curve, effects in (
                (1, {"weapon_attack": entry["base_attack"]}),
                (2, entry["secondary"]),
            ):
                ratio = next(
                    r[3] for r in catalog["weapon_growth"] if r[:3] == [curve, weapon.level, wp]
                )
                grown = {k: Decimal(str(v)) * ratio / 10000 for k, v in effects.items()}
                if curve == 1:
                    grown = {
                        k: v.quantize(Decimal(1), rounding=ROUND_DOWN) for k, v in grown.items()
                    }
                add(
                    f"武器・{weapon.name} Lv.{weapon.level}・{wp}階突破",
                    effects if curve == 2 else grown,
                    ref,
                    growth=Decimal(ratio) / 10000 if curve == 2 else Decimal(1),
                )
            add(
                f"武器常駐被動・諧振{weapon.resonance_rank}階",
                {k: v[weapon.resonance_rank - 1] for k, v in entry["permanent_passive"].items()},
                ref,
            )

    counts: Counter[str] = Counter()
    species: set[tuple[str, str]] = set()
    echo_ids: set[str] = set()
    cost = 0
    for slot in detail.echo_slots:
        prefix = f"聲骸位置{slot.slot_index}"
        if slot.echo_id is None:
            missing.append(f"{prefix}尚未登記,無法確認是否空裝")
            continue
        if slot.echo_id in echo_ids:
            missing.append(f"{prefix}重複使用同一聲骸")
            continue
        echo_ids.add(slot.echo_id)
        if slot.level is None or not 0 <= slot.level <= 25:
            missing.append(f"{prefix}等級待確認")
        if slot.cost not in {1, 3, 4}:
            missing.append(f"{prefix}COST待確認")
        else:
            cost += slot.cost
        if len(slot.main_stats) != 2:
            missing.append(f"{prefix}需包含主要詞條與固定次要詞條")
        if slot.level is not None and len(slot.sub_stats) != min(slot.level // 5, 5):
            missing.append(f"{prefix}已開副詞條數量與等級不符")
        for group, stats in (("主/次詞條", slot.main_stats), ("副詞條", slot.sub_stats)):
            seen: set[str] = set()
            for observed in stats:
                parsed = _observed_stat(observed)
                if parsed is None:
                    missing.append(
                        f"{prefix}{group}有未確認或不支援的數值:{observed.get('name') or '未知'}"
                    )
                else:
                    key, value = parsed
                    if key in seen:
                        missing.append(f"{prefix}{group}重複詞條: {observed.get('name')}")
                        continue
                    seen.add(key)
                    if group == "副詞條" and not any(
                        abs(value - roll) < Decimal("0.000001")
                        for roll in _substat_rolls().get(key, ())
                    ):
                        missing.append(f"{prefix}副詞條數值不在已公布檔位: {observed.get('name')}")
                        continue
                    add(
                        f"{prefix}・{slot.name or '名稱未確認'}・{group}",
                        {key: value},
                        kind="echo_substat" if group == "副詞條" else "default",
                    )
        if not slot.name:
            missing.append(f"{prefix}名稱未確認,無法核對套裝重複種類")
        found_set = _lookup(catalog["sets"], slot.set_name or "")
        if found_set is None:
            missing.append(f"{prefix}套裝 {slot.set_name or '未知'} 尚無已校對效果")
        else:
            set_key, entry = found_set
            identity = _echo_name(slot.name or prefix)
            found_echo = _lookup(catalog["main_echoes"], identity)
            if found_echo:
                identity = found_echo[0]
            if (set_key, identity) not in species:
                counts[set_key] += 1
                species.add((set_key, identity))
        if slot.slot_index == 1:
            main = _lookup(catalog["main_echoes"], _echo_name(slot.name or ""))
            if main is None:
                missing.append(f"首位聲骸 {slot.name or '未知'} 的常駐效果尚未校對")
            else:
                key, entry = main
                add(f"首位聲骸・{key}", entry["effects"], entry["source"]["url"], off_panel=True)
                add(
                    f"首位聲骸・{key}・{name}專屬",
                    entry["by_character"].get(name, {}),
                    entry["source"]["url"],
                    off_panel=True,
                )
    if cost > 12:
        missing.append("已登記聲骸的總COST超過12")
    for key, count in counts.items():
        entry = catalog["sets"][key]
        if key == "不絕餘音" and count >= 5:
            missing.append("不絕餘音5件的在場累積層數未確認")
        for rule in entry["rules"]:
            if count >= rule["count"]:
                add(f"合鳴・{key} {rule['count']}件", rule["effects"], entry["source"]["url"])

    totals = {k: Decimal(sums[k]) for k in STAT_LABELS}
    bases = {}
    for stat, pct, flat in (
        ("hp", "hp_pct", "flat_hp"),
        ("attack", "attack_pct", "flat_attack"),
        ("defense", "defense_pct", "flat_defense"),
    ):
        base = Decimal(sums[f"base_{stat}"])
        if stat == "attack":
            base += Decimal(sums["weapon_attack"])
        bases[stat] = base
        totals[stat] = base * (1 + Decimal(sums[pct]) / 100) + Decimal(sums[flat])
    comparison = _observation_comparison(detail, totals, bases) if not missing else None
    return CharacterPanel(
        tuple(totals.items()),
        tuple(contributions),
        tuple(dict.fromkeys(missing)),
        tuple(notes),
        nodes,
        catalog["version"],
        tuple(bases.items()),
        comparison == (),
        comparison or (),
    )


def _observed_stat(stat: dict[str, Any]) -> tuple[str, Decimal] | None:
    value = stat.get("value")
    if (
        isinstance(value, bool)
        or not isinstance(value, (float, int))
        or not isfinite(value)
        or value < 0
    ):
        return None
    for field in ("confidence", "name_confidence", "value_confidence"):
        confidence = stat.get(field, 1)
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (float, int))
            or not isfinite(confidence)
            or not MINIMUM_PANEL_CONFIDENCE <= confidence <= 1
        ):
            return None
    name = str(stat.get("name", "")).strip()
    unit = stat.get("unit")
    if unit == "%":
        key = _PCT_NAMES.get(name) or _ATTRIBUTE_NAMES.get(name)
    elif unit in {None, ""}:
        key = _FLAT_NAMES.get(name)
    else:
        return None
    if key is None:
        return None
    return key, Decimal(str(value))
