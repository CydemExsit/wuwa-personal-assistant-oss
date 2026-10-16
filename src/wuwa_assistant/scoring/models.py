"""Typed, UI-independent models for echo scoring and upgrade decisions."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

type Scalar = str | int | float | bool


class StatKey(StrEnum):
    """Canonical stat identifiers used in versioned profile data."""

    CRIT_RATE = "crit_rate"
    CRIT_DAMAGE = "crit_damage"
    ATTACK_PCT = "attack_pct"
    FLAT_ATTACK = "flat_attack"
    HP_PCT = "hp_pct"
    FLAT_HP = "flat_hp"
    DEFENSE_PCT = "defense_pct"
    FLAT_DEFENSE = "flat_defense"
    ENERGY_REGEN = "energy_regen"
    BASIC_ATTACK_DAMAGE = "basic_attack_damage"
    HEAVY_ATTACK_DAMAGE = "heavy_attack_damage"
    RESONANCE_SKILL_DAMAGE = "resonance_skill_damage"
    RESONANCE_LIBERATION_DAMAGE = "resonance_liberation_damage"


class ProvenanceStatus(StrEnum):
    OFFICIAL_DISCLOSURE = "official_disclosure"
    PROJECT_DEFAULT = "project_default"
    COMMUNITY_OBSERVED_UNVERIFIED = "community_observed_unverified"
    UNIFORM_FALLBACK = "uniform_fallback"


class ScoreBand(StrEnum):
    TRANSITIONAL = "transitional"
    USABLE = "usable"
    PASSING = "passing"
    PERFECT = "perfect"
    EXCEPTIONAL = "exceptional"


class UpgradeAction(StrEnum):
    CONTINUE = "continue"
    STOP = "stop"
    QUALIFIED = "qualified_finish"
    CANNOT_SURPASS = "cannot_surpass_existing"
    MAXED = "max_level"
    MANUAL_CONFIRMATION = "manual_confirmation"


class EmbryoSupply(StrEnum):
    SCARCE = "scarce"
    NORMAL = "normal"
    ABUNDANT = "abundant"


@dataclass(frozen=True, slots=True)
class Provenance:
    status: ProvenanceStatus
    source: str
    is_official: bool
    notes: str


@dataclass(frozen=True, slots=True)
class StatWeight:
    stat: StatKey
    weight: float
    cap_value: float | None = None

    def __post_init__(self) -> None:
        if self.weight < 0:
            raise ValueError("stat weight must be non-negative")
        if self.cap_value is not None and self.cap_value <= 0:
            raise ValueError("stat cap must be positive")


@dataclass(frozen=True, slots=True)
class ConditionalWeight:
    stat: StatKey
    condition_key: str
    expected_value: Scalar
    multiplier: float

    def __post_init__(self) -> None:
        if not self.condition_key:
            raise ValueError("condition key must not be empty")
        if self.multiplier < 0:
            raise ValueError("conditional multiplier must be non-negative")


@dataclass(frozen=True, slots=True)
class ThresholdRule:
    rule_id: str
    stat: StatKey
    minimum: float | None = None
    maximum: float | None = None
    below_multiplier: float = 1.0
    within_multiplier: float = 1.0
    above_multiplier: float = 1.0
    required_context: bool = False

    def __post_init__(self) -> None:
        if self.minimum is None and self.maximum is None:
            raise ValueError("threshold rule needs a minimum or maximum")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("threshold minimum must not exceed maximum")
        if min(self.below_multiplier, self.within_multiplier, self.above_multiplier) < 0:
            raise ValueError("threshold multipliers must be non-negative")


@dataclass(frozen=True, slots=True)
class CritBalanceRule:
    target_excess_damage_per_rate: float = 2.0
    tolerance: float = 0.25
    max_adjustment: float = 0.20
    required_context: bool = False

    def __post_init__(self) -> None:
        if self.target_excess_damage_per_rate <= 0:
            raise ValueError("crit target ratio must be positive")
        if self.tolerance < 0:
            raise ValueError("crit tolerance must be non-negative")
        if not 0 <= self.max_adjustment <= 1:
            raise ValueError("crit max adjustment must be between zero and one")


@dataclass(frozen=True, slots=True)
class RollTier:
    key: str
    label_zh_tw: str
    minimum_ratio: float
    maximum_ratio: float

    def __post_init__(self) -> None:
        if not 0 <= self.minimum_ratio <= self.maximum_ratio <= 1:
            raise ValueError("roll tier ratios must be ordered within zero and one")


@dataclass(frozen=True, slots=True)
class ScoreThresholds:
    usable: float = 55.0
    passing: float = 70.0
    perfect: float = 88.0
    exceptional: float = 95.0

    def __post_init__(self) -> None:
        values = (self.usable, self.passing, self.perfect, self.exceptional)
        if any(value < 0 or value > 100 for value in values):
            raise ValueError("score thresholds must be between zero and one hundred")
        if values != tuple(sorted(values)):
            raise ValueError("score thresholds must be ordered")


@dataclass(frozen=True, slots=True)
class CharacterScoringProfile:
    profile_id: str
    name: str
    template_kind: str
    version: int
    description: str
    thresholds: ScoreThresholds
    weights: tuple[StatWeight, ...]
    conditional_weights: tuple[ConditionalWeight, ...] = ()
    threshold_rules: tuple[ThresholdRule, ...] = ()
    crit_balance_rule: CritBalanceRule | None = None
    roll_data_version: str = ""
    provenance: Provenance | None = None
    is_default: bool = False

    def __post_init__(self) -> None:
        if not self.profile_id or not self.name or not self.template_kind:
            raise ValueError("profile identifiers and name must not be empty")
        if self.version <= 0:
            raise ValueError("profile version must be positive")
        keys = [weight.stat for weight in self.weights]
        if len(keys) != len(set(keys)):
            raise ValueError("profile contains duplicate base stat weights")
        if not any(weight.weight > 0 for weight in self.weights):
            raise ValueError("profile must contain a positive stat weight")

    def weight_for(self, stat: StatKey) -> StatWeight | None:
        return next((weight for weight in self.weights if weight.stat is stat), None)


@dataclass(frozen=True, slots=True)
class ObservedStat:
    """Untrusted OCR/manual stat before exact canonicalization."""

    name: str | None
    value: float | int | None
    unit: str | None
    confidence: float | None
    slot_index: int | None = None


@dataclass(frozen=True, slots=True)
class ScorableStat:
    stat: StatKey
    value: float
    unit: str
    confidence: float
    slot_index: int | None = None


@dataclass(frozen=True, slots=True)
class ScoringContext:
    current_totals: dict[StatKey, float] = field(default_factory=dict)
    conditions: dict[str, Scalar] = field(default_factory=dict)
    panel_crit_rate: float | None = None
    panel_crit_damage: float | None = None


@dataclass(frozen=True, slots=True)
class StatScoreLine:
    stat: StatKey
    value: float
    unit: str
    confidence: float
    roll_ratio: float
    roll_tier: RollTier
    base_weight: float
    roll_credit: float
    conditional_multiplier: float
    threshold_multiplier: float
    crit_balance_multiplier: float
    weighted_points: float
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EchoScoreBreakdown:
    profile_id: str
    profile_version: int
    score: float | None
    band: ScoreBand | None
    lines: tuple[StatScoreLine, ...]
    earned_weight_points: float
    maximum_weight_points: float
    opened_slot_quality_score: float | None = None
    unknown_stats: tuple[str, ...] = ()
    low_confidence_stats: tuple[str, ...] = ()
    invalid_rolls: tuple[str, ...] = ()
    missing_context: tuple[str, ...] = ()
    requires_manual_confirmation: bool = False
    notes: tuple[str, ...] = ()
    ideal_weight_points: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(asdict(self))


@dataclass(frozen=True, slots=True)
class ProjectionSummary:
    assumptions_version: str
    remaining_slots: int
    outcome_count: int
    minimum_score: float | None
    maximum_score: float | None
    expected_score: float | None
    probabilities: dict[str, float]
    total_probability: float
    requires_manual_confirmation: bool = False
    notes: tuple[str, ...] = ()
    improvement_target: float | None = None
    improvement_probability: float | None = None
    expected_positive_gain: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(asdict(self))


@dataclass(frozen=True, slots=True)
class DecisionInputs:
    level: int
    existing_gear_score: float | None = None
    character_already_qualified: bool = False
    breadth_first_progression: bool = True
    embryo_supply: EmbryoSupply = EmbryoSupply.NORMAL
    same_type_candidates: int | None = None
    replacement_margin: float = 0.0

    def __post_init__(self) -> None:
        if self.existing_gear_score is not None and not 0 <= self.existing_gear_score <= 100:
            raise ValueError("existing gear score must be between zero and one hundred")
        if self.same_type_candidates is not None and self.same_type_candidates < 0:
            raise ValueError("candidate count must be non-negative")
        if self.replacement_margin < 0:
            raise ValueError("replacement margin must be non-negative")


@dataclass(frozen=True, slots=True)
class UpgradeDecision:
    action: UpgradeAction
    current_score: float | None
    predicted_min_score: float | None
    predicted_max_score: float | None
    probabilities: dict[str, float]
    assumptions_version: str
    reason: str
    reason_codes: tuple[str, ...]
    requires_manual_confirmation: bool = False
    suggested_level: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(asdict(self))


def _json_safe(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


__all__ = [
    "CharacterScoringProfile",
    "ConditionalWeight",
    "CritBalanceRule",
    "DecisionInputs",
    "EchoScoreBreakdown",
    "EmbryoSupply",
    "ObservedStat",
    "ProjectionSummary",
    "Provenance",
    "ProvenanceStatus",
    "RollTier",
    "ScorableStat",
    "ScoreBand",
    "ScoreThresholds",
    "ScoringContext",
    "StatKey",
    "StatScoreLine",
    "StatWeight",
    "ThresholdRule",
    "UpgradeAction",
    "UpgradeDecision",
]
