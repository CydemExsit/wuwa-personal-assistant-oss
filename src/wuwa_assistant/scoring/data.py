"""Strict loaders for bundled, versioned scoring data."""

from __future__ import annotations

import json
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass, field
from importlib.resources import files
from itertools import pairwise
from math import isfinite
from typing import Any, cast

from .models import (
    CharacterScoringProfile,
    ConditionalWeight,
    CritBalanceRule,
    Provenance,
    ProvenanceStatus,
    RollTier,
    ScoreThresholds,
    StatKey,
    StatWeight,
    ThresholdRule,
)

_RESOURCE_PACKAGE = "wuwa_assistant.data.scoring"
LEGACY_ROLL_VERSION = "echo-rolls-2026.08-v1"
CURRENT_ROLL_VERSION = "echo-rolls-2026.09-official-v2"


@dataclass(frozen=True, slots=True)
class ProbabilityAssumption:
    status: ProvenanceStatus
    name: str
    is_official: bool
    notes: str


@dataclass(frozen=True, slots=True)
class RollDataCatalog:
    schema_version: int
    data_version: str
    provenance: Provenance
    probability_assumption: ProbabilityAssumption
    minimum_roll_credit: float
    tiers: tuple[RollTier, ...]
    roll_values: dict[StatKey, tuple[float, ...]]
    roll_probabilities: dict[StatKey, tuple[float, ...]] = field(default_factory=dict)
    reference_quantile: float | None = None

    def values_for(self, stat: StatKey) -> tuple[float, ...] | None:
        return self.roll_values.get(stat)

    def probabilities_for(self, stat: StatKey) -> tuple[float, ...]:
        values = self.roll_values[stat]
        return self.roll_probabilities.get(stat, tuple(1 / len(values) for _ in values))


@dataclass(frozen=True, slots=True)
class DecisionPolicyAdjustments:
    embryo_supply_abundant: float
    embryo_supply_scarce: float
    breadth_first_gap: float
    character_already_qualified: float
    many_candidates_at_least: int
    many_candidates: float
    few_candidates_at_most: int
    few_candidates: float
    current_score_at_least_usable: float


@dataclass(frozen=True, slots=True)
class DecisionPolicy:
    schema_version: int
    data_version: str
    provenance: Provenance
    decision_levels: frozenset[int]
    base_continue_chance: dict[int, float]
    adjustments: DecisionPolicyAdjustments
    minimum_continue_chance: float
    maximum_continue_chance: float
    cost_reference_experience: int = 63500
    cost_chance_penalty: float = 0.12
    exploration_level: int = 15
    exploration_base_chance: float = 0.02
    exploration_cost_chance_penalty: float = 0.03
    exploration_supply_factor: float = 0.25
    expected_gain_per_cost_reference: float = 0.75
    minimum_expected_gain: float = 0.10
    scarce_gain_factor: float = 0.70
    abundant_gain_factor: float = 1.30
    weak_baseline_quality_level: int = 15
    high_quality_gap: float = 10.0
    high_quality_factor: float = 0.5


@dataclass(frozen=True, slots=True)
class ScoringCatalog:
    data_version: str
    provenance: Provenance
    profiles: tuple[CharacterScoringProfile, ...]
    rolls: RollDataCatalog

    def profile(self, profile_id: str) -> CharacterScoringProfile:
        match = next((item for item in self.profiles if item.profile_id == profile_id), None)
        if match is None:
            raise KeyError(f"unknown scoring profile: {profile_id}")
        return match


def _load_json_resource(name: str) -> dict[str, Any]:
    resource = files(_RESOURCE_PACKAGE).joinpath(name)
    try:
        value = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"unable to load bundled scoring resource {name}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"scoring resource {name} must contain a JSON object")
    return cast(dict[str, Any], value)


def _provenance(raw: object) -> Provenance:
    if not isinstance(raw, dict):
        raise ValueError("provenance must be an object")
    return Provenance(
        status=ProvenanceStatus(str(raw["status"])),
        source=str(raw["source"]),
        is_official=bool(raw["is_official"]),
        notes=str(raw["notes"]),
    )


