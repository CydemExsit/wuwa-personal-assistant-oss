"""Pure, versioned ideal-rotation build comparison; never a damage simulator.

Quality scores remain independent. Each required team evaluates the same complete
equipment snapshot, with capped ER requirements and a smooth crit preference.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from functools import lru_cache
from importlib.resources import files
from math import isfinite, log
from typing import Any

from wuwa_assistant.scoring.character_guides import guide_for_character

from .character_stats import (
    STAT_LABELS,
    CharacterPanel,
    _echo_name,
    _lookup,
    calculate_character_panel,
    load_panel_catalog,
)
from .characters import CharacterDetail, EchoSlotSnapshot

MODEL_VERSION = "combat-build.v1"
DAMAGE_TYPES = ("basic_attack", "heavy_attack", "resonance_skill", "resonance_liberation")
ELEMENT_KEYS = {
    "氣動": "aero_damage",
    "熱熔": "fusion_damage",
    "冷凝": "glacio_damage",
    "導電": "electro_damage",
    "衍射": "spectro_damage",
    "湮滅": "havoc_damage",
}
_EFFECT_LABELS = {
    **STAT_LABELS,
    "deepen": "全傷害加深",
    "all_element_damage": "全屬性傷害加成",
    "basic_attack_deepen": "普攻傷害加深",
    "heavy_attack_deepen": "重擊傷害加深",
    "resonance_skill_deepen": "共鳴技能傷害加深",
    "resonance_liberation_deepen": "共鳴解放傷害加深",
    "fusion_damage_deepen": "熱熔傷害加深",
    "glacio_damage_deepen": "冷凝傷害加深",
    "electro_damage_deepen": "導電傷害加深",
    "guaranteed_crit": "指定窗口必定暴擊",
}


@dataclass(frozen=True, slots=True)
class CombatTeam:
    identifier: str
    name: str
    member_ids: tuple[str, ...] = ()
    energy_target: float | None = None
    damage_type: str = "resonance_liberation"
    excluded_effects: tuple[str, ...] = ()
    modes: tuple[tuple[str, str], ...] = ()
    confirmed: bool = False
    outro_id: str | None = None

    def __post_init__(self) -> None:
        if not self.identifier.strip() or not self.name.strip():
            raise ValueError("隊伍名稱不可空白")
        if len(self.member_ids) > 2 or len(set(self.member_ids)) != len(self.member_ids):
            raise ValueError("隊友最多兩位且不可重複")
        if self.energy_target is not None and (
            isinstance(self.energy_target, bool)
            or not isfinite(self.energy_target)
            or not 100 <= self.energy_target <= 1000
        ):
            raise ValueError("隊伍充能目標需為100%至1000%")
        if self.damage_type not in DAMAGE_TYPES:
            raise ValueError("主要傷害類型不正確")
        if len(dict(self.modes)) != len(self.modes):
            raise ValueError("角色模態不可重複")
        if not isinstance(self.confirmed, bool):
            raise ValueError("輪轉確認需為布林值")


def teams_from_goal(goal: Mapping[str, Any]) -> tuple[CombatTeam, ...]:
    raw = goal.get("combat_teams", [])
    if not isinstance(raw, list):
        raise ValueError("戰鬥隊伍資料需重新儲存")
    result = []
    for row in raw:
        if not isinstance(row, dict):
            raise ValueError("戰鬥隊伍格式不正確")
        result.append(
            CombatTeam(
                identifier=str(row["id"]),
                name=str(row["name"]),
                member_ids=tuple(row.get("member_ids", [])),
                energy_target=row.get("energy_target"),
                damage_type=str(row.get("damage_type", "resonance_liberation")),
                excluded_effects=tuple(row.get("excluded_effects", [])),
                modes=tuple(sorted(row.get("modes", {}).items())),
                confirmed=row.get("confirmed", False),
                outro_id=row.get("outro_id"),
            )
        )
    if len({t.identifier for t in result}) != len(result):
        raise ValueError("戰鬥隊伍識別不可重複")
    return tuple(result)


def team_to_json(team: CombatTeam) -> dict[str, Any]:
    return {
        "id": team.identifier,
        "name": team.name,
        "member_ids": list(team.member_ids),
        "energy_target": team.energy_target,
        "damage_type": team.damage_type,
        "excluded_effects": list(team.excluded_effects),
        "modes": dict(team.modes),
        "confirmed": team.confirmed,
        "outro_id": team.outro_id,
    }


@lru_cache(maxsize=1)
def load_combat_catalog() -> dict[str, Any]:
    return json.loads(
        files("wuwa_assistant.data.characters")
        .joinpath("combat-effects.v1.json")
        .read_text(encoding="utf-8")
    )


@dataclass(frozen=True, slots=True)
class CombatEffect:
    identifier: str
    label: str
    values: tuple[tuple[str, float], ...]
    source: str
    condition: str
    active: bool = True

    @property
    def text(self) -> str:
        values = "、".join(
            _EFFECT_LABELS[k]
            if k == "guaranteed_crit"
            else f"{_EFFECT_LABELS.get(k, k)} +{v:g}{'點' if k == 'tune_break_boost' else '%'}"
            for k, v in self.values
        )
        return f"{self.label} · {values}"


@dataclass(frozen=True, slots=True)
class TeamBuild:
    team: CombatTeam
    totals: tuple[tuple[str, float], ...]
    energy_target: float | None
    effects: tuple[CombatEffect, ...]
    missing: tuple[str, ...]
    index: float | None

    @property
    def energy_gap(self) -> float | None:
        if self.missing or self.energy_target is None:
            return None
        return max(0.0, self.energy_target - dict(self.totals)["energy_regen"])

    @property
    def sufficient(self) -> bool | None:
        gap = self.energy_gap
        return None if gap is None else gap <= 1e-7


@dataclass(frozen=True, slots=True)
class CombatBuild:
    teams: tuple[TeamBuild, ...]
    primary_id: str | None
    panel: CharacterPanel
    weights: tuple[tuple[str, float], ...] = ()

    @property
    def primary(self) -> TeamBuild | None:
        return next((t for t in self.teams if t.team.identifier == self.primary_id), None)

    @property
    def sufficient(self) -> bool | None:
        if not self.teams or any(t.sufficient is None for t in self.teams):
            return None
        return all(t.sufficient for t in self.teams)

    @property
    def summary(self) -> str:
        if not self.teams:
            return "尚未設定戰鬥隊伍"
        parts = []
        for row in self.teams:
            status = (
                "待補資料"
                if row.sufficient is None
                else "充能達標"
                if row.sufficient
                else f"充能缺{row.energy_gap:.1f}個百分點"
            )
            parts.append(
                f"{'主隊' if row.team.identifier == self.primary_id else row.team.name} {status}"
            )
        return " · ".join(parts)

    def details(self) -> str:
        lines = ["理想輪轉配裝指標, 並非實際傷害百分比。", f"模型 {MODEL_VERSION}"]
        for row in self.teams:
            values = dict(row.totals)
            effective_rate = (
                100.0 if values.get("guaranteed_crit", 0) else min(100, values.get("crit_rate", 0))
            )
            lines += [
                f"\n{row.team.name}",
                f"暴擊 {values.get('crit_rate', 0):.1f}% / "
                f"暴傷 {values.get('crit_damage', 0):.1f}% / "
                f"充能 {values.get('energy_regen', 0):.1f}% / "
                + ("需求待補" if row.energy_target is None else f"需求 {row.energy_target:g}%"),
                f"有效暴率 {effective_rate:.1f}%; "
                f"溢出 {max(0, values.get('crit_rate', 0) - 100):.1f}個百分點不計入配裝效益。",
            ]
            lines += [f"待補: {m}" for m in row.missing]
            lines += [
                f"{'採用' if e.active else '排除'} {e.text}\n條件: {e.condition}\n來源: {e.source}"
                for e in row.effects
            ]
        return "\n".join(lines)


def crit_preference(rate: float, damage: float) -> float:
    """Smooth crit utility with saturation, reliability preference and capped credit.

    The preference is a project policy, not a game formula. Its marginal bonus
    tends to zero near full crit; only the part at/below100 has any contribution.
    Equal-cost 1:2 CR/excess-CD has the usual low-stat balance before this bonus.
    """
    p = min(1.0, max(0.0, rate / 100))
    # This is score credit, not an altered probability. Low/mid crit remains
    # near linear; its derivative smoothly approaches zero at full crit, so a
    # tiny98->99 gain does not dominate an equal-resource CD improvement.
    credit = p - p**9 / 9
    utility = 1 + credit * max(0.0, damage / 100 - 1)
    return utility * (1 - 0.15 * (1 - p) ** 2)


def index_weights(detail: CharacterDetail) -> dict[str, float]:
    guide = guide_for_character(detail.summary.name)
    profile = None if guide is None else guide.profile()
    return {} if profile is None else {str(w.stat): w.weight for w in profile.weights}


def build_index(
    detail: CharacterDetail,
    totals: Mapping[str, float],
    weights: Mapping[str, float] | None = None,
) -> float:
    """Weighted stat utility; ER is exclusively a feasibility constraint."""
    weights = index_weights(detail) if weights is None else weights
    drivers = {k: weights.get(k + "_pct", 0.0) for k in ("attack", "hp", "defense")}
    top = max(drivers.values(), default=0) or 1.0
    result = sum(w / top * log(max(1.0, totals.get(k, 1))) for k, w in drivers.items())
    crit_weight = min(1.0, (weights.get("crit_rate", 1) + weights.get("crit_damage", 1)) / 2)
    rate = 100.0 if totals.get("guaranteed_crit", 0) else totals["crit_rate"]
    result += crit_weight * log(crit_preference(rate, totals["crit_damage"]))
    element = totals.get(ELEMENT_KEYS.get(detail.summary.element or "", ""), 0)
    bonus = element + totals.get("all_element_damage", 0) + totals.get("selected_damage", 0)
    result += log(max(0.01, 1 + bonus / 100))
    result += log(max(0.01, 1 + totals.get("deepen", 0) / 100))
    return result


def _set_counts(detail: CharacterDetail) -> Counter[str]:
    catalog = load_panel_catalog()
    found = set()
    for slot in detail.echo_slots:
        row = _lookup(catalog["sets"], slot.set_name or "")
        if slot.echo_id and row:
            found.add((row[0], _echo_name(slot.name or "")))
    return Counter(name for name, _ in found)


def _mode(actor: CharacterDetail, team: CombatTeam) -> str:
    return dict(team.modes).get(
        actor.summary.id, load_combat_catalog().get("default_modes", {}).get(actor.summary.name, "")
    )


def _actor_tags(actor: CharacterDetail, team: CombatTeam) -> set[str]:
    tags = set(load_combat_catalog().get("triggers", {}).get(actor.summary.name, []))
    mode = _mode(actor, team)
    if actor.summary.name in {"愛彌斯", "心", "達妮婭", "洛瑟菈"}:
        tags.intersection_update({mode})
    return tags


def available_effects(
    detail: CharacterDetail,
    team: CombatTeam,
    roster: Mapping[str, CharacterDetail],
) -> tuple[CombatEffect, ...]:
    """Derive effects from actual ownership, rank, nodes, species and team mode."""
    catalog = load_panel_catalog()
    result: list[CombatEffect] = []
    actors = [detail, *(roster[i] for i in team.member_ids if i in roster)]
    for actor in actors:
        counts = _set_counts(actor)
        for rule in load_combat_catalog()["effects"]:
            kind, owner = rule["kind"], rule["owner"]
            if kind == "character":
                matches = actor.summary.name == owner
            elif kind == "weapon":
                found = (
                    None if actor.weapon is None else _lookup(catalog["weapons"], actor.weapon.name)
                )
                matches = found is not None and found[0] == owner
            elif kind == "set":
                matches = counts[owner] >= rule["count"]
            else:
                first = actor.echo_slots[0]
                found = _lookup(catalog["main_echoes"], _echo_name(first.name or ""))
                matches = first.echo_id is not None and found is not None and found[0] == owner
            if not matches or actor.summary.resonance_chain < rule.get("chain", 0):
                continue
            node = rule.get("node")
            if node is not None and not actor.small_stat_nodes[node]:
                continue
            if rule.get("mode") and _mode(actor, team) != rule["mode"]:
                continue
            trigger = rule.get("trigger")
            actor_tags = _actor_tags(actor, team)
            if trigger is not None and trigger not in actor_tags:
                continue
            if (
                rule.get("max_chain") is not None
                and actor.summary.resonance_chain > rule["max_chain"]
            ):
                continue
            self_actor = actor.summary.id == detail.summary.id
            recipient = rule.get("recipient", "self")
            if (recipient == "self" and not self_actor) or (recipient == "next" and self_actor):
                continue
            if recipient == "next" and actor.summary.id != team.outro_id:
                continue
            if rule.get("scope", "all") not in {"all", team.damage_type}:
                continue
            if rule.get("scopes") and team.damage_type not in rule["scopes"]:
                continue
            identifier = f"{actor.summary.id}:{rule['id']}"
            values = {}
            for key, value in rule["values"].items():
                if isinstance(value, list):
                    if actor.weapon is None:
                        continue
                    value = value[actor.weapon.resonance_rank - 1]
                values[key] = float(value)
            stack = rule.get("stacks")
            if stack:
                count = sum(stack["tag"] in _actor_tags(member, team) for member in actors)
                amount = min(stack["cap"], count * stack["per_actor"])
                if actor.summary.resonance_chain >= stack.get("full_chain", 7) and count:
                    amount = stack["cap"]
                values = {key: float(amount) for key in values}
            result.append(
                CombatEffect(
                    identifier,
                    f"{actor.summary.name}・{rule['label']}",
                    tuple(values.items()),
                    rule["source"]["url"],
                    rule["condition"],
                    identifier not in team.excluded_effects,
                )
            )
    return tuple(result)


def _provider_totals(
    actor: CharacterDetail,
    detail: CharacterDetail,
    team: CombatTeam,
    roster: Mapping[str, CharacterDetail],
) -> tuple[dict[str, float], tuple[str, ...]]:
    source_panel = calculate_character_panel(actor)
    values = _panel_totals(source_panel, damage_type=team.damage_type)
    provider_team = replace(
        team,
        member_ids=tuple(
            dict.fromkeys(i for i in (detail.summary.id, *team.member_ids) if i != actor.summary.id)
        ),
    )
    for effect in available_effects(actor, provider_team, {**roster, detail.summary.id: detail}):
        if effect.active:
            for key, value in effect.values:
                values[key] = values.get(key, 0) + value
    return values, source_panel.missing


def _panel_totals(panel: CharacterPanel, *, damage_type: str) -> dict[str, float]:
    totals = {k: float(v) for k, v in panel.totals}
    bases = {k: float(v) for k, v in panel.bases}
    # These are guaranteed equipped main-Echo passives and known inherent nodes;
    # conditional chain effects are applied by sourced combat rules instead.
    for c in panel.contributions:
        if c.in_panel or c.source.startswith("共鳴鏈"):
            continue
        if c.stat in {"attack_pct", "hp_pct", "defense_pct"}:
            key = c.stat.removesuffix("_pct")
            totals[key] += bases[key] * float(c.value) / 100
        else:
            totals[c.stat] = totals.get(c.stat, 0) + float(c.value)
    totals["selected_damage"] = totals.get(damage_type + "_damage", 0)
    return totals


def evaluate_build(
    detail: CharacterDetail,
    roster: Mapping[str, CharacterDetail] | None = None,
    *,
    weights: Mapping[str, float] | None = None,
) -> CombatBuild:
    roster = roster or {detail.summary.id: detail}
    panel = calculate_character_panel(detail)
    weights = index_weights(detail) if weights is None else weights
    teams = teams_from_goal(detail.cultivation_goal)
    primary = detail.cultivation_goal.get("combat_primary_team")
    results = []
    for team in teams:
        missing = list(panel.missing)
        if not team.confirmed:
            missing.append("請確認隊伍的正常輪轉條件與增益")
        if primary not in {t.identifier for t in teams}:
            missing.append("請指定主要隊伍")
        if detail.summary.id in team.member_ids:
            missing.append("隊友不可重複選擇目前角色")
        for identifier in team.member_ids:
            actor = roster.get(identifier)
            if actor is None or actor.summary.archived_at is not None:
                missing.append("隊友已封存或不存在")
            elif actor.summary.data_set_id != detail.summary.data_set_id:
                missing.append("隊友不屬於目前資料集")
            else:
                provider_panel = calculate_character_panel(actor)
                if provider_panel.missing:
                    missing.extend(
                        f"{actor.summary.name}: {item}" for item in provider_panel.missing
                    )
        totals = _panel_totals(panel, damage_type=team.damage_type)
        effects = list(available_effects(detail, team, roster))
        adds: dict[str, float] = defaultdict(float)
        # Same-name party effects do not stack. Keep highest applicable value.
        unique: dict[tuple[str, str], float] = {}
        for effect in effects:
            if not effect.active:
                continue
            rule_id = effect.identifier.split(":", 1)[-1]
            for key, value in effect.values:
                unique[rule_id, key] = max(unique.get((rule_id, key), 0), value)
        for (_, key), value in unique.items():
            adds[key] += value
        for key, value in adds.items():
            if key in {"attack_pct", "hp_pct", "defense_pct"}:
                stat = key.removesuffix("_pct")
                totals[stat] += float(dict(panel.bases).get(stat, 0)) * value / 100
            elif key not in {"shorekeeper", "mornye", "suisui"}:
                totals[key] = totals.get(key, 0) + value
        dynamic_max: dict[tuple[str, str], float] = {}
        rules_by_id = {r["id"]: r for r in load_combat_catalog()["effects"]}
        actors = {**roster, detail.summary.id: detail}
        for effect_index, effect in enumerate(effects):
            provider_id, rule_id = effect.identifier.split(":", 1)
            rule = rules_by_id[rule_id]
            variable = rule.get("variable")
            if variable is None:
                continue
            provider = detail if variable.get("receiver") else actors[provider_id]
            provider_values, provider_missing = _provider_totals(provider, detail, team, actors)
            if provider_missing and effect.active:
                missing.append(f"{provider.summary.name}的合鳴轉換數值待補")
            value = min(
                variable["cap"],
                max(0, provider_values.get(variable["stat"], 0) - variable.get("offset", 0))
                * variable["factor"],
            )
            output = next(iter(rule["values"]))
            effects[effect_index] = replace(effect, values=((output, value),))
            if effect.active:
                dynamic_key = rule_id, output
                increment = max(0, value - dynamic_max.get(dynamic_key, 0))
                dynamic_max[dynamic_key] = max(dynamic_max.get(dynamic_key, 0), value)
                if output in {"attack_pct", "hp_pct", "defense_pct"}:
                    target_stat = output.removesuffix("_pct")
                    totals[target_stat] += (
                        float(dict(panel.bases).get(target_stat, 0)) * increment / 100
                    )
                else:
                    totals[output] = totals.get(output, 0) + increment
        # Typed and elemental deepen only affect the selected output window.
        totals["deepen"] = totals.get("deepen", 0) + adds.get(team.damage_type + "_deepen", 0)
        element = ELEMENT_KEYS.get(detail.summary.element or "", "")
        totals["deepen"] += adds.get(element + "_deepen", 0)
        target = team.energy_target
        if target is None:
            guide = guide_for_character(detail.summary.name)
            target = detail.cultivation_goal.get("scoring_energy_target") or (
                None if guide is None else guide.energy_target
            )
        # ER-to-benefit conversions depend on the provider's real equipment.
        for actor in [detail, *(roster[i] for i in team.member_ids if i in roster)]:
            conversion = load_combat_catalog().get("conversions", {}).get(actor.summary.name)
            if conversion is None:
                continue
            self_actor = actor.summary.id == detail.summary.id
            if self_actor:
                energy = totals["energy_regen"]
                target = max(target or 100, conversion["target"])
            else:
                provider_values, provider_missing = _provider_totals(actor, detail, team, roster)
                if provider_missing:
                    missing.append(f"{actor.summary.name}的充能轉換數值待補")
                    continue
                # Re-evaluate provider-specific ER buffs, without recursively
                # evaluating the provider's unrelated teams.
                energy = provider_values["energy_regen"]
            for rule in conversion["rules"]:
                if actor.summary.resonance_chain < rule.get("chain", 0):
                    continue
                if rule.get("scope", "all") not in {"all", team.damage_type}:
                    continue
                if rule.get("recipient", "team") == "self" and not self_actor:
                    continue
                if rule.get("mode") and _mode(actor, team) != rule["mode"]:
                    continue
                value = min(rule["cap"], max(0, energy - rule["offset"]) * rule["factor"])
                key = rule["stat"]
                identifier = f"{actor.summary.id}:conversion-{rule['stat']}"
                effect = CombatEffect(
                    identifier,
                    f"{actor.summary.name}・充能轉換",
                    ((key, value),),
                    conversion["source"]["url"],
                    rule["condition"],
                    identifier not in team.excluded_effects,
                )
                effects.append(effect)
                if effect.active:
                    if key == "attack_pct":
                        totals["attack"] += float(dict(panel.bases).get("attack", 0)) * value / 100
                    else:
                        totals[key] = totals.get(key, 0) + value
        totals["selected_damage"] += totals.get(team.damage_type + "_damage", 0) - dict(
            _panel_totals(panel, damage_type=team.damage_type)
        ).get(team.damage_type + "_damage", 0)
        if target is None:
            missing.append("隊伍充能目標待確認")
        results.append(
            TeamBuild(
                team,
                tuple(totals.items()),
                target,
                tuple(effects),
                tuple(dict.fromkeys(missing)),
                None if missing else build_index(detail, totals, weights),
            )
        )
    return CombatBuild(
        tuple(results), str(primary) if primary is not None else None, panel, tuple(weights.items())
    )


def with_candidate(
    detail: CharacterDetail, slot_index: int, candidate: EchoSlotSnapshot
) -> CharacterDetail:
    if slot_index not in range(1, 6):
        raise ValueError("invalid equipment position")
    return replace(
        detail,
        echo_slots=tuple(
            replace(candidate, slot_index=slot_index) if row.slot_index == slot_index else row
            for row in detail.echo_slots
        ),
    )


@dataclass(frozen=True, slots=True)
class BuildComparison:
    before: CombatBuild
    after: CombatBuild
    primary_delta: float | None
    team_deltas: tuple[tuple[str, float | None], ...]

    @property
    def improves(self) -> bool:
        if self.after.sufficient is not True or self.primary_delta is None:
            return False
        if self.before.sufficient is False:
            return True  # Restoring a broken rotation has first priority.
        if self.primary_delta > 1e-9:
            return True
        if abs(self.primary_delta) <= 1e-9:
            deltas = [delta for _, delta in self.team_deltas if delta is not None]
            return bool(deltas) and min(deltas) >= -1e-9 and max(deltas) > 1e-9
        return False

    @property
    def details(self) -> str:
        lines = [self.after.details(), "\n替換前後的配裝指標差(非傷害%):"]
        lines += [
            f"{name}: {'待補資料' if delta is None else f'{delta:+.4f}'}"
            for name, delta in self.team_deltas
        ]
        return "\n".join(lines)


def compare_builds(before: CombatBuild, after: CombatBuild) -> BuildComparison:
    prior = {row.team.identifier: row for row in before.teams}
    deltas = []
    primary_delta = None
    for row in after.teams:
        old = prior.get(row.team.identifier)
        delta = (
            None if old is None or old.index is None or row.index is None else row.index - old.index
        )
        deltas.append((row.team.name, delta))
        if row.team.identifier == after.primary_id:
            primary_delta = delta
    return BuildComparison(before, after, primary_delta, tuple(deltas))


@dataclass(frozen=True, slots=True)
class CompensationPlan:
    replacements: tuple[tuple[int, str, str], ...]
    comparison: BuildComparison


def find_compensation(
    original: CharacterDetail,
    anchored: CharacterDetail,
    inventory: Sequence[EchoSlotSnapshot],
    roster: Mapping[str, CharacterDetail],
    anchor_slot: int,
    *,
    weights: Mapping[str, float] | None = None,
) -> tuple[CompensationPlan, ...]:
    """Enumerate available other-slot repairs, prefer the least primary loss.

    Explore all four other positions with a bounded beam. Only actual,
    fully confirmed free inventory is supplied by the service. All results retain
    every team constraint and full recalculation; no imagined Echo is accepted.
    """
    before = evaluate_build(original, roster, weights=weights)
    plans = []
    used = {s.echo_id for s in anchored.echo_slots}
    options: list[tuple[int, EchoSlotSnapshot]] = []
    for slot in anchored.echo_slots:
        if slot.slot_index == anchor_slot:
            continue
        for candidate in inventory:
            if (
                candidate.echo_id in used
                or candidate.cost != slot.cost
                or candidate.level != 25
                or candidate.set_name != slot.set_name
            ):
                continue
            # ER restoration must not silently change the slot's intended main.
            if not slot.main_stats or not candidate.main_stats:
                continue
            if (slot.main_stats[0].get("name"), slot.main_stats[0].get("unit")) != (
                candidate.main_stats[0].get("name"),
                candidate.main_stats[0].get("unit"),
            ):
                continue
            options.append((slot.slot_index, candidate))

    def consider(build: CharacterDetail, replacements: tuple[tuple[int, str, str], ...]) -> None:
        after = evaluate_build(build, roster, weights=weights)
        comparison = compare_builds(before, after)
        if comparison.improves:
            plans.append(CompensationPlan(replacements, comparison))

    states: list[tuple[CharacterDetail, tuple[tuple[int, str, str], ...]]] = [(anchored, ())]
    for position in sorted({p for p, _ in options}):
        expanded = list(states)
        for state, replacements in states:
            for candidate_position, candidate in options:
                if candidate_position != position:
                    continue
                if candidate.echo_id in {s.echo_id for s in state.echo_slots}:
                    continue
                build = with_candidate(state, position, candidate)
                changes = (
                    *replacements,
                    (position, candidate.echo_id or "", candidate.name or "聲骸"),
                )
                consider(build, changes)
                expanded.append((build, changes))
        ranked = []
        for state, replacements in expanded:
            assessed = evaluate_build(state, roster, weights=weights)
            if assessed.sufficient is None or assessed.primary is None:
                continue
            gap = sum(row.energy_gap or 0 for row in assessed.teams)
            value = assessed.primary.index or 0
            ranked.append((gap, -value, len(replacements), state, replacements))
        best_gap = sorted(ranked, key=lambda row: row[:3])[:32]
        best_value = sorted(ranked, key=lambda row: (row[1], row[0], row[2]))[:32]
        seen = set()
        states = []
        for _, _, _, state, replacements in (*best_gap, *best_value):
            identity = tuple(s.echo_id for s in state.echo_slots)
            if identity not in seen:
                states.append((state, replacements))
                seen.add(identity)
    plans = list({plan.replacements: plan for plan in plans}.values())
    plans.sort(
        key=lambda p: (-(p.comparison.primary_delta or 0), len(p.replacements), p.replacements)
    )
    return tuple(plans[:3])
