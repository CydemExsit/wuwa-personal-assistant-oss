"""Exact remaining substat outlook after the +15 exploration checkpoint.

Uses the disclosed type/value probabilities. Main stats are held at observed
values: this is a substat outlook, not an invented full-level main-stat forecast.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations, product
from math import comb, prod

import numpy as np

from wuwa_assistant.scoring import load_roll_data
from wuwa_assistant.scoring.engine import canonical_stat_key

from .character_stats import _echo_substat_percent
from .characters import CharacterDetail
from .combat_build import ELEMENT_KEYS, CombatBuild, load_combat_catalog


@dataclass(frozen=True, slots=True)
class CombatProjection:
    probability_sufficient: float
    probability_improve: float
    expected_positive_delta: float
    maximum_delta: float | None
    outcome_count: int
    scope: str = "剩餘副詞條; 主詞條維持目前值"


def project_combat_substats(
    original: CombatBuild,
    candidate: CombatBuild,
    anchored: CharacterDetail,
    slot_index: int,
) -> CombatProjection | None:
    piece = anchored.echo_slots[slot_index - 1]
    if piece.level is None or piece.level < 15 or piece.level == 25:
        return None
    if original.primary is None or original.primary.index is None or candidate.sufficient is None:
        return None
    remaining = 5 - len(piece.sub_stats)
    if remaining not in {1, 2}:
        return None
    used = {canonical_stat_key(s.get("name"), s.get("unit")) for s in piece.sub_stats}
    rolls = load_roll_data()
    keys = [key for key in rolls.roll_values if key not in used]
    arrays = []
    probabilities = []
    # Direct columns are shared by every team. Only ER conversions differ.
    columns = (
        "attack",
        "hp",
        "defense",
        "crit_rate",
        "crit_damage",
        "energy_regen",
        "basic_attack_damage",
        "heavy_attack_damage",
        "resonance_skill_damage",
        "resonance_liberation_damage",
    )
    bases = dict(candidate.panel.bases)
    from decimal import Decimal

    for selected in combinations(keys, remaining):
        value_rows = [
            tuple(zip(rolls.roll_values[k], rolls.probabilities_for(k), strict=True))
            for k in selected
        ]
        for outcomes in product(*value_rows):
            row = dict.fromkeys(columns, 0.0)
            for key, (value, _) in zip(selected, outcomes, strict=True):
                stat = str(key)
                if stat.endswith("_pct"):
                    target = stat.removesuffix("_pct")
                    row[target] += (
                        float(bases[target])
                        * float(_echo_substat_percent(stat, Decimal(str(value))))
                        / 100
                    )
                elif stat.startswith("flat_"):
                    row[stat.removeprefix("flat_")] += value
                elif stat in row:
                    row[stat] += value
            arrays.append(tuple(row[k] for k in columns))
            probabilities.append(prod(prob for _, prob in outcomes) / comb(len(keys), remaining))
    delta = np.asarray(arrays, dtype=np.float64)
    probability = np.asarray(probabilities, dtype=np.float64)
    sufficient = np.ones(len(arrays), dtype=np.bool_)
    primary_delta = None
    weights = dict(candidate.weights)
    drivers = {k: weights.get(k + "_pct", 0.0) for k in ("attack", "hp", "defense")}
    top = max(drivers.values(), default=0) or 1.0
    crit_weight = min(1.0, (weights.get("crit_rate", 1) + weights.get("crit_damage", 1)) / 2)
    conversion = load_combat_catalog()["conversions"].get(anchored.summary.name)
    for team in candidate.teams:
        current = dict(team.totals)
        totals = {key: delta[:, i] + current.get(key, 0) for i, key in enumerate(columns)}
        deepen = np.full(len(arrays), current.get("deepen", 0))
        rules_by_id = {r["id"]: r for r in load_combat_catalog()["effects"]}
        for effect in team.effects:
            actor_id, rule_id = effect.identifier.split(":", 1)
            rule = rules_by_id.get(rule_id, {})
            variable = rule.get("variable")
            if (
                not effect.active
                or variable is None
                or variable["stat"] != "energy_regen"
                or actor_id != anchored.summary.id
            ):
                continue
            output, prior_value = effect.values[0]
            new_value = np.minimum(
                variable["cap"],
                np.maximum(0, totals["energy_regen"] - variable.get("offset", 0))
                * variable["factor"],
            )
            increment = new_value - prior_value
            if output == "attack_pct":
                totals["attack"] += float(bases["attack"]) * increment / 100
        if conversion:
            for rule in conversion["rules"]:
                if anchored.summary.resonance_chain < rule.get("chain", 0):
                    continue
                if rule.get("scope", "all") not in {"all", team.team.damage_type}:
                    continue
                key = rule["stat"]
                excluded = f"{anchored.summary.id}:conversion-{key}" in team.team.excluded_effects
                if excluded:
                    continue
                old = min(
                    rule["cap"], max(0, current["energy_regen"] - rule["offset"]) * rule["factor"]
                )
                new = np.minimum(
                    rule["cap"],
                    np.maximum(0, totals["energy_regen"] - rule["offset"]) * rule["factor"],
                )
                increment = new - old
                if key == "attack_pct":
                    totals["attack"] += float(bases["attack"]) * increment / 100
                elif key == "deepen":
                    deepen += increment
                elif key in totals:
                    totals[key] += increment
        sufficient &= totals["energy_regen"] >= (team.energy_target or 100) - 1e-7
        index = sum(w / top * np.log(np.maximum(1, totals[k])) for k, w in drivers.items())
        rate = (
            np.ones(len(arrays))
            if current.get("guaranteed_crit", 0)
            else np.clip(totals["crit_rate"] / 100, 0, 1)
        )
        credit = rate - rate**9 / 9
        crit = (1 + credit * np.maximum(0, totals["crit_damage"] / 100 - 1)) * (
            1 - 0.15 * (1 - rate) ** 2
        )
        index += crit_weight * np.log(crit)
        element = current.get(ELEMENT_KEYS.get(anchored.summary.element or "", ""), 0)
        extra = delta[:, columns.index(team.team.damage_type + "_damage")]
        bonus = (
            element
            + current.get("all_element_damage", 0)
            + current.get("selected_damage", 0)
            + extra
        )
        index += np.log(np.maximum(0.01, 1 + bonus / 100))
        index += np.log(np.maximum(0.01, 1 + deepen / 100))
        if team.team.identifier == candidate.primary_id:
            primary_delta = index - original.primary.index
    if primary_delta is None:
        return None
    improve = sufficient & (primary_delta > 1e-9)
    total = float(probability.sum())
    return CombatProjection(
        float(probability[sufficient].sum()) / total,
        float(probability[improve].sum()) / total,
        float(np.dot(np.where(improve, primary_delta, 0), probability)) / total,
        float(primary_delta[sufficient].max()) if sufficient.any() else None,
        len(arrays),
    )
