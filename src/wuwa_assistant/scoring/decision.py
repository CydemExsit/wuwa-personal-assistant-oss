"""Versioned, target-relative investment screening with bounded exploration."""

from __future__ import annotations

from .data import DecisionPolicy, load_decision_policy
from .models import (
    CharacterScoringProfile,
    DecisionInputs,
    EchoScoreBreakdown,
    EmbryoSupply,
    ProjectionSummary,
    UpgradeAction,
    UpgradeDecision,
)
from .upgrade_costs import next_node_cost, upgrade_cost_to_level


class UpgradeDecisionEngine:
    """Choose a conservative action from a score and enumerated projection."""

    def __init__(self, policy: DecisionPolicy | None = None) -> None:
        self._policy = policy or load_decision_policy()

    @property
    def assumptions_version(self) -> str:
        return self._policy.data_version

    def decide(
        self,
        profile: CharacterScoringProfile,
        breakdown: EchoScoreBreakdown,
        projection: ProjectionSummary,
        inputs: DecisionInputs,
    ) -> UpgradeDecision:
        from dataclasses import replace

        result = self._decide(profile, breakdown, projection, inputs)
        if type(inputs.level) is int and 0 <= inputs.level < 25:
            checkpoint = result.suggested_level
            if (
                inputs.level < self._policy.exploration_level
                and result.action is UpgradeAction.STOP
            ):
                checkpoint = self._policy.exploration_level
            cost = (
                upgrade_cost_to_level(inputs.level, checkpoint)
                if checkpoint is not None
                else next_node_cost(inputs.level)
            )
            if cost is not None and not result.requires_manual_confirmation:
                result = replace(result, reason=result.reason + "\n" + cost.explanation)
        return result

    def _decide(
        self,
        profile: CharacterScoringProfile,
        breakdown: EchoScoreBreakdown,
        projection: ProjectionSummary,
        inputs: DecisionInputs,
    ) -> UpgradeDecision:
        assumptions_version = self._combined_assumptions_version(projection)
        if type(inputs.level) is not int or not 5 <= inputs.level <= 25:
            return self._manual(
                breakdown,
                projection,
                "尚未開啟第一詞條或等級不在 0-25; 到 +5 調諧後再判斷投入。",
                "unsupported_level",
                assumptions_version,
            )
        if (
            breakdown.score is None
            or breakdown.requires_manual_confirmation
            or projection.requires_manual_confirmation
        ):
            return self._manual(
                breakdown,
                projection,
                "OCR/Roll 或角色門檻資料不足, 需要人工確認。",
                "unconfirmed_input",
                assumptions_version,
            )
        if projection.remaining_slots != 5 - inputs.level // 5:
            return self._manual(
                breakdown,
                projection,
                "已解鎖副詞條尚未全部確認, 請補齊後再判斷投入。",
                "unconfirmed_unlocked_slots",
                assumptions_version,
            )
        if inputs.level == 25:
            return UpgradeDecision(
                action=UpgradeAction.MAXED,
                current_score=breakdown.score,
                predicted_min_score=projection.minimum_score,
                predicted_max_score=projection.maximum_score,
                probabilities=dict(projection.probabilities),
                assumptions_version=assumptions_version,
                reason="聲骸已達 +25, 沒有剩餘強化節點。",
                reason_codes=("maximum_level",),
            )
        if projection.maximum_score is None or projection.minimum_score is None:
            return self._manual(
                breakdown,
                projection,
                "缺少可用的剩餘詞條預測, 需要人工確認。",
                "projection_unavailable",
                assumptions_version,
            )

        if inputs.existing_gear_score is not None:
            required = inputs.existing_gear_score + inputs.replacement_margin
            if projection.maximum_score <= required:
                return UpgradeDecision(
                    action=UpgradeAction.CANNOT_SURPASS,
                    current_score=breakdown.score,
                    predicted_min_score=projection.minimum_score,
                    predicted_max_score=projection.maximum_score,
                    probabilities=dict(projection.probabilities),
                    assumptions_version=assumptions_version,
                    reason=(
                        f"即使剩餘詞條達到目前模型上限 {projection.maximum_score:.1f}, "
                        f"仍無法超過替換目標 {required:.1f}。"
                    ),
                    reason_codes=("cannot_reach_replacement_target",),
                )
            if (
                projection.improvement_target != required
                or projection.improvement_probability is None
                or projection.expected_positive_gain is None
            ):
                return self._manual(
                    breakdown,
                    projection,
                    "尚無針對目前裝備的超越機率, 請重新計算。",
                    "replacement_probability_unavailable",
                    assumptions_version,
                )
            chance = projection.improvement_probability
            required_chance = self._required_continue_chance(inputs, breakdown.score, profile)
            quality = self._weak_baseline_quality_guard(
                profile, breakdown, projection, inputs, required_chance, assumptions_version
            )
            if quality is not None and quality.action is not UpgradeAction.CONTINUE:
                return quality
            required_gain = self._required_expected_gain(inputs)
            close_high_quality = (
                breakdown.score >= profile.thresholds.passing
                and inputs.existing_gear_score >= profile.thresholds.passing
                and 0
                <= inputs.existing_gear_score - breakdown.score
                <= self._policy.high_quality_gap
            )
            relaxation_reason = ""
            if close_high_quality:
                required_chance *= self._policy.high_quality_factor
                required_gain *= self._policy.high_quality_factor
                relaxation_reason = (
                    f"兩顆目前都達角色及格 {profile.thresholds.passing:.1f} 分, "
                    f"候選落後不超過 {self._policy.high_quality_gap:g} 分; "
                    "本次超越機率與平均提升門檻減半, 給高品質候選更多強化機會。"
                )
            chance_sufficient = chance >= required_chance
            gain_sufficient = projection.expected_positive_gain >= required_gain
            continue_upgrade = chance_sufficient and gain_sufficient
            suggested_level = self._checkpoint_level(inputs.level)
            early = inputs.level < self._policy.exploration_level
            expected = projection.expected_score
            expected_text = "—" if expected is None else f"{expected:.2f}"
            return UpgradeDecision(
                action=UpgradeAction.CONTINUE if continue_upgrade else UpgradeAction.STOP,
                current_score=breakdown.score,
                predicted_min_score=projection.minimum_score,
                predicted_max_score=projection.maximum_score,
                probabilities={**projection.probabilities, "improve_existing": chance},
                assumptions_version=assumptions_version,
                reason=(
                    (quality.reason if quality is not None else "")
                    + relaxation_reason
                    + f"超越現有 {required:.2f} 分的機率 {chance:.1%}, "
                    f"{'達到' if chance_sufficient else '低於'}本次投入門檻 "
                    f"{required_chance:.1%}; "
                    f"完成期望 {expected_text} 分, 保留現有裝備時的平均可得提升 "
                    f"{projection.expected_positive_gain:.2f} 分 "
                    f"({'達到' if gain_sufficient else '低於'}成本對應門檻 {required_gain:.2f})。"
                    + (
                        (
                            "先到 +15 確認三條副詞條再評估, 不代表建議直接升滿。"
                            if early
                            else f"可到 +{suggested_level} 再評估。"
                        )
                        if continue_upgrade
                        else "建議保留現有裝備、改練其他胚子。"
                    )
                ),
                reason_codes=(
                    *(quality.reason_codes if quality is not None else ()),
                    *(("high_quality_close_target_relaxation",) if close_high_quality else ()),
                    "replacement_probability_sufficient"
                    if chance_sufficient
                    else "replacement_probability_too_low",
                    "replacement_gain_sufficient"
                    if gain_sufficient
                    else "replacement_gain_too_small",
                    "exploration_checkpoint_considered" if early else "next_node_cost_considered",
                ),
                suggested_level=suggested_level if continue_upgrade else None,
            )

        if breakdown.score >= profile.thresholds.passing:
            return UpgradeDecision(
                action=UpgradeAction.QUALIFIED,
                current_score=breakdown.score,
                predicted_min_score=projection.minimum_score,
                predicted_max_score=projection.maximum_score,
                probabilities=dict(projection.probabilities),
                assumptions_version=assumptions_version,
                reason="目前分數已達設定檔的及格門檻, 可升滿並保留。",
                reason_codes=("passing_threshold_reached",),
                suggested_level=25,
            )

        if inputs.level < self._policy.exploration_level:
            return UpgradeDecision(
                action=UpgradeAction.CONTINUE,
                current_score=breakdown.score,
                predicted_min_score=projection.minimum_score,
                predicted_max_score=projection.maximum_score,
                probabilities=dict(projection.probabilities),
                assumptions_version=assumptions_version,
                reason="尚無替換目標且未滿三條副詞條; 先到 +15 確認三條後再判斷, 不急於淘汰。",
                reason_codes=("early_exploration_without_target",),
                suggested_level=self._policy.exploration_level,
            )

        pass_chance = projection.probabilities.get("passing", 0.0)
        if projection.maximum_score < profile.thresholds.passing:
            return UpgradeDecision(
                action=UpgradeAction.STOP,
                current_score=breakdown.score,
                predicted_min_score=projection.minimum_score,
                predicted_max_score=projection.maximum_score,
                probabilities=dict(projection.probabilities),
                assumptions_version=assumptions_version,
                reason="剩餘詞條的模型上限仍無法達到目前及格門檻。",
                reason_codes=("passing_unreachable",),
            )

        required_chance = self._required_continue_chance(inputs, breakdown.score, profile)
        if pass_chance >= required_chance:
            reason_codes = ["passing_probability_sufficient"]
            if inputs.breadth_first_progression and not inputs.character_already_qualified:
                reason_codes.append("breadth_first_gap")
            return UpgradeDecision(
                action=UpgradeAction.CONTINUE,
                current_score=breakdown.score,
                predicted_min_score=projection.minimum_score,
                predicted_max_score=projection.maximum_score,
                probabilities=dict(projection.probabilities),
                assumptions_version=assumptions_version,
                reason=(
                    f"達到及格門檻的枚舉機率為 {pass_chance:.1%}, "
                    f"高於目前情境所需的 {required_chance:.1%}。"
                ),
                reason_codes=tuple(reason_codes),
            )

        return UpgradeDecision(
            action=UpgradeAction.STOP,
            current_score=breakdown.score,
            predicted_min_score=projection.minimum_score,
            predicted_max_score=projection.maximum_score,
            probabilities=dict(projection.probabilities),
            assumptions_version=assumptions_version,
            reason=(
                f"達到及格門檻的枚舉機率為 {pass_chance:.1%}, "
                f"低於目前胚子供給及養成狀態所需的 {required_chance:.1%}。"
            ),
            reason_codes=("passing_probability_too_low",),
        )

    def _weak_baseline_quality_guard(
        self,
        profile: CharacterScoringProfile,
        breakdown: EchoScoreBreakdown,
        projection: ProjectionSummary,
        inputs: DecisionInputs,
        required_chance: float,
        assumptions_version: str,
    ) -> UpgradeDecision | None:
        """An easy replacement is insufficient if the candidate is likely to stay poor."""
        floor = profile.thresholds.usable
        if (
            inputs.level < self._policy.weak_baseline_quality_level
            or inputs.existing_gear_score is None
            or inputs.existing_gear_score >= floor
            or breakdown.score is None
            or breakdown.score >= floor
        ):
            return None
        chance = projection.probabilities.get("usable")
        if projection.maximum_score is not None and projection.maximum_score < floor:
            action = UpgradeAction.STOP
            code = "weak_baseline_quality_unreachable"
            reason = (
                f"現有與候選都偏弱; 即使升滿達模型上限 {projection.maximum_score:.1f} 分, "
                f"仍低於可用門檻 {floor:.1f} 分。建議停止追加材料、改練其他胚子。"
            )
        elif chance is None or not 0 <= chance <= 1:
            return self._manual(
                breakdown,
                projection,
                f"現有與候選都低於可用門檻 {floor:.1f} 分, "
                "缺少達到此品質的機率, 請重新計算後再判斷投入。",
                "weak_baseline_quality_probability_unavailable",
                assumptions_version,
            )
        else:
            sufficient = chance >= required_chance
            action = UpgradeAction.CONTINUE if sufficient else UpgradeAction.STOP
            code = (
                "weak_baseline_quality_probability_sufficient"
                if sufficient
                else "weak_baseline_quality_probability_too_low"
            )
            reason = (
                f"現有與候選都偏弱; 升滿達到可用 {floor:.1f} 分的機率 {chance:.1%}, "
                f"{'達到' if sufficient else '低於'}本次成本與供給門檻 {required_chance:.1%}。"
                + (
                    "仍需同時滿足超越現有的機率與提升幅度。"
                    if sufficient
                    else "即使能超過現有, 仍容易留下低分聲骸, 建議停止追加材料。"
                )
            )
        probabilities = dict(projection.probabilities)
        if projection.improvement_probability is not None:
            probabilities["improve_existing"] = projection.improvement_probability
        return UpgradeDecision(
            action=action,
            current_score=breakdown.score,
            predicted_min_score=projection.minimum_score,
            predicted_max_score=projection.maximum_score,
            probabilities=probabilities,
            assumptions_version=assumptions_version,
            reason=reason,
            reason_codes=("weak_baseline_quality_considered", code),
        )

    def _required_continue_chance(
        self,
        inputs: DecisionInputs,
        current_score: float,
        profile: CharacterScoringProfile,
    ) -> float:
        policy = self._policy
        adjustments = policy.adjustments
        node = inputs.level // 5 * 5
        early = inputs.level < policy.exploration_level
        required = policy.exploration_base_chance if early else policy.base_continue_chance[node]
        cost = upgrade_cost_to_level(inputs.level, self._checkpoint_level(inputs.level))
        penalty = policy.exploration_cost_chance_penalty if early else policy.cost_chance_penalty
        required += penalty * cost.experience / policy.cost_reference_experience
        supply_adjustment = 0.0
        if inputs.embryo_supply is EmbryoSupply.ABUNDANT:
            supply_adjustment += adjustments.embryo_supply_abundant
        elif inputs.embryo_supply is EmbryoSupply.SCARCE:
            supply_adjustment += adjustments.embryo_supply_scarce
        if inputs.same_type_candidates is not None:
            if inputs.same_type_candidates >= adjustments.many_candidates_at_least:
                supply_adjustment += adjustments.many_candidates
            elif inputs.same_type_candidates <= adjustments.few_candidates_at_most:
                supply_adjustment += adjustments.few_candidates
        required += supply_adjustment * (policy.exploration_supply_factor if early else 1)
        if inputs.existing_gear_score is None:
            if inputs.breadth_first_progression and not inputs.character_already_qualified:
                required += adjustments.breadth_first_gap
            elif inputs.character_already_qualified:
                required += adjustments.character_already_qualified
            if current_score >= profile.thresholds.usable:
                required += adjustments.current_score_at_least_usable
        return min(
            policy.maximum_continue_chance,
            max(policy.minimum_continue_chance, required),
        )

    def _checkpoint_level(self, level: int) -> int:
        return max(self._policy.exploration_level, (level // 5 + 1) * 5)

    def _required_expected_gain(self, inputs: DecisionInputs) -> float:
        """Screen tiny possible upgrades by future cost, without charging sunk EXP."""
        policy = self._policy
        cost = upgrade_cost_to_level(inputs.level, self._checkpoint_level(inputs.level))
        factor = 1.0
        if inputs.embryo_supply is EmbryoSupply.SCARCE:
            factor = policy.scarce_gain_factor
        elif inputs.embryo_supply is EmbryoSupply.ABUNDANT:
            factor = policy.abundant_gain_factor
        return max(
            policy.minimum_expected_gain,
            policy.expected_gain_per_cost_reference
            * cost.experience
            / policy.cost_reference_experience
            * factor,
        )

    def _manual(
        self,
        breakdown: EchoScoreBreakdown,
        projection: ProjectionSummary,
        reason: str,
        reason_code: str,
        assumptions_version: str,
    ) -> UpgradeDecision:
        return UpgradeDecision(
            action=UpgradeAction.MANUAL_CONFIRMATION,
            current_score=breakdown.score,
            predicted_min_score=projection.minimum_score,
            predicted_max_score=projection.maximum_score,
            probabilities=dict(projection.probabilities),
            assumptions_version=assumptions_version,
            reason=reason,
            reason_codes=(reason_code,),
            requires_manual_confirmation=True,
        )

    def _combined_assumptions_version(self, projection: ProjectionSummary) -> str:
        return f"roll:{projection.assumptions_version}|decision:{self._policy.data_version}"


__all__ = ["UpgradeDecisionEngine"]
