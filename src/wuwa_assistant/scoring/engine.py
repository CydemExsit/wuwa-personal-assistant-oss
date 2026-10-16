"""Explainable echo scoring and bounded remaining-slot enumeration."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from itertools import combinations
from math import isfinite, prod

from wuwa_assistant.models.domain import EchoObservation

from .calibration import probability_reference, score_points
from .data import LEGACY_ROLL_VERSION, RollDataCatalog, load_roll_data, unit_for_stat
from .models import (
    CharacterScoringProfile,
    EchoScoreBreakdown,
    ObservedStat,
    ProjectionSummary,
    RollTier,
    ScorableStat,
    ScoreBand,
    ScoringContext,
    StatKey,
    StatScoreLine,
    ThresholdRule,
)
from .projection import score_distribution, summarize_distribution

_EXACT_NAME_MAP: dict[tuple[str, str], StatKey] = {
    ("暴擊", "%"): StatKey.CRIT_RATE,
    ("暴擊傷害", "%"): StatKey.CRIT_DAMAGE,
    ("攻擊", "%"): StatKey.ATTACK_PCT,
    ("攻擊百分比", "%"): StatKey.ATTACK_PCT,
    ("攻擊", ""): StatKey.FLAT_ATTACK,
    ("生命", "%"): StatKey.HP_PCT,
    ("生命百分比", "%"): StatKey.HP_PCT,
    ("生命", ""): StatKey.FLAT_HP,
    ("防禦", "%"): StatKey.DEFENSE_PCT,
    ("防禦百分比", "%"): StatKey.DEFENSE_PCT,
    ("防禦", ""): StatKey.FLAT_DEFENSE,
    ("共鳴效率", "%"): StatKey.ENERGY_REGEN,
    ("普攻傷害加成", "%"): StatKey.BASIC_ATTACK_DAMAGE,
    ("重擊傷害加成", "%"): StatKey.HEAVY_ATTACK_DAMAGE,
    ("共鳴技能傷害加成", "%"): StatKey.RESONANCE_SKILL_DAMAGE,
    ("共鳴解放傷害加成", "%"): StatKey.RESONANCE_LIBERATION_DAMAGE,
}


def canonical_stat_key(name: str | None, unit: str | None) -> StatKey | None:
    """Map only exact known Traditional Chinese labels; never fuzzy-guess."""

    if name is None:
        return None
    normalized_name = name.strip()
    normalized_unit = (unit or "").strip()
    return _EXACT_NAME_MAP.get((normalized_name, normalized_unit))


def observed_stats_from_echo(observation: EchoObservation) -> tuple[ObservedStat, ...]:
    """Convert only unlocked substats; main-stat correctness is a separate concern."""

    result: list[ObservedStat] = []
    for stat in observation.sub_stats:
        confidence_candidates = (
            stat.confidence,
            stat.name_confidence,
            stat.value_confidence,
        )
        present = [value for value in confidence_candidates if value is not None]
        confidence = min(present) if present else None
        result.append(
            ObservedStat(
                name=stat.name,
                value=stat.value,
                unit=stat.unit,
                confidence=confidence,
                slot_index=stat.slot_index,
            )
        )
    return tuple(result)


class EchoScoringEngine:
    """Data-driven scorer with conservative input validation."""

    def __init__(
        self,
        roll_data: RollDataCatalog | None = None,
        *,
        minimum_confidence: float = 0.85,
    ) -> None:
        if not 0 <= minimum_confidence <= 1:
            raise ValueError("minimum confidence must be between zero and one")
        self._rolls = roll_data or load_roll_data()
        self._minimum_confidence = minimum_confidence
        self._legacy_engine: EchoScoringEngine | None = None

    @property
    def minimum_confidence(self) -> float:
        """Confidence boundary used to accept a stat for scoring."""

        return self._minimum_confidence

    @property
    def assumptions_version(self) -> str:
        return self._rolls.data_version

    def supports_version(self, version: str | None) -> bool:
        return version in {self.assumptions_version, LEGACY_ROLL_VERSION}

    def _legacy_for(self, profile: CharacterScoringProfile) -> EchoScoringEngine | None:
        if (
            profile.roll_data_version == LEGACY_ROLL_VERSION
            and self.assumptions_version != LEGACY_ROLL_VERSION
        ):
            if self._legacy_engine is None:
                self._legacy_engine = EchoScoringEngine(
                    load_roll_data(LEGACY_ROLL_VERSION), minimum_confidence=self._minimum_confidence
                )
            return self._legacy_engine
        return None

    def score_echo(
        self,
        profile: CharacterScoringProfile,
        observation: EchoObservation,
        context: ScoringContext | None = None,
    ) -> EchoScoreBreakdown:
        return self.score(profile, observed_stats_from_echo(observation), context)

    def score(
        self,
        profile: CharacterScoringProfile,
        observed_stats: Iterable[ObservedStat],
        context: ScoringContext | None = None,
    ) -> EchoScoreBreakdown:
        legacy = self._legacy_for(profile)
        if legacy is not None:
            return legacy.score(profile, observed_stats, context)
        scoring_context = context or ScoringContext()
        canonical, unknown, low_confidence, invalid_rolls = self._validate_stats(observed_stats)
        duplicate_names = self._duplicates(item.stat for item in canonical)
        if duplicate_names:
            unknown.extend(f"duplicate:{stat}" for stat in duplicate_names)
        missing_weights = [
            str(item.stat) for item in canonical if profile.weight_for(item.stat) is None
        ]
        if self._rolls.probability_assumption.is_official:
            missing_weights.extend(
                str(stat) for stat in StatKey if profile.weight_for(stat) is None
            )
        unknown.extend(f"profile_missing_weight:{item}" for item in missing_weights)

        eligible = [
            item
            for item in canonical
            if item.stat not in duplicate_names and profile.weight_for(item.stat) is not None
        ]
        missing_context = self._missing_required_context(profile, scoring_context)
        breakdown = self._score_canonical(profile, eligible, scoring_context, missing_context)
        rejected = bool(unknown or low_confidence or invalid_rolls)
        notes = list(breakdown.notes)
        if rejected:
            notes.append("存在無法安全確認的詞條, 分數不作為真值")
        return EchoScoreBreakdown(
            profile_id=profile.profile_id,
            profile_version=profile.version,
            score=None if rejected else breakdown.score,
            band=None if rejected else breakdown.band,
            lines=breakdown.lines,
            earned_weight_points=breakdown.earned_weight_points,
            maximum_weight_points=breakdown.maximum_weight_points,
            opened_slot_quality_score=(None if rejected else breakdown.opened_slot_quality_score),
            unknown_stats=tuple(sorted(set(unknown))),
            low_confidence_stats=tuple(sorted(set(low_confidence))),
            invalid_rolls=tuple(sorted(set(invalid_rolls))),
            missing_context=breakdown.missing_context,
            requires_manual_confirmation=rejected or bool(missing_context),
            notes=tuple(notes),
            ideal_weight_points=breakdown.ideal_weight_points,
        )

    def enumerate_remaining(
        self,
        profile: CharacterScoringProfile,
        observed_stats: Iterable[ObservedStat],
        context: ScoringContext | None = None,
        *,
        total_slots: int = 5,
        improvement_target: float | None = None,
    ) -> ProjectionSummary:
        """Enumerate unseen types and their versioned conditional value probabilities."""

        if improvement_target is not None and (
            not isfinite(improvement_target) or improvement_target < 0
        ):
            raise ValueError("improvement target must be finite and nonnegative")
        legacy = self._legacy_for(profile)
        if legacy is not None:
            return legacy.enumerate_remaining(
                profile,
                observed_stats,
                context,
                total_slots=total_slots,
                improvement_target=improvement_target,
            )
        raw_stats = tuple(observed_stats)
        current = self.score(profile, raw_stats, context)
        remaining = total_slots - len(raw_stats)
        if remaining < 0 or total_slots <= 0:
            raise ValueError("total slots must be positive and not below observed stat count")
        if current.score is None or current.requires_manual_confirmation:
            return ProjectionSummary(
                assumptions_version=self.assumptions_version,
                remaining_slots=remaining,
                outcome_count=0,
                minimum_score=None,
                maximum_score=None,
                expected_score=None,
                probabilities={},
                total_probability=0.0,
                requires_manual_confirmation=True,
                notes=("目前資料需要人工確認, 未建立機率預測",),
            )

        canonical, _, _, _ = self._validate_stats(raw_stats)
        used = {item.stat for item in canonical}
        candidates = tuple(
            weight.stat
            for weight in profile.weights
            if weight.stat not in used and self._rolls.values_for(weight.stat)
        )
        if remaining > len(candidates):
            return ProjectionSummary(
                assumptions_version=self.assumptions_version,
                remaining_slots=remaining,
                outcome_count=0,
                minimum_score=None,
                maximum_score=None,
                expected_score=None,
                probabilities={},
                total_probability=0.0,
                requires_manual_confirmation=True,
                notes=("評分設定檔沒有足夠的可枚舉詞條",),
            )
        if remaining == 0:
            probabilities = _threshold_probabilities(profile, ((current.score, 1.0),))
            return ProjectionSummary(
                assumptions_version=self.assumptions_version,
                remaining_slots=0,
                outcome_count=1,
                minimum_score=current.score,
                maximum_score=current.score,
                expected_score=current.score,
                probabilities=probabilities,
                total_probability=1.0,
                notes=(self._rolls.probability_assumption.notes,),
                improvement_target=improvement_target,
                improvement_probability=(
                    None
                    if improvement_target is None
                    else float(current.score > improvement_target)
                ),
                expected_positive_gain=(
                    None
                    if improvement_target is None
                    else round(max(0.0, current.score - improvement_target), 4)
                ),
            )

        context_value = context or ScoringContext()
        threshold_values = {
            "usable": profile.thresholds.usable,
            "passing": profile.thresholds.passing,
            "perfect": profile.thresholds.perfect,
            "exceptional": profile.thresholds.exceptional,
        }
        contribution_options: dict[StatKey, tuple[tuple[float, float], ...]] = {}
        for stat in candidates:
            values = self._rolls.values_for(stat)
            if not values:  # guarded by candidate construction; keeps the type boundary explicit
                raise RuntimeError(f"missing Roll values for projection stat: {stat}")
            contribution_options[stat] = tuple(
                (
                    self._line(
                        profile,
                        ScorableStat(
                            stat=stat,
                            value=value,
                            unit=unit_for_stat(stat),
                            confidence=1.0,
                        ),
                        context_value,
                    ).weighted_points,
                    probability,
                )
                for value, probability in zip(
                    values, self._rolls.probabilities_for(stat), strict=True
                )
            )
            # Zero-weight stats still occupy slots and keep all their probability
            # mass, but their identical score outcomes need only one enumeration.
            grouped: dict[float, float] = {}
            for points, probability in contribution_options[stat]:
                grouped[points] = grouped.get(points, 0.0) + probability
            contribution_options[stat] = tuple(grouped.items())
        outcome_count = sum(
            prod(len(self._rolls.roll_values[stat]) for stat in selected)
            for selected in combinations(candidates, remaining)
        )
        distribution = score_distribution(
            tuple(contribution_options[stat] for stat in candidates),
            remaining,
            current.earned_weight_points,
            current.maximum_weight_points,
            current.ideal_weight_points,
        )
        (
            minimum_score,
            maximum_score,
            weighted_score_sum,
            total_probability,
            threshold_mass,
            improvement_mass,
            positive_gain_sum,
        ) = summarize_distribution(distribution, threshold_values, improvement_target)

        if outcome_count == 0 or total_probability <= 0:
            return ProjectionSummary(
                assumptions_version=self.assumptions_version,
                remaining_slots=remaining,
                outcome_count=0,
                minimum_score=None,
                maximum_score=None,
                expected_score=None,
                probabilities={},
                total_probability=0.0,
                requires_manual_confirmation=True,
                notes=("無法建立剩餘詞條組合",),
            )
        expected = weighted_score_sum / total_probability
        return ProjectionSummary(
            assumptions_version=self.assumptions_version,
            remaining_slots=remaining,
            outcome_count=outcome_count,
            minimum_score=round(minimum_score, 2),
            maximum_score=round(maximum_score, 2),
            expected_score=round(expected, 2),
            probabilities={
                name: round(mass / total_probability, 6) for name, mass in threshold_mass.items()
            },
            total_probability=round(total_probability, 10),
            improvement_target=improvement_target,
            improvement_probability=(
                None
                if improvement_target is None
                else round(improvement_mass / total_probability, 6)
            ),
            expected_positive_gain=(
                None
                if improvement_target is None
                else round(positive_gain_sum / total_probability, 4)
            ),
            notes=(
                self._rolls.probability_assumption.notes,
                "逐一枚舉未出現詞條組合與每個版本化 Roll 值",
            ),
        )

    def _validate_stats(
        self, observed_stats: Iterable[ObservedStat]
    ) -> tuple[list[ScorableStat], list[str], list[str], list[str]]:
        canonical: list[ScorableStat] = []
        unknown: list[str] = []
        low_confidence: list[str] = []
        invalid_rolls: list[str] = []
        for index, item in enumerate(observed_stats):
            label = item.name.strip() if item.name else f"slot-{item.slot_index or index + 1}"
            stat = canonical_stat_key(item.name, item.unit)
            if stat is None or item.value is None:
                unknown.append(label)
                continue
            if item.confidence is None or item.confidence < self._minimum_confidence:
                low_confidence.append(label)
                continue
            value = float(item.value)
            if not self._is_known_roll(stat, value):
                invalid_rolls.append(f"{label}:{value:g}{item.unit or ''}")
                continue
            canonical.append(
                ScorableStat(
                    stat=stat,
                    value=value,
                    unit=unit_for_stat(stat),
                    confidence=item.confidence,
                    slot_index=item.slot_index,
                )
            )
        return canonical, unknown, low_confidence, invalid_rolls

    def _is_known_roll(self, stat: StatKey, value: float) -> bool:
        values = self._rolls.values_for(stat)
        if not values:
            return False
        integer_table = all(item.is_integer() for item in values)
        tolerance = 0.51 if integer_table else 0.051
        return min(abs(value - candidate) for candidate in values) <= tolerance

    @staticmethod
    def _duplicates(values: Iterable[StatKey]) -> set[StatKey]:
        counts: dict[StatKey, int] = defaultdict(int)
        for value in values:
            counts[value] += 1
        return {value for value, count in counts.items() if count > 1}

    def _score_canonical(
        self,
        profile: CharacterScoringProfile,
        stats: Iterable[ScorableStat],
        context: ScoringContext,
        missing_context: tuple[str, ...],
    ) -> EchoScoreBreakdown:
        stats_tuple = tuple(stats)
        maximum = sum(sorted((weight.weight for weight in profile.weights), reverse=True)[:5])
        reference = self._rolls.reference_quantile
        ideal = None
        if reference is not None and {w.stat for w in profile.weights} == set(StatKey):
            options = tuple(
                tuple(
                    (
                        self._line(
                            profile,
                            ScorableStat(stat, value, unit_for_stat(stat), 1.0),
                            context,
                        ).weighted_points,
                        probability,
                    )
                    for value, probability in zip(
                        self._rolls.roll_values[stat],
                        self._rolls.probabilities_for(stat),
                        strict=True,
                    )
                )
                for stat in StatKey
            )
            maximum = probability_reference(options, reference) or maximum
            ideal = sum(sorted((max(p for p, _ in row) for row in options), reverse=True)[:5])
            maximum = min(maximum, ideal)
        lines = tuple(self._line(profile, stat, context) for stat in stats_tuple)
        earned = sum(line.weighted_points for line in lines)
        score = score_points(earned, maximum, ideal) if maximum else None
        opened_slot_maximum = sum(
            sorted((weight.weight for weight in profile.weights), reverse=True)[: len(lines)]
        )
        opened_slot_quality_score = (
            round(
                min(100.0, max(0.0, earned / opened_slot_maximum * 100)),
                2,
            )
            if lines and opened_slot_maximum
            else None
        )
        notes: list[str] = []
        if ideal is not None:
            notes.append(
                "實用評分: 官方機率推算的隨機五詞條品質 P99 對應 90 分, "
                "90 到 100 保留追求頂值的空間; 工具評分不是官方傷害公式。"
            )
        if profile.crit_balance_rule is not None and (
            context.panel_crit_rate is None or context.panel_crit_damage is None
        ):
            notes.append("未提供完整面板暴擊資料; 本次不套用暴擊平衡修正")
        if missing_context:
            notes.append("缺少必要角色門檻資料, 建議人工確認")
        return EchoScoreBreakdown(
            profile_id=profile.profile_id,
            profile_version=profile.version,
            score=score,
            band=score_band(score, profile) if score is not None else None,
            lines=lines,
            earned_weight_points=round(earned, 6),
            maximum_weight_points=round(maximum, 6),
            opened_slot_quality_score=opened_slot_quality_score,
            missing_context=missing_context,
            requires_manual_confirmation=bool(missing_context),
            notes=tuple(notes),
            ideal_weight_points=None if ideal is None else round(ideal, 6),
        )

    def _line(
        self,
        profile: CharacterScoringProfile,
        stat: ScorableStat,
        context: ScoringContext,
    ) -> StatScoreLine:
        weight = profile.weight_for(stat.stat)
        if weight is None:
            raise ValueError(f"profile is missing weight for {stat.stat}")
        value = min(stat.value, weight.cap_value) if weight.cap_value is not None else stat.value
        roll_ratio = self._roll_ratio(stat.stat, value)
        tier = self._roll_tier(roll_ratio)
        roll_credit = (
            self._rolls.minimum_roll_credit + (1.0 - self._rolls.minimum_roll_credit) * roll_ratio
        )
        conditional_multiplier = 1.0
        notes: list[str] = []
        for conditional in profile.conditional_weights:
            if conditional.stat is not stat.stat:
                continue
            if context.conditions.get(conditional.condition_key) == conditional.expected_value:
                conditional_multiplier *= conditional.multiplier
                notes.append(f"條件 {conditional.condition_key} 已套用")
        threshold_multiplier = 1.0
        for rule in profile.threshold_rules:
            if rule.stat is not stat.stat:
                continue
            applied = self._threshold_multiplier(rule, stat.value, context)
            threshold_multiplier *= applied
            if applied != 1.0:
                notes.append(f"門檻 {rule.rule_id} 倍率 {applied:.2f}")
        crit_multiplier = self._crit_multiplier(profile, stat.stat, context)
        if crit_multiplier != 1.0:
            notes.append(f"暴擊平衡倍率 {crit_multiplier:.2f}")
        points = (
            weight.weight
            * roll_credit
            * conditional_multiplier
            * threshold_multiplier
            * crit_multiplier
        )
        return StatScoreLine(
            stat=stat.stat,
            value=stat.value,
            unit=stat.unit,
            confidence=stat.confidence,
            roll_ratio=round(roll_ratio, 6),
            roll_tier=tier,
            base_weight=weight.weight,
            roll_credit=round(roll_credit, 6),
            conditional_multiplier=round(conditional_multiplier, 6),
            threshold_multiplier=round(threshold_multiplier, 6),
            crit_balance_multiplier=round(crit_multiplier, 6),
            weighted_points=round(points, 6),
            notes=tuple(notes),
        )

    def _roll_ratio(self, stat: StatKey, value: float) -> float:
        values = self._rolls.values_for(stat)
        if not values:
            raise ValueError(f"no roll values for {stat}")
        low, high = values[0], values[-1]
        if high == low:
            return 1.0
        return min(1.0, max(0.0, (value - low) / (high - low)))

    def _roll_tier(self, ratio: float) -> RollTier:
        for tier in self._rolls.tiers:
            if tier.minimum_ratio <= ratio <= tier.maximum_ratio:
                return tier
        return self._rolls.tiers[-1]

    @staticmethod
    def _threshold_multiplier(
        rule: ThresholdRule,
        stat_value: float,
        context: ScoringContext,
    ) -> float:
        current = context.current_totals.get(rule.stat)
        if current is None:
            return 1.0
        total = current + stat_value
        if rule.minimum is not None and total < rule.minimum:
            return rule.below_multiplier
        if rule.maximum is not None and total > rule.maximum:
            return rule.above_multiplier
        return rule.within_multiplier

    @staticmethod
    def _crit_multiplier(
        profile: CharacterScoringProfile,
        stat: StatKey,
        context: ScoringContext,
    ) -> float:
        rule = profile.crit_balance_rule
        if rule is None or stat not in {StatKey.CRIT_RATE, StatKey.CRIT_DAMAGE}:
            return 1.0
        if context.panel_crit_rate is None or context.panel_crit_damage is None:
            return 1.0
        rate = max(context.panel_crit_rate, 0.0001)
        excess_damage = max(context.panel_crit_damage - 100.0, 0.0)
        ratio = excess_damage / rate
        difference = ratio - rule.target_excess_damage_per_rate
        if abs(difference) <= rule.tolerance:
            return 1.0
        adjustment = min(
            rule.max_adjustment,
            abs(difference) / rule.target_excess_damage_per_rate * rule.max_adjustment,
        )
        if difference > 0:
            return 1.0 + adjustment if stat is StatKey.CRIT_RATE else 1.0 - adjustment
        return 1.0 - adjustment if stat is StatKey.CRIT_RATE else 1.0 + adjustment

    @staticmethod
    def _missing_required_context(
        profile: CharacterScoringProfile,
        context: ScoringContext,
    ) -> tuple[str, ...]:
        missing = [
            f"current_totals.{rule.stat}"
            for rule in profile.threshold_rules
            if rule.required_context and rule.stat not in context.current_totals
        ]
        crit = profile.crit_balance_rule
        if crit is not None and crit.required_context:
            if context.panel_crit_rate is None:
                missing.append("panel_crit_rate")
            if context.panel_crit_damage is None:
                missing.append("panel_crit_damage")
        return tuple(sorted(set(missing)))


def score_band(score: float, profile: CharacterScoringProfile) -> ScoreBand:
    thresholds = profile.thresholds
    if score >= thresholds.exceptional:
        return ScoreBand.EXCEPTIONAL
    if score >= thresholds.perfect:
        return ScoreBand.PERFECT
    if score >= thresholds.passing:
        return ScoreBand.PASSING
    if score >= thresholds.usable:
        return ScoreBand.USABLE
    return ScoreBand.TRANSITIONAL


def _threshold_probabilities(
    profile: CharacterScoringProfile,
    score_probability_pairs: Iterable[tuple[float, float]],
) -> dict[str, float]:
    pairs = tuple(score_probability_pairs)
    total = sum(probability for _, probability in pairs)
    if total <= 0:
        return {}
    thresholds = {
        "usable": profile.thresholds.usable,
        "passing": profile.thresholds.passing,
        "perfect": profile.thresholds.perfect,
        "exceptional": profile.thresholds.exceptional,
    }
    return {
        name: round(
            sum(probability for score, probability in pairs if score >= threshold) / total,
            6,
        )
        for name, threshold in thresholds.items()
    }


__all__ = [
    "EchoScoringEngine",
    "canonical_stat_key",
    "observed_stats_from_echo",
    "score_band",
]