def load_roll_data(version: str = CURRENT_ROLL_VERSION) -> RollDataCatalog:
    if version not in {LEGACY_ROLL_VERSION, CURRENT_ROLL_VERSION}:
        raise ValueError(f"unsupported roll data version: {version}")
    raw = _load_json_resource(
        "rolls.v1.json" if version == LEGACY_ROLL_VERSION else "rolls.v2.json"
    )
    probability = raw.get("probability_model")
    if not isinstance(probability, dict):
        raise ValueError("probability_model must be an object")
    raw_tiers = raw.get("tiers")
    raw_values = raw.get("roll_values")
    if not isinstance(raw_tiers, list) or not isinstance(raw_values, dict):
        raise ValueError("roll data needs tiers and roll_values")

    tiers = tuple(
        RollTier(
            key=str(item["key"]),
            label_zh_tw=str(item["label_zh_tw"]),
            minimum_ratio=float(item["minimum_ratio"]),
            maximum_ratio=float(item["maximum_ratio"]),
        )
        for item in raw_tiers
        if isinstance(item, dict)
    )
    if len(tiers) != len(raw_tiers) or not tiers:
        raise ValueError("every roll tier must be an object")
    _validate_tier_coverage(tiers)

    values: dict[StatKey, tuple[float, ...]] = {}
    for raw_key, raw_rolls in raw_values.items():
        if not isinstance(raw_rolls, list) or not raw_rolls:
            raise ValueError(f"roll values for {raw_key} must be a non-empty list")
        stat = StatKey(str(raw_key))
        parsed = tuple(float(value) for value in raw_rolls)
        if parsed != tuple(sorted(set(parsed))):
            raise ValueError(f"roll values for {raw_key} must be unique and ordered")
        values[stat] = parsed
    if set(values) != set(StatKey):
        missing = sorted(str(stat) for stat in set(StatKey) - set(values))
        raise ValueError(f"roll table is incomplete: {missing}")

    minimum_credit = float(raw["minimum_roll_credit"])
    if not 0 <= minimum_credit <= 1:
        raise ValueError("minimum_roll_credit must be between zero and one")
    probabilities: dict[StatKey, tuple[float, ...]] = {}
    raw_probabilities = raw.get("roll_probability_percent", {})
    if probability["is_official"] and set(raw_probabilities) != {str(s) for s in StatKey}:
        raise ValueError("official probability table must cover every stat")
    for key, row in raw_probabilities.items():
        stat = StatKey(key)
        numbers = tuple(float(p) for p in row)
        if (
            len(numbers) != len(values[stat])
            or any(not isfinite(p) or p <= 0 for p in numbers)
            or abs(sum(numbers) - 100) > 0.001
        ):
            raise ValueError(f"invalid roll probabilities: {stat}")
        # The disclosure rounds percentages to four decimal places.
        probabilities[stat] = tuple(p / sum(numbers) for p in numbers)
    quantile = raw.get("reference_quantile")
    if quantile is not None and not 0 < float(quantile) < 1:
        raise ValueError("reference_quantile must be within (0, 1)")
    return RollDataCatalog(
        schema_version=int(raw["schema_version"]),
        data_version=str(raw["data_version"]),
        provenance=_provenance(raw["provenance"]),
        probability_assumption=ProbabilityAssumption(
            status=ProvenanceStatus(str(probability["status"])),
            name=str(probability["name"]),
            is_official=bool(probability["is_official"]),
            notes=str(probability["notes"]),
        ),
        minimum_roll_credit=minimum_credit,
        tiers=tiers,
        roll_values=values,
        roll_probabilities=probabilities,
        reference_quantile=None if quantile is None else float(quantile),
    )


