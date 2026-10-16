"""Read-only comparison against freshly loaded character equipment."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, replace

from wuwa_assistant.scoring import DecisionInputs, ScoringContext, UpgradeAction
from wuwa_assistant.scoring.character_guides import AUTO_PROFILE_ID, guide_for_character
from wuwa_assistant.scoring.upgrade_costs import next_node_cost, upgrade_cost_to_level
from wuwa_assistant.storage.engine import SessionFactory

from .character_completion import _main_stat_validity, _set_validity
from .character_scoring import character_context, character_profile
from .characters import CharacterCultivationService, EchoSlotSnapshot
from .equipment_analysis import guide_fit
from .equipment_overview import EquipmentOverviewService, EquippedEchoScore
from .scoring_profiles import ScoringProfileService
from .scoring_workflow import ScoringEvaluation, ScoringWorkflowService


@dataclass(frozen=True, slots=True)
class ReplacementTarget:
    character_id: str
    character_name: str
    slot_index: int
    piece: EquippedEchoScore
    profile_id: str | None
    profile_name: str
    context: ScoringContext
    description: str


@dataclass(frozen=True, slots=True)
class ReplacementComparison:
    target: ReplacementTarget
    evaluation: ScoringEvaluation | None
    conclusion: str
    reason: str
    delta: float | None = None
    target_comparable: bool = False
    is_same_echo: bool = False


class ReplacementTrainingService:
    """A target is a comparison reference, never an equipment assignment."""

    def __init__(self, factory: SessionFactory) -> None:
        self._characters = CharacterCultivationService(factory)
        self._overview = EquipmentOverviewService(factory)
        self._profiles = ScoringProfileService(factory)
        self._scoring = ScoringWorkflowService(factory, self._profiles)

    @property
    def minimum_confidence(self) -> float:
        return self._scoring.minimum_confidence

    def target(self, data_set_id: str, character_id: str, slot_index: int) -> ReplacementTarget:
        if slot_index not in range(1, 6):
            raise ValueError("invalid equipment position")
        detail = self._characters.get_character(character_id, data_set_id=data_set_id)
        character = next(
            (
                c
                for c in self._overview.get_overview(
                    data_set_id=data_set_id, profile_id=AUTO_PROFILE_ID
                ).characters
                if c.character_id == character_id
            ),
            None,
        )
        if character is None:
            raise ValueError("角色已封存或不存在")
        piece = character.slots[slot_index - 1]
        snapshot = character_profile(
            self._profiles,
            data_set_id=data_set_id,
            name=character.character_name,
            explicit_profile_id=detail.summary.scoring_profile_id,
        )
        advice = piece.training_recommendation
        score = "資料待補" if piece.score is None else f"{piece.score:.2f} 分"
        description = (
            f"{character.character_name} · 位置 {slot_index}"
            f" · 現有 {piece.name or '未登記'} · {score}"
        )
        if piece.cost is not None:
            description += f" · COST {piece.cost}"
        if advice is not None:
            description += "\n套裝: " + (" / ".join(advice.target_sets) or "依自訂配裝")
            description += "\n主詞條: " + (" / ".join(advice.target_main_stats) or "依目標位置確認")
            description += "\n副詞條優先: " + ("、".join(advice.target_substats) or "尚無角色設定")
        description += "\n沿用目前角色需求與面板效率; 換裝後若效率改變請更新面板。"
        return ReplacementTarget(
            character_id,
            character.character_name,
            slot_index,
            piece,
            None if snapshot is None else snapshot.id,
            "尚無角色設定" if snapshot is None else snapshot.name,
            character_context(detail.cultivation_goal),
            description,
        )

    def evaluate(
        self,
        payload: Mapping[str, object],
        *,
        data_set_id: str,
        character_id: str,
        slot_index: int,
        requires_review: bool = False,
        candidate_echo_id: str | None = None,
    ) -> ReplacementComparison:
        target = self.target(data_set_id, character_id, slot_index)
        is_same_echo = bool(candidate_echo_id and candidate_echo_id == target.piece.echo_id)
        if target.profile_id is None:
            return ReplacementComparison(
                target,
                None,
                "尚無角色評分依據",
                "先選擇此角色的評分設定",
                is_same_echo=is_same_echo,
            )
        level = payload.get("level")
        baseline = target.piece.score
        if is_same_echo:
            baseline = None
        if target.piece.set_is_valid is False or target.piece.main_stat_is_valid is False:
            baseline = None  # Wrong-fit equipment must not prevent improving the build.
        evaluation = self._scoring.evaluate(
            payload,
            data_set_id=data_set_id,
            profile_id=target.profile_id,
            context=target.context,
            requires_review=requires_review,
            decision_inputs=DecisionInputs(
                level=level if type(level) is int else -1,
                existing_gear_score=baseline,
                breadth_first_progression=baseline is None,
            ),
            persist=False,
        )
        evaluation = replace(evaluation, character_label=target.character_name)
        score = evaluation.breakdown.score
        delta = (
            None
            if score is None or target.piece.score is None
            else round(score - target.piece.score, 2)
        )
        detail = self._characters.get_character(character_id, data_set_id=data_set_id)
        raw_mains = payload.get("main_stats")
        mains = (
            tuple(s for s in raw_mains if isinstance(s, dict))
            if isinstance(raw_mains, list)
            else ()
        )
        cost = payload.get("cost")
        cost = cost if type(cost) is int else None
        set_name = payload.get("set_name")
        set_name = set_name if isinstance(set_name, str) else None
        candidate = EchoSlotSnapshot(
            slot_index=slot_index,
            echo_id=target.piece.echo_id if is_same_echo else None,
            name=None,
            set_name=set_name,
            cost=cost,
            level=level if type(level) is int else None,
            score=score,
            final_status=None,
            main_stats=mains,
            sub_stats=(),
        )
        guide = guide_for_character(target.character_name)
        set_fit, main_fit, _, _ = guide_fit(
            guide,
            cost=cost,
            set_name=set_name,
            main_stats=mains,
        )
        custom_set = _set_validity(candidate, detail.cultivation_goal)
        custom_main = _main_stat_validity(candidate, detail.cultivation_goal)
        if custom_set is not None:
            set_fit = custom_set
        if custom_main is not None:
            main_fit = custom_main
        custom_set_plan = bool(detail.cultivation_goal.get("echo_set_targets")) or all(
            _set_validity(s, detail.cultivation_goal) is True for s in detail.echo_slots
        )
        remaining = [
            s.set_name for s in detail.echo_slots if s.echo_id and s.slot_index != slot_index
        ]
        if guide is not None and not custom_set_plan:
            before = Counter(s.set_name for s in detail.echo_slots if s.echo_id and s.set_name)
            after = Counter(name for name in (*remaining, set_name) if name)
            breaks_set = guide.sets_complete(before) and not guide.sets_complete(after)
        else:
            breaks_set = (
                not custom_set_plan
                and len(remaining) == 4
                and all(s == target.piece.set_name for s in remaining)
                and set_name != target.piece.set_name
            )
        unrolled = (
            type(level) is int
            and 0 <= level < 5
            and not payload.get("sub_stats")
            and not requires_review
        )
        if requires_review or (
            not unrolled and (evaluation.breakdown.requires_manual_confirmation or score is None)
        ):
            conclusion, reason = "資料待補", "先確認聲骸的辨識內容, 再評估"
        elif cost is None or set_name is None:
            conclusion, reason = "配裝資料待補", "先辨識套裝與 COST, 再判斷是否適合這個位置"
        elif target.piece.cost is not None and cost != target.piece.cost:
            conclusion, reason = "COST 不同", "此位置的替換需先重配整套 COST, 暫不判定能否直接替換"
        elif set_fit is False or main_fit is False or breaks_set:
            conclusion, reason = (
                "先檢查配裝",
                "套裝或主詞條不符參考, 或替換會拆開現有套裝組合; 高分不代表可直接替換",
            )
        elif set_fit is None or main_fit is None:
            conclusion, reason = "配裝資料待補", "已有副詞條評分, 但套裝或主詞條尚無判定依據"
        elif is_same_echo:
            decision = evaluation.decision
            if unrolled:
                conclusion, reason = "可先培養至 +15", "尚未調諧; 先確認三條副詞條再評估。"
            else:
                conclusion = {
                    UpgradeAction.QUALIFIED: "建議升至 +25 並保留",
                    UpgradeAction.MAXED: "已滿級, 可保留",
                    UpgradeAction.CONTINUE: (
                        f"可先培養至 +{decision.suggested_level}"
                        if decision.suggested_level is not None
                        else "可繼續強化並評估"
                    ),
                    UpgradeAction.STOP: "建議停止追加強化",
                    UpgradeAction.MANUAL_CONFIRMATION: "資料待補",
                }.get(decision.action, "強化參考")
                reason = decision.reason
            reason = "目前辨識的是此位置已裝備的同一顆聲骸, 依自身品質評估強化。" + reason
        elif unrolled:
            if baseline is not None and baseline >= 100:
                conclusion, reason = (
                    "無法超過現有聲骸",
                    "現有裝備已達此評分模型上限, 不建議為分數替換投入這顆胚子",
                )
            else:
                conclusion, reason = (
                    "可先培養至 +15",
                    "胚子符合此位置方向; 先確認三條副詞條再評估。"
                    "尚未調諧, 不預估完成分數或超越機率",
                )
        elif level == 25:
            if baseline is None:
                conclusion, reason = (
                    "已滿級, 檢查替換",
                    "新件符合配裝方向; 現有裝備尚無適用的分數基準",
                )
            elif score is not None and score > baseline:
                if target.piece.level is not None and target.piece.level < 25:
                    conclusion, reason = (
                        "現有聲骸尚有強化空間",
                        f"新件目前高 {score - baseline:.2f} 分, "
                        f"但現有 +{target.piece.level} 還能開詞條; 先確認其潛力再決定替換",
                    )
                else:
                    conclusion, reason = (
                        "可考慮替換",
                        f"同角色標準下比現有聲骸高 {score - baseline:.2f} 分",
                    )
            else:
                conclusion, reason = "保留現有聲骸", "新件已滿級, 副詞條分數沒有超過現有裝備"
        elif baseline is not None and not evaluation.projection.requires_manual_confirmation:
            upper = evaluation.projection.maximum_score
            if upper is None:
                conclusion, reason = "資料待補", "尚無可用的剩餘詞條預測"
            elif upper <= baseline:
                conclusion, reason = (
                    "無法超過現有聲骸",
                    f"此模型的剩餘上限 {upper:.2f} 分未超過現有 {baseline:.2f} 分",
                )
            elif evaluation.decision.requires_manual_confirmation:
                conclusion, reason = "資料待補", evaluation.decision.reason
            elif score is not None and score > baseline:
                conclusion, reason = (
                    "已超過現有, 建議停止投入"
                    if evaluation.decision.action is UpgradeAction.STOP
                    else "已超過現有分數",
                    f"目前已高 {score - baseline:.2f} 分。" + evaluation.decision.reason,
                )
            else:
                conclusion, reason = (
                    (
                        f"可先培養至 +{evaluation.decision.suggested_level}"
                        if evaluation.decision.suggested_level is not None
                        else "下一節點繼續比較"
                    )
                    if str(evaluation.decision.action) == "continue"
                    else "建議停止投入",
                    evaluation.decision.reason,
                )
            if target.piece.level is not None and target.piece.level < 25:
                reason += f"; 現有 +{target.piece.level} 也尚未滿級, 請一併考慮其潛力"
        else:
            conclusion = {
                "continue": (
                    f"可先培養至 +{evaluation.decision.suggested_level}"
                    if evaluation.decision.suggested_level is not None
                    else "可繼續觀察"
                ),
                "stop": "建議停止投入",
                "qualified_finish": "已達門檻",
                "cannot_surpass_existing": "無法超過現有聲骸",
                "manual_confirmation": "資料待補",
            }.get(str(evaluation.decision.action), "強化參考")
            reason = evaluation.decision.reason
        if type(level) is int and 0 <= level < 25:
            suggested_level = 15 if unrolled else evaluation.decision.suggested_level
            node_cost = (
                upgrade_cost_to_level(level, suggested_level)
                if suggested_level is not None
                else next_node_cost(level)
            )
            if node_cost is not None and "最多尚需" not in reason:
                reason += "\n" + node_cost.explanation
        reason = reason.rstrip("。") + "。評分比較副詞條品質, 不等於實際傷害提升。"
        comparable = (
            not is_same_echo
            and baseline is not None
            and not requires_review
            and not evaluation.breakdown.requires_manual_confirmation
            and not evaluation.projection.requires_manual_confirmation
            and score is not None
            and cost is not None
            and target.piece.cost == cost
            and set_fit is True
            and main_fit is True
            and not breaks_set
        )
        return ReplacementComparison(
            target,
            evaluation,
            conclusion,
            reason,
            delta if comparable else None,
            comparable,
            is_same_echo,
        )
