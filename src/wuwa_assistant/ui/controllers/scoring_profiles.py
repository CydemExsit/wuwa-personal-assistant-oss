"""Non-blocking Qt presenter for editable, dataset-scoped scoring profiles."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from copy import deepcopy
from dataclasses import dataclass, replace
from enum import StrEnum
from threading import RLock
from typing import TypeVar

from PySide6.QtCore import QObject, Qt, Signal, Slot

from wuwa_assistant.application.scoring_profiles import (
    ScoreThresholds,
    ScoringProfileDefinition,
    ScoringProfileService,
    ScoringProfileSnapshot,
    StatWeightDefinition,
)
from wuwa_assistant.application.scoring_workflow import ScoringEvaluation
from wuwa_assistant.scoring import (
    ScoreBand,
    StatKey,
    UpgradeAction,
    canonical_stat_key,
    load_template_seed_definitions,
    unit_for_stat,
)
from wuwa_assistant.ui.controllers.background_executor import BackgroundExecutor
from wuwa_assistant.ui.pages.echo_live import ChoiceOption, EchoStatView, RecommendationView
from wuwa_assistant.ui.pages.scoring_profiles import (
    EchoScoreView,
    ProfileSummaryView,
    ScoreBreakdownItemView,
    ScoreDecisionPresentationView,
    ScoreThresholdsView,
    ScoringProfileEditRequest,
    ScoringProfilesPage,
    ScoringProfileView,
    StatWeightView,
    UpgradeDecisionView,
)
from wuwa_assistant.ui.panel_model import retention_advice

_T = TypeVar("_T")


class _Operation(StrEnum):
    INITIALIZE = "initialize"
    LOAD = "load"
    CREATE = "create"
    CLONE = "clone"
    SAVE = "save"
    DELETE = "delete"
    DEFAULT = "default"


@dataclass(frozen=True, slots=True)
class _Bundle:
    profiles: tuple[ScoringProfileSnapshot, ...]
    selected: ScoringProfileSnapshot | None


@dataclass(frozen=True, slots=True)
class _JobResult:
    operation: _Operation
    value: object | None = None
    error: str | None = None


class ScoringProfilesPresenter(QObject):
    """Run profile transactions away from Qt and publish immutable view DTOs."""

    profile_choices_changed = Signal(object)
    default_profile_changed = Signal(str)
    profile_changed = Signal(str)
    _job_completed = Signal(object)

    def __init__(
        self,
        *,
        service: ScoringProfileService,
        page: ScoringProfilesPage,
        data_set_id: str,
        template_loader: Callable[[], Sequence[Mapping[str, object]]] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._page = page
        self._data_set_id = data_set_id
        self._template_loader = template_loader or load_template_seed_definitions
        self._executor = BackgroundExecutor(max_workers=1, thread_name_prefix="wuwa-profile-db")
        self._lock = RLock()
        self._futures: set[Future[object]] = set()
        self._snapshots: dict[str, ScoringProfileSnapshot] = {}
        self._applying_bundle = False
        self._closing = False

        self._job_completed.connect(
            self._handle_result,
            Qt.ConnectionType.QueuedConnection,
        )
        self._page.profile_selected.connect(self.load_profile)
        self._page.create_requested.connect(self.create_profile)
        self._page.clone_requested.connect(self.clone_profile)
        self._page.save_requested.connect(self.save_profile)
        self._page.delete_requested.connect(self.delete_profile)
        self._page.default_requested.connect(self.set_default)

    def initialize(self) -> None:
        self._page.set_loading(True)

        def initialize_profiles() -> object:
            self._service.ensure_system_templates(self._template_loader())
            return self._bundle()

        self._submit(_Operation.INITIALIZE, initialize_profiles)

    def prepare_shutdown(self) -> None:
        """Quiesce UI callbacks and cancel queued reads without waiting."""

        self._closing = True
        self._executor.prepare_shutdown()

    def shutdown(self) -> bool:
        self.prepare_shutdown()
        return self.finish_shutdown()

    def finish_shutdown(self) -> bool:
        self._executor.shutdown()
        return True

    @Slot(str)
    def load_profile(self, profile_id: str) -> None:
        if not profile_id or self._closing or self._applying_bundle:
            return
        self._page.set_loading(True)
        self._submit(
            _Operation.LOAD,
            lambda: self._service.get_profile(profile_id, data_set_id=self._data_set_id),
        )

    @Slot()
    def create_profile(self) -> None:
        if self._closing:
            return
        self._page.set_loading(True, "正在建立可編輯設定檔…")

        def create() -> object:
            profiles = self._service.list_profiles(data_set_id=self._data_set_id)
            source = self._service.get_default(data_set_id=self._data_set_id)
            if source is None:
                source = profiles[0] if profiles else None
            if source is None:
                raise LookupError("沒有可複製的系統模板")
            name = _unique_name(f"{source.name} 自訂", profiles)
            created = self._service.clone_template(
                source.id,
                data_set_id=self._data_set_id,
                name=name,
            )
            return self._bundle(created.id)

        self._submit(_Operation.CREATE, create)

    @Slot(str)
    def clone_profile(self, profile_id: str) -> None:
        if not profile_id or self._closing:
            return
        self._page.set_loading(True, "正在複製設定檔…")

        def clone() -> object:
            source = self._service.get_profile(profile_id, data_set_id=self._data_set_id)
            profiles = self._service.list_profiles(data_set_id=self._data_set_id)
            created = self._service.clone_template(
                source.id,
                data_set_id=self._data_set_id,
                name=_unique_name(f"{source.name} 副本", profiles),
            )
            return self._bundle(created.id)

        self._submit(_Operation.CLONE, clone)

    @Slot(object)
    def save_profile(self, request: object) -> None:
        if not isinstance(request, ScoringProfileEditRequest) or self._closing:
            return
        self._page.set_loading(True, "正在儲存設定檔…")

        def save() -> object:
            current = self._service.get_profile(
                request.identifier,
                data_set_id=self._data_set_id,
            )
            if current.version != request.expected_version:
                raise RuntimeError("設定檔已被其他操作更新, 請重新載入後再儲存")
            definition = ScoringProfileDefinition(
                name=request.name,
                template_kind=request.template_kind,
                description=request.description,
                thresholds=ScoreThresholds(
                    usable=request.thresholds.usable,
                    passing=request.thresholds.passing,
                    perfect=request.thresholds.perfect,
                    exceptional=request.thresholds.exceptional,
                ),
                threshold_rules=current.threshold_rules,
                crit_balance_rule=deepcopy(current.crit_balance_rule),
                roll_data_version=current.roll_data_version,
            )
            weights = _weight_definitions(request.stat_weights, current)
            updated = self._service.update_user_profile(
                current.id,
                data_set_id=self._data_set_id,
                definition=definition,
                weights=weights,
            )
            return self._bundle(updated.id)

        self._submit(_Operation.SAVE, save)

    @Slot(str)
    def delete_profile(self, profile_id: str) -> None:
        if not profile_id or self._closing:
            return
        self._page.set_loading(True, "正在刪除本機設定檔…")

        def delete() -> object:
            self._service.delete_user_profile(profile_id, data_set_id=self._data_set_id)
            return self._bundle()

        self._submit(_Operation.DELETE, delete)

    @Slot(str)
    def set_default(self, profile_id: str) -> None:
        if not profile_id or self._closing:
            return
        self._page.set_loading(True, "正在更新預設設定檔…")

        def choose_default() -> object:
            selected = self._service.set_default(profile_id, data_set_id=self._data_set_id)
            return self._bundle(selected.id)

        self._submit(_Operation.DEFAULT, choose_default)

    def _bundle(self, selected_id: str | None = None) -> _Bundle:
        profiles = self._service.list_profiles(data_set_id=self._data_set_id)
        selected = (
            self._service.get_profile(selected_id, data_set_id=self._data_set_id)
            if selected_id is not None
            else self._service.get_default(data_set_id=self._data_set_id)
        )
        return _Bundle(profiles, selected)

    def _submit(self, operation: _Operation, function: Callable[[], _T]) -> None:
        if self._closing:
            return

        def execute() -> object:
            try:
                return _JobResult(operation, function())
            except Exception as exc:
                return _JobResult(operation, error=f"{type(exc).__name__}: {exc}")

        future = self._executor.submit(execute, read_only=operation is _Operation.LOAD)
        with self._lock:
            self._futures.add(future)
        future.add_done_callback(self._job_done)

    def _job_done(self, future: Future[object]) -> None:
        with self._lock:
            self._futures.discard(future)
        if self._closing:
            return
        self._job_completed.emit(future.result())

    @Slot(object)
    def _handle_result(self, value: object) -> None:
        if self._closing or not isinstance(value, _JobResult):
            return
        if value.error is not None:
            self._page.set_loading(False)
            self._page.show_error(value.error)
            return
        if isinstance(value.value, _Bundle):
            self._apply_bundle(value.value)
            action_messages = {
                _Operation.CREATE: "已建立可編輯設定檔。",
                _Operation.CLONE: "已複製設定檔。",
                _Operation.SAVE: "設定檔已儲存並可重新評分。",
                _Operation.DELETE: "設定檔已刪除。",
                _Operation.DEFAULT: "預設評分設定檔已更新。",
            }
            message = action_messages.get(value.operation)
            if message:
                self._page.show_status(message)
            if (
                value.operation in {_Operation.SAVE, _Operation.DEFAULT}
                and value.value.selected is not None
            ):
                self.profile_changed.emit(value.value.selected.id)
        elif isinstance(value.value, ScoringProfileSnapshot):
            self._snapshots[value.value.id] = value.value
            self._page.apply_profile(_profile_view(value.value))
        self._page.set_loading(False)

    def _apply_bundle(self, bundle: _Bundle) -> None:
        self._snapshots = {item.id: item for item in bundle.profiles}
        summaries = tuple(_summary_view(item) for item in bundle.profiles)
        self._applying_bundle = True
        try:
            self._page.set_profiles(summaries)
        finally:
            self._applying_bundle = False
        choices = tuple(ChoiceOption(item.id, item.name) for item in bundle.profiles)
        self.profile_choices_changed.emit(choices)
        if bundle.selected is not None:
            self._page.apply_profile(_profile_view(bundle.selected))
            if bundle.selected.is_default:
                self.default_profile_changed.emit(bundle.selected.id)
        elif bundle.profiles:
            self._page.apply_profile(_profile_view(bundle.profiles[0]))
        else:
            self._page.clear_profile()


_TEMPLATE_LABELS: Mapping[str, str] = {
    "generic_dps": "通用輸出型",
    "healing_hp": "治療/生命型",
    "defense": "防禦型",
    "energy_requirement": "共鳴效率需求型",
    "custom": "自訂",
}


def _summary_view(snapshot: ScoringProfileSnapshot) -> ProfileSummaryView:
    return ProfileSummaryView(
        identifier=snapshot.id,
        name=snapshot.name,
        template_label=_TEMPLATE_LABELS.get(
            snapshot.template_kind,
            "角色攻略參考"
            if snapshot.template_kind.startswith("character_guide_")
            else snapshot.template_kind,
        ),
        is_system_template=snapshot.is_system_template,
        is_default=snapshot.is_default,
    )


def _profile_view(snapshot: ScoringProfileSnapshot) -> ScoringProfileView:
    return ScoringProfileView(
        identifier=snapshot.id,
        name=snapshot.name,
        template_kind=snapshot.template_kind,
        template_label=_TEMPLATE_LABELS.get(
            snapshot.template_kind,
            "角色攻略參考"
            if snapshot.template_kind.startswith("character_guide_")
            else snapshot.template_kind,
        ),
        description=snapshot.description or "",
        thresholds=ScoreThresholdsView(
            usable=snapshot.thresholds.usable,
            passing=snapshot.thresholds.passing,
            perfect=snapshot.thresholds.perfect,
            exceptional=snapshot.thresholds.exceptional,
        ),
        stat_weights=tuple(
            StatWeightView(
                stat_name=item.stat_name,
                unit=item.unit,
                weight=item.weight,
                cap_value=item.cap_value,
                condition=item.condition_key,
            )
            for item in snapshot.weights
        ),
        is_system_template=snapshot.is_system_template,
        is_default=snapshot.is_default,
        version=snapshot.version,
    )


def _weight_definitions(
    rows: Sequence[StatWeightView],
    current: ScoringProfileSnapshot,
) -> tuple[StatWeightDefinition, ...]:
    if not any(item.condition == "always" and item.weight > 0 for item in rows):
        raise ValueError("設定檔至少需要一個正權重的基礎詞條")
    previous = {(item.stat_name, item.unit, item.condition_key): item for item in current.weights}
    base_weights = {
        (item.stat_name, item.unit): item.weight for item in rows if item.condition == "always"
    }
    definitions: list[StatWeightDefinition] = []
    for order, item in enumerate(rows):
        try:
            stat_key = StatKey(item.stat_name)
        except ValueError as exc:
            raise ValueError(f"未知的版本化詞條: {item.stat_name}") from exc
        if item.unit != unit_for_stat(stat_key):
            raise ValueError(f"詞條單位不正確: {item.stat_name}")
        if item.cap_value is not None and item.cap_value <= 0:
            raise ValueError(f"詞條上限必須大於 0: {item.stat_name}")
        original = previous.get((item.stat_name, item.unit, item.condition))
        condition = deepcopy(original.condition) if original is not None else None
        if item.condition != "always":
            base = base_weights.get((item.stat_name, item.unit))
            if base is None or base <= 0:
                raise ValueError(f"條件詞條缺少正權重基礎列: {item.stat_name}")
            condition = condition or {"operator": "equals", "value": True}
            condition["base_weight"] = base
            condition["multiplier"] = item.weight / base
        definitions.append(
            StatWeightDefinition(
                stat_name=item.stat_name,
                unit=item.unit,
                weight=item.weight,
                cap_value=item.cap_value,
                condition_key=item.condition,
                condition=condition,
                display_order=order,
            )
        )
    return tuple(definitions)


def _unique_name(
    base: str,
    profiles: Sequence[ScoringProfileSnapshot],
) -> str:
    names = {item.name for item in profiles}
    if base not in names:
        return base
    index = 2
    while f"{base} {index}" in names:
        index += 1
    return f"{base} {index}"


_STAT_LABELS: Mapping[StatKey, str] = {
    StatKey.CRIT_RATE: "暴擊",
    StatKey.CRIT_DAMAGE: "暴擊傷害",
    StatKey.ATTACK_PCT: "攻擊%",
    StatKey.FLAT_ATTACK: "固定攻擊",
    StatKey.HP_PCT: "生命%",
    StatKey.FLAT_HP: "固定生命",
    StatKey.DEFENSE_PCT: "防禦%",
    StatKey.FLAT_DEFENSE: "固定防禦",
    StatKey.ENERGY_REGEN: "共鳴效率",
    StatKey.BASIC_ATTACK_DAMAGE: "普攻傷害加成",
    StatKey.HEAVY_ATTACK_DAMAGE: "重擊傷害加成",
    StatKey.RESONANCE_SKILL_DAMAGE: "共鳴技能傷害加成",
    StatKey.RESONANCE_LIBERATION_DAMAGE: "共鳴解放傷害加成",
}

_BAND_LABELS: Mapping[ScoreBand | None, str] = {
    None: "需要人工確認",
    ScoreBand.TRANSITIONAL: "過渡",
    ScoreBand.USABLE: "可用",
    ScoreBand.PASSING: "及格",
    ScoreBand.PERFECT: "完美",
    ScoreBand.EXCEPTIONAL: "極品",
}

_ACTION_LABELS: Mapping[UpgradeAction, str] = {
    UpgradeAction.CONTINUE: "繼續到下一階段",
    UpgradeAction.STOP: "建議停止",
    UpgradeAction.QUALIFIED: "已達及格, 可升滿",
    UpgradeAction.CANNOT_SURPASS: "已無法合理超越現有裝備",
    UpgradeAction.MAXED: "已升滿",
    UpgradeAction.MANUAL_CONFIRMATION: "資料不足, 需要人工確認",
}


def evaluation_to_profile_preview(
    evaluation: ScoringEvaluation,
) -> ScoreDecisionPresentationView:
    """Map a pure evaluation into the settings-page preview DTO."""

    breakdown = evaluation.breakdown
    decision = evaluation.decision
    projection = evaluation.projection
    score_lines = tuple(
        ScoreBreakdownItemView(
            label=_STAT_LABELS.get(line.stat, line.stat.value),
            observed_value=f"{line.value:g}{line.unit}",
            roll_tier=line.roll_tier.label_zh_tw,
            weight=line.base_weight,
            contribution=_normalized_contribution(
                line.weighted_points,
                breakdown.maximum_weight_points,
                breakdown.earned_weight_points,
                breakdown.score,
            ),
            note="; ".join((*line.notes, f"OCR {line.confidence:.0%}")),
        )
        for line in breakdown.lines
    )
    score = EchoScoreView(
        score=breakdown.score,
        grade_label=_BAND_LABELS[breakdown.band],
        breakdown=score_lines,
        is_provisional=breakdown.requires_manual_confirmation,
    )
    score_range = (
        (decision.predicted_min_score, decision.predicted_max_score)
        if decision.predicted_min_score is not None and decision.predicted_max_score is not None
        else None
    )
    assumptions = tuple(
        dict.fromkeys(
            (
                f"Roll 資料版本: {decision.assumptions_version}",
                *projection.notes,
                *breakdown.notes,
            )
        )
    )
    decision_view = UpgradeDecisionView(
        action_label=_ACTION_LABELS[decision.action],
        reason=decision.reason
        + ("; " + "; ".join(evaluation.breakdown.notes) if evaluation.breakdown.notes else ""),
        projected_score_range=score_range,
        pass_probability=decision.probabilities.get("passing"),
        perfect_probability=decision.probabilities.get("perfect"),
        exceptional_probability=decision.probabilities.get("exceptional"),
        assumptions=assumptions,
    )
    return ScoreDecisionPresentationView(
        score=score,
        decision=decision_view,
        profile_name=evaluation.profile_name,
    )


def evaluation_to_echo_recommendation(
    evaluation: ScoringEvaluation,
    *,
    character_label: str | None = None,
    target_score: float | None = None,
    score_delta: float | None = None,
) -> RecommendationView:
    """Map a pure evaluation into the compact live-echo recommendation panel."""

    presentation = evaluation_to_profile_preview(evaluation)
    decision = presentation.decision
    score = presentation.score
    projection = evaluation.projection
    if (
        target_score is None
        and projection.improvement_target is not None
        and 0 <= projection.improvement_target <= 100
    ):
        target_score = projection.improvement_target
    if score_delta is None and score.score is not None and target_score is not None:
        score_delta = round(score.score - target_score, 2)
    target_probability = (
        projection.improvement_probability
        if target_score is not None
        and projection.improvement_target == target_score
        and not projection.requires_manual_confirmation
        and not evaluation.breakdown.requires_manual_confirmation
        else None
    )
    suggested_level = evaluation.decision.suggested_level
    conclusion = (
        f"可到 +{suggested_level} 再評估"
        if evaluation.decision.action is UpgradeAction.CONTINUE and suggested_level is not None
        else decision.action_label
    )
    return RecommendationView(
        conclusion=conclusion,
        upgrade_advice=conclusion,
        save_advice=retention_advice(
            str(evaluation.decision.action),
            qualified=evaluation.breakdown.band
            in {ScoreBand.PASSING, ScoreBand.PERFECT, ScoreBand.EXCEPTIONAL},
        ),
        opened_slot_quality_score=evaluation.breakdown.opened_slot_quality_score,
        score=score.score,
        expected_score=evaluation.projection.expected_score,
        estimated_score_range=decision.projected_score_range,
        pass_probability=decision.pass_probability,
        perfect_probability=decision.perfect_probability,
        exceptional_probability=decision.exceptional_probability,
        reason=decision.reason,
        breakdown=(
            *(
                f"{item.label} {item.observed_value} · {item.roll_tier} · +{item.contribution:.2f}"
                for item in score.breakdown
            ),
            *decision.assumptions,
        ),
        scoring_basis=(
            character_label.strip()
            if character_label and character_label.strip()
            else f"{evaluation.character_label}需求參考"
            if evaluation.character_label
            else "指定設定檔參考"
        ),
        scoring_profile_name=evaluation.profile_name,
        target_score=target_score,
        target_probability=target_probability,
        score_delta=score_delta,
    )


def evaluation_to_live_substats(
    evaluation: ScoringEvaluation,
    observed: Sequence[EchoStatView],
) -> tuple[EchoStatView, ...]:
    """Add Roll tier and role value without hiding rejected/unknown OCR rows."""

    lines = {line.stat: line for line in evaluation.breakdown.lines}
    keys = [canonical_stat_key(stat.name, stat.unit) for stat in observed]
    maximum_weight_points = evaluation.breakdown.maximum_weight_points
    earned_weight_points = evaluation.breakdown.earned_weight_points
    rendered: list[EchoStatView] = []
    for stat in observed:
        key = canonical_stat_key(stat.name, stat.unit)
        line = lines.get(key) if key is not None and keys.count(key) == 1 else None
        contribution = (
            _normalized_contribution(
                line.weighted_points,
                maximum_weight_points,
                earned_weight_points,
                evaluation.breakdown.score,
            )
            if line is not None
            else 0.0
        )
        rendered.append(
            EchoStatView(
                name=stat.name,
                value=stat.value,
                unit=stat.unit,
                confidence=stat.confidence,
                roll_tier=line.roll_tier.label_zh_tw if line is not None else None,
                character_value=(
                    f"權重 {line.base_weight:.2f} · {contribution:+.2f} 分"
                    if line is not None
                    else "需要人工確認"
                ),
                score_contribution=contribution if line is not None else None,
            )
        )
    score = evaluation.breakdown.score
    if score is not None:
        points = [round(row.score_contribution or 0, 2) for row in rendered]
        nonzero = [index for index, value in enumerate(points) if value > 0]
        if nonzero:
            last = nonzero[-1]
            points[last] = round(points[last] + score - sum(points), 2)
        rendered = [
            replace(row, score_contribution=points[index])
            if row.score_contribution is not None
            else row
            for index, row in enumerate(rendered)
        ]
    return tuple(rendered)


def _normalized_contribution(
    weighted_points: float,
    maximum_weight_points: float,
    earned_weight_points: float,
    score: float | None = None,
) -> float:
    """Return one line's share on the same capped 0-100 scale as the score."""

    if score is not None:
        return weighted_points / earned_weight_points * score if earned_weight_points > 0 else 0.0
    if maximum_weight_points <= 0:
        return 0.0
    denominator = max(maximum_weight_points, earned_weight_points)
    return weighted_points / denominator * 100.0


__all__ = [
    "ScoringProfilesPresenter",
    "evaluation_to_echo_recommendation",
    "evaluation_to_live_substats",
    "evaluation_to_profile_preview",
]