def load_decision_policy() -> DecisionPolicy:
    raw = _load_json_resource("decision-policy.v6.json")
    provenance = _provenance(raw.get("provenance"))
    if provenance.status is not ProvenanceStatus.PROJECT_DEFAULT or provenance.is_official:
        raise ValueError("decision policy must be a non-official project default")

    raw_levels = raw.get("decision_levels")
    raw_chances = raw.get("base_continue_chance")
    raw_adjustments = raw.get("adjustments")
    raw_clamp = raw.get("continue_chance_clamp")
    if not isinstance(raw_levels, list) or not isinstance(raw_chances, dict):
        raise ValueError("decision policy needs decision_levels and base_continue_chance")
    if not isinstance(raw_adjustments, dict) or not isinstance(raw_clamp, dict):
        raise ValueError("decision policy needs adjustments and continue_chance_clamp")

    decision_levels = frozenset(int(level) for level in raw_levels)
    if decision_levels != {5, 10, 15, 20, 25} or len(raw_levels) != len(decision_levels):
        raise ValueError("decision levels must be exactly +5/+10/+15/+20/+25")
    base_continue_chance = {int(level): float(chance) for level, chance in raw_chances.items()}
    if set(base_continue_chance) != decision_levels - {25}:
        raise ValueError("base continue chances must cover +5/+10/+15/+20")
    _validate_probability_values(base_continue_chance.values(), "base continue chance")

    adjustments = DecisionPolicyAdjustments(
        embryo_supply_abundant=float(raw_adjustments["embryo_supply_abundant"]),
        embryo_supply_scarce=float(raw_adjustments["embryo_supply_scarce"]),
        breadth_first_gap=float(raw_adjustments["breadth_first_gap"]),
        character_already_qualified=float(raw_adjustments["character_already_qualified"]),
        many_candidates_at_least=int(raw_adjustments["many_candidates_at_least"]),
        many_candidates=float(raw_adjustments["many_candidates"]),
        few_candidates_at_most=int(raw_adjustments["few_candidates_at_most"]),
        few_candidates=float(raw_adjustments["few_candidates"]),
        current_score_at_least_usable=float(raw_adjustments["current_score_at_least_usable"]),
    )
    _validate_decision_adjustments(adjustments)

    minimum = float(raw_clamp["minimum"])
    maximum = float(raw_clamp["maximum"])
    if not 0 <= minimum <= maximum <= 1:
        raise ValueError("continue chance clamp must be ordered within zero and one")
    schema_version = int(raw["schema_version"])
    data_version = str(raw["data_version"])
    if schema_version <= 0 or not data_version:
        raise ValueError("decision policy versions must be present and positive")
    cost_reference = int(raw["cost_reference_experience"])
    cost_penalty = float(raw["cost_chance_penalty"])
    if cost_reference <= 0 or not 0 <= cost_penalty <= 1:
        raise ValueError("cost-aware policy needs a positive reference and probability penalty")
    exploration = raw["early_exploration"]
    gain = raw["target_gain"]
    if exploration["checkpoint_level"] != 15:
        raise ValueError("exploration checkpoint must unlock three slots at +15")
    _validate_probability_values(
        (
            exploration["base_chance"],
            exploration["cost_chance_penalty"],
            exploration["supply_factor"],
        ),
        "early exploration",
    )
    if any(not isfinite(float(value)) or value <= 0 for value in gain.values()):
        raise ValueError("target gain policy values must be finite and positive")
    if not gain["scarce_factor"] <= 1 <= gain["abundant_factor"]:
        raise ValueError("scarce supply must not be stricter than abundant supply")
    quality = raw.get("weak_baseline_quality")
    if (
        not isinstance(quality, dict)
        or quality.get("minimum_level") != exploration["checkpoint_level"]
        or quality.get("threshold") != "usable"
    ):
        raise ValueError("weak-baseline quality must use the usable threshold after exploration")
    high_quality = raw.get("high_quality_close_target")
    if (
        not isinstance(high_quality, dict)
        or high_quality.get("threshold") != "passing"
        or not isinstance(high_quality.get("maximum_gap"), (int, float))
        or not 0 <= high_quality["maximum_gap"] <= 100
        or not isinstance(high_quality.get("requirements_factor"), (int, float))
        or not 0 < high_quality["requirements_factor"] <= 1
    ):
        raise ValueError("high-quality relaxation needs passing threshold, finite gap and factor")
    return DecisionPolicy(
        schema_version=schema_version,
        data_version=data_version,
        provenance=provenance,
        decision_levels=decision_levels,
        base_continue_chance=base_continue_chance,
        adjustments=adjustments,
        minimum_continue_chance=minimum,
        maximum_continue_chance=maximum,
        cost_reference_experience=cost_reference,
        cost_chance_penalty=cost_penalty,
        exploration_level=int(exploration["checkpoint_level"]),
        exploration_base_chance=float(exploration["base_chance"]),
        exploration_cost_chance_penalty=float(exploration["cost_chance_penalty"]),
        exploration_supply_factor=float(exploration["supply_factor"]),
        expected_gain_per_cost_reference=float(gain["per_cost_reference"]),
        minimum_expected_gain=float(gain["minimum"]),
        scarce_gain_factor=float(gain["scarce_factor"]),
        abundant_gain_factor=float(gain["abundant_factor"]),
        weak_baseline_quality_level=int(quality["minimum_level"]),
        high_quality_gap=float(high_quality["maximum_gap"]),
        high_quality_factor=float(high_quality["requirements_factor"]),
    )


def _validate_probability_values(values: Iterable[float], label: str) -> None:
    if not all(0 <= value <= 1 for value in values):
        raise ValueError(f"{label} values must be between zero and one")


def _validate_decision_adjustments(adjustments: DecisionPolicyAdjustments) -> None:
    values = (
        adjustments.embryo_supply_abundant,
        adjustments.embryo_supply_scarce,
        adjustments.breadth_first_gap,
        adjustments.character_already_qualified,
        adjustments.many_candidates,
        adjustments.few_candidates,
        adjustments.current_score_at_least_usable,
    )
    if any(not -1 <= value <= 1 for value in values):
        raise ValueError("decision policy adjustments must be between minus one and one")
    if adjustments.few_candidates_at_most < 0:
        raise ValueError("few-candidate threshold must not be negative")
    if adjustments.many_candidates_at_least <= adjustments.few_candidates_at_most:
        raise ValueError("many-candidate threshold must exceed the few-candidate threshold")


def _validate_tier_coverage(tiers: tuple[RollTier, ...]) -> None:
    ordered = sorted(tiers, key=lambda tier: tier.minimum_ratio)
    if ordered[0].minimum_ratio != 0 or ordered[-1].maximum_ratio != 1:
        raise ValueError("roll tiers must cover zero through one")
    for previous, current in pairwise(ordered):
        if current.minimum_ratio - previous.maximum_ratio > 0.00001:
            raise ValueError("roll tiers must not contain gaps")


def load_template_profiles() -> tuple[CharacterScoringProfile, ...]:
    raw = _load_json_resource("templates.v1.json")
    raw_templates = raw.get("templates")
    raw_defaults = raw.get("default_thresholds")
    if not isinstance(raw_templates, list) or not isinstance(raw_defaults, dict):
        raise ValueError("template data needs templates and default_thresholds")
    provenance = _provenance(raw["provenance"])
    profiles = tuple(
        _parse_profile(item, raw_defaults, provenance)
        for item in raw_templates
        if isinstance(item, dict)
    )
    if len(profiles) != len(raw_templates) or not profiles:
        raise ValueError("every scoring template must be an object")
    if len({profile.profile_id for profile in profiles}) != len(profiles):
        raise ValueError("scoring template ids must be unique")
    return profiles


def _parse_profile(
    raw: dict[str, Any],
    defaults: dict[str, Any],
    provenance: Provenance,
) -> CharacterScoringProfile:
    raw_thresholds = raw.get("thresholds", defaults)
    raw_weights = raw.get("weights")
    raw_conditional = raw.get("conditional_weights", [])
    raw_rules = raw.get("threshold_rules", [])
    if not isinstance(raw_thresholds, dict) or not isinstance(raw_weights, dict):
        raise ValueError("profile thresholds and weights must be objects")
    if not isinstance(raw_conditional, list) or not isinstance(raw_rules, list):
        raise ValueError("profile conditional weights and threshold rules must be lists")

    crit_raw = raw.get("crit_balance_rule")
    crit_rule = None
    if crit_raw is not None:
        if not isinstance(crit_raw, dict):
            raise ValueError("crit balance rule must be an object or null")
        crit_rule = CritBalanceRule(
            target_excess_damage_per_rate=float(crit_raw["target_excess_damage_per_rate"]),
            tolerance=float(crit_raw["tolerance"]),
            max_adjustment=float(crit_raw["max_adjustment"]),
            required_context=bool(crit_raw.get("required_context", False)),
        )

    return CharacterScoringProfile(
        profile_id=str(raw["profile_id"]),
        name=str(raw["name"]),
        template_kind=str(raw["template_kind"]),
        version=int(raw["version"]),
        description=str(raw["description"]),
        thresholds=ScoreThresholds(
            usable=float(raw_thresholds["usable"]),
            passing=float(raw_thresholds["passing"]),
            perfect=float(raw_thresholds["perfect"]),
            exceptional=float(raw_thresholds["exceptional"]),
        ),
        weights=tuple(
            StatWeight(stat=StatKey(str(key)), weight=float(value))
            for key, value in raw_weights.items()
        ),
        conditional_weights=tuple(
            ConditionalWeight(
                stat=StatKey(str(item["stat"])),
                condition_key=str(item["condition_key"]),
                expected_value=cast(str | int | float | bool, item["expected_value"]),
                multiplier=float(item["multiplier"]),
            )
            for item in raw_conditional
            if isinstance(item, dict)
        ),
        threshold_rules=tuple(
            ThresholdRule(
                rule_id=str(item["rule_id"]),
                stat=StatKey(str(item["stat"])),
                minimum=float(item["minimum"]) if item.get("minimum") is not None else None,
                maximum=float(item["maximum"]) if item.get("maximum") is not None else None,
                below_multiplier=float(item.get("below_multiplier", 1.0)),
                within_multiplier=float(item.get("within_multiplier", 1.0)),
                above_multiplier=float(item.get("above_multiplier", 1.0)),
                required_context=bool(item.get("required_context", False)),
            )
            for item in raw_rules
            if isinstance(item, dict)
        ),
        crit_balance_rule=crit_rule,
        roll_data_version=str(raw["roll_data_version"]),
        provenance=provenance,
        is_default=bool(raw.get("is_default", False)),
    )


def load_scoring_catalog() -> ScoringCatalog:
    raw_templates = _load_json_resource("templates.v1.json")
    rolls = load_roll_data()
    profiles = load_template_profiles()
    mismatches = [
        profile.profile_id
        for profile in profiles
        if profile.roll_data_version != rolls.data_version
    ]
    if mismatches:
        raise ValueError(f"profiles reference an unavailable roll data version: {mismatches}")
    return ScoringCatalog(
        data_version=str(raw_templates["data_version"]),
        provenance=_provenance(raw_templates["provenance"]),
        profiles=profiles,
        rolls=rolls,
    )


def load_template_seed_definitions() -> list[dict[str, Any]]:
    """Return a fresh, JSON-safe stable contract for idempotent DB seeding.

    Conditional weights are both retained at profile level and materialized as
    condition-keyed weight rows so the current SQL schema can store them losslessly.
    Callers may mutate the returned value without affecting later calls.
    """

    return profiles_to_seed_definitions(load_template_profiles())


def profiles_to_seed_definitions(
    profiles: Iterable[CharacterScoringProfile],
) -> list[dict[str, Any]]:
    """Convert immutable profiles to the existing versioned storage contract."""
    definitions: list[dict[str, Any]] = []
    for profile in profiles:
        base_by_stat = {weight.stat: weight for weight in profile.weights}
        rows: list[dict[str, Any]] = []
        for index, weight in enumerate(profile.weights):
            rows.append(
                {
                    "stat_name": str(weight.stat),
                    "unit": unit_for_stat(weight.stat),
                    "weight": weight.weight,
                    "cap_value": weight.cap_value,
                    "condition_key": "always",
                    "condition": None,
                    "display_order": index,
                }
            )
        conditional_payload: list[dict[str, Any]] = []
        for index, conditional in enumerate(profile.conditional_weights, start=len(rows)):
            base = base_by_stat[conditional.stat]
            payload = {
                "stat": str(conditional.stat),
                "condition_key": conditional.condition_key,
                "expected_value": conditional.expected_value,
                "multiplier": conditional.multiplier,
            }
            conditional_payload.append(payload)
            rows.append(
                {
                    "stat_name": str(conditional.stat),
                    "unit": unit_for_stat(conditional.stat),
                    "weight": base.weight * conditional.multiplier,
                    "cap_value": base.cap_value,
                    "condition_key": conditional.condition_key,
                    "condition": {
                        "operator": "equals",
                        "value": conditional.expected_value,
                        "base_weight": base.weight,
                        "multiplier": conditional.multiplier,
                    },
                    "display_order": index,
                }
            )
        definitions.append(
            {
                "profile_id": profile.profile_id,
                "name": profile.name,
                "template_kind": profile.template_kind,
                "version": profile.version,
                "description": profile.description,
                "thresholds": {
                    "usable": profile.thresholds.usable,
                    "passing": profile.thresholds.passing,
                    "perfect": profile.thresholds.perfect,
                    "exceptional": profile.thresholds.exceptional,
                },
                "threshold_rules": [
                    _threshold_rule_payload(rule) for rule in profile.threshold_rules
                ],
                "crit_balance_rule": _crit_rule_payload(profile.crit_balance_rule),
                "roll_data_version": profile.roll_data_version,
                "is_default": profile.is_default,
                "weights": rows,
                "conditional_weights": conditional_payload,
                "provenance": _provenance_payload(profile.provenance),
            }
        )
    return deepcopy(definitions)


def _threshold_rule_payload(rule: ThresholdRule) -> dict[str, Any]:
    return {
        "rule_id": rule.rule_id,
        "stat": str(rule.stat),
        "minimum": rule.minimum,
        "maximum": rule.maximum,
        "below_multiplier": rule.below_multiplier,
        "within_multiplier": rule.within_multiplier,
        "above_multiplier": rule.above_multiplier,
        "required_context": rule.required_context,
    }


def _crit_rule_payload(rule: CritBalanceRule | None) -> dict[str, Any] | None:
    if rule is None:
        return None
    return {
        "target_excess_damage_per_rate": rule.target_excess_damage_per_rate,
        "tolerance": rule.tolerance,
        "max_adjustment": rule.max_adjustment,
        "required_context": rule.required_context,
    }


def _provenance_payload(provenance: Provenance | None) -> dict[str, Any] | None:
    if provenance is None:
        return None
    return {
        "status": str(provenance.status),
        "source": provenance.source,
        "is_official": provenance.is_official,
        "notes": provenance.notes,
    }


def unit_for_stat(stat: StatKey) -> str:
    if stat in {StatKey.FLAT_ATTACK, StatKey.FLAT_HP, StatKey.FLAT_DEFENSE}:
        return ""
    return "%"


__all__ = [
    "DecisionPolicy",
    "DecisionPolicyAdjustments",
    "ProbabilityAssumption",
    "RollDataCatalog",
    "ScoringCatalog",
    "load_decision_policy",
    "load_roll_data",
    "load_scoring_catalog",
    "load_template_profiles",
    "load_template_seed_definitions",
    "unit_for_stat",
]
