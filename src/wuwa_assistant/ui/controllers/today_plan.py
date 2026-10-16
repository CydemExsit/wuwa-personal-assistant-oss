"""Non-blocking presenter for account-wide cultivation and daily planning."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, cast

from PySide6.QtCore import QObject, Qt, Signal, Slot

from wuwa_assistant.application.character_completion import evaluate_character_detail
from wuwa_assistant.application.characters import (
    CharacterCultivationService,
    CharacterDetail,
    CharacterSummary,
)
from wuwa_assistant.application.farming import build_today_farming
from wuwa_assistant.application.planning import (
    CultivationPlanningService,
    CultivationTargetDefinition,
    CultivationTargetSnapshot,
    DailyPlanItemDefinition,
    DailyPlanSnapshot,
    InventoryCountSnapshot,
    MaterialSnapshot,
)
from wuwa_assistant.application.planning import (
    MaterialDefinition as StoredMaterialDefinition,
)
from wuwa_assistant.application.planning import (
    MaterialRequirement as StoredMaterialRequirement,
)
from wuwa_assistant.application.scoring_profiles import ScoringProfileService
from wuwa_assistant.cultivation import BlockerCode, CultivationCompletion
from wuwa_assistant.planning import (
    CharacterPlanningState,
    CultivationTaskProgress,
    DailyActionStatus,
    FunctionalThreshold,
    InventoryCount,
    MaterialDefinition,
    MaterialRequirement,
    PlanningEchoSlot,
    ProgressionAction,
    ResourcePlanningTarget,
    StaticDataSourceStatus,
    ThresholdDirection,
    WeeklyMaterialUsage,
    build_breadth_first_queue,
    build_daily_plan,
)
from wuwa_assistant.planning import (
    DailyPlanRequest as DomainDailyPlanRequest,
)
from wuwa_assistant.storage.settings import SettingsService
from wuwa_assistant.ui.controllers.background_executor import BackgroundExecutor
from wuwa_assistant.ui.dialogs.planning_data import (
    CultivationTargetSaveRequest,
    CultivationTargetView,
    MaterialSaveRequest,
    MaterialView,
    PlanningCharacterChoiceView,
    PlanningDataBundle,
    PlanningDataDialog,
    RequiredMaterialView,
)
from wuwa_assistant.ui.pages.today_plan import (
    CharacterTargetSummaryView,
    InventoryEditRequest,
    InventoryItemView,
    PlanRecommendationView,
    TodayPlanPage,
    TodayPlanRequest,
    TodayPlanSnapshot,
    WeeklyShortageView,
)

DAILY_WAVEPLATES_KEY = "planning/daily_waveplates"
DEFAULT_DAILY_WAVEPLATES = 240


class _Operation(StrEnum):
    LOAD = "load"
    GENERATE = "generate"
    INVENTORY = "inventory"
    COMPLETE = "complete"


class _DialogOperation(StrEnum):
    LOAD = "load"
    MATERIAL_SAVE = "material_save"
    MATERIAL_DELETE = "material_delete"
    TARGET_SAVE = "target_save"
    TARGET_ARCHIVE = "target_archive"


@dataclass(frozen=True, slots=True)
class _Bundle:
    snapshot: TodayPlanSnapshot
    dashboard_priority: str


@dataclass(frozen=True, slots=True)
class _JobResult:
    operation: _Operation
    request_id: int
    bundle: _Bundle | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _DialogResult:
    operation: _DialogOperation
    bundle: PlanningDataBundle | None = None
    selected_material_id: str | None = None
    selected_target_id: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _CharacterEvaluation:
    detail: CharacterDetail
    completion: CultivationCompletion
    next_action: str
    passing_score: float


class TodayPlanPresenter(QObject):
    """Coordinate local persistence and pure planning away from the Qt thread."""

    dashboard_priority_changed = Signal(str)
    _job_completed = Signal(object)
    _dialog_completed = Signal(object)

    def __init__(
        self,
        *,
        planning_service: CultivationPlanningService,
        character_service: CharacterCultivationService,
        scoring_profiles: ScoringProfileService,
        settings: SettingsService,
        page: TodayPlanPage,
        data_set_id: str,
        today: Callable[[], date] = date.today,
        activity_notices: Callable[[date], tuple[str, ...]] | None = None,
        tacet_cache_directory: Path | None = None,
        legacy_resource_planning: bool = False,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._planning = planning_service
        self._characters = character_service
        self._profiles = scoring_profiles
        self._settings = settings
        self._page = page
        self._data_set_id = data_set_id
        self._today = today
        self._activity_notices = activity_notices
        self._tacet_cache_directory = tacet_cache_directory
        self._legacy_resource_planning = legacy_resource_planning
        self._executor = BackgroundExecutor(max_workers=1, thread_name_prefix="wuwa-planner-db")
        self._lock = RLock()
        self._futures: set[Future[object]] = set()
        self._data_dialog: PlanningDataDialog | None = None
        self._closing = False
        self._latest_request_id = 0

        self._job_completed.connect(self._handle_result, Qt.ConnectionType.QueuedConnection)
        self._dialog_completed.connect(
            self._handle_dialog_result,
            Qt.ConnectionType.QueuedConnection,
        )
        page.plan_requested.connect(self.generate_plan)
        page.inventory_edit_requested.connect(self.update_inventory)
        page.manage_data_requested.connect(self.open_data_editor)
        page.complete_plan_requested.connect(self.complete_plan)

    def initialize(self) -> None:
        if self._closing:
            return
        self._page.set_loading(True, "正在載入本機養成資料…")
        self._submit(
            _Operation.LOAD,
            lambda: self._load_initial_bundle(self._today(), self._saved_waveplates()),
        )

    def prepare_shutdown(self) -> None:
        """Quiesce UI callbacks and cancel queued reads without waiting."""

        self._closing = True
        self._executor.prepare_shutdown()

    def shutdown(self) -> bool:
        self.prepare_shutdown()
        return self.finish_shutdown()

    def finish_shutdown(self) -> bool:
        self._executor.shutdown()
        with self._lock:
            self._futures.clear()
        return True

    @Slot(object)
    def generate_plan(self, payload: object) -> None:
        if self._closing or not isinstance(payload, TodayPlanRequest):
            return
        self._page.set_loading(True, "正在依目前資料更新養成規劃…")

        def work() -> _Bundle:
            self._settings.set(DAILY_WAVEPLATES_KEY, payload.available_waveplates)
            return self._build_bundle(
                payload.plan_date if self._legacy_resource_planning else self._today(),
                payload.available_waveplates,
                persist=True,
            )

        self._submit(_Operation.GENERATE, work)

    @Slot(object)
    def update_inventory(self, payload: object) -> None:
        if self._closing or not isinstance(payload, InventoryEditRequest):
            return
        request = self._page.current_request()
        self._page.set_loading(True, "正在保存本機素材庫存並重新規劃…")

        def work() -> _Bundle:
            self._planning.set_inventory_count(
                payload.material_id,
                payload.count,
                data_set_id=self._data_set_id,
            )
            return self._build_bundle(
                request.plan_date,
                request.available_waveplates,
                persist=True,
            )

        self._submit(_Operation.INVENTORY, work)

    def refresh(self) -> None:
        if self._closing:
            return
        request = self._page.current_request()
        self._page.set_loading(True, "正在重新載入養成資料…")
        self._submit(
            _Operation.LOAD,
            lambda: self._build_bundle(
                request.plan_date if self._legacy_resource_planning else self._today(),
                request.available_waveplates,
                persist=False,
            ),
        )

    @Slot(str)
    def complete_plan(self, generation_id: str) -> None:
        if self._closing or not generation_id.strip():
            return
        request = self._page.current_request()
        self._page.set_loading(True, "正在標記今日規劃已執行…")

        def work() -> _Bundle:
            current = self._planning.get_daily_plan(generation_id, data_set_id=self._data_set_id)
            if current.is_archived or current.plan_date != request.plan_date:
                raise ValueError("畫面上的規劃已不是目前版本, 請重新規劃")
            self._planning.set_daily_plan_status(
                generation_id,
                "completed",
                data_set_id=self._data_set_id,
            )
            return self._build_bundle(
                request.plan_date,
                request.available_waveplates,
                persist=False,
            )

        self._submit(_Operation.COMPLETE, work)

    @Slot()
    def open_data_editor(self) -> None:
        if self._closing:
            return
        dialog = self._ensure_data_dialog()
        dialog.set_loading(True, "正在載入素材與養成目標…")
        dialog.show()
        dialog.raise_()
        self._submit_dialog(_DialogOperation.LOAD, self._load_data_bundle)

    def _ensure_data_dialog(self) -> PlanningDataDialog:
        if self._data_dialog is None:
            dialog = PlanningDataDialog(self._page)
            dialog.material_save_requested.connect(self._save_material)
            dialog.material_archive_requested.connect(self._delete_material)
            dialog.target_save_requested.connect(self._save_target)
            dialog.target_archive_requested.connect(self._archive_target)
            self._data_dialog = dialog
        return self._data_dialog

    @Slot(object)
    def _save_material(self, payload: object) -> None:
        if self._closing or not isinstance(payload, MaterialSaveRequest):
            return
        dialog = self._ensure_data_dialog()
        dialog.set_loading(True, "正在以單一 transaction 儲存素材與庫存…")

        def work() -> _DialogResult:
            stored, _inventory = self._planning.save_material_with_inventory(
                data_set_id=self._data_set_id,
                material_id=payload.identifier,
                definition=StoredMaterialDefinition(
                    code=payload.code,
                    name=payload.name,
                    category=payload.category,
                    stamina_cost_per_run=payload.waveplate_cost_per_run or None,
                    average_yield=payload.average_output or None,
                    open_days=payload.open_weekdays,
                    weekly_limit=payload.weekly_limit,
                    source_status=payload.source_status,
                    source_version=payload.version,
                    notes=payload.notes or None,
                ),
                inventory_quantity=payload.inventory_count,
                clear_inventory=payload.inventory_count is None,
            )
            return _DialogResult(
                _DialogOperation.MATERIAL_SAVE,
                bundle=self._load_data_bundle(),
                selected_material_id=stored.id,
            )

        self._submit_dialog(_DialogOperation.MATERIAL_SAVE, work)

    @Slot(str)
    def _delete_material(self, material_id: str) -> None:
        if self._closing:
            return
        dialog = self._ensure_data_dialog()
        dialog.set_loading(True, "正在刪除未使用的素材…")

        def work() -> _DialogResult:
            self._planning.delete_material(material_id, data_set_id=self._data_set_id)
            return _DialogResult(
                _DialogOperation.MATERIAL_DELETE,
                bundle=self._load_data_bundle(),
            )

        self._submit_dialog(_DialogOperation.MATERIAL_DELETE, work)

    @Slot(object)
    def _save_target(self, payload: object) -> None:
        if self._closing or not isinstance(payload, CultivationTargetSaveRequest):
            return
        dialog = self._ensure_data_dialog()
        dialog.set_loading(True, "正在儲存養成目標與素材需求…")

        def work() -> _DialogResult:
            definition = CultivationTargetDefinition(
                character_id=payload.character_id,
                target_type=payload.target_type,
                target_key=payload.target_key,
                current_value=_parse_manual_value(payload.current_value, allow_blank=True),
                target_value=_parse_manual_value(payload.target_value, allow_blank=False),
                priority=payload.priority,
                is_completed=payload.completed,
                due_date=payload.due_date,
                notes=payload.notes or None,
                required_materials=tuple(
                    StoredMaterialRequirement(item.material_id, item.quantity)
                    for item in payload.required_materials
                ),
            )
            if payload.identifier is None:
                saved = self._planning.create_cultivation_target(
                    data_set_id=self._data_set_id,
                    definition=definition,
                )
            else:
                saved = self._planning.update_cultivation_target(
                    payload.identifier,
                    data_set_id=self._data_set_id,
                    definition=definition,
                )
            return _DialogResult(
                _DialogOperation.TARGET_SAVE,
                bundle=self._load_data_bundle(),
                selected_target_id=saved.id,
            )

        self._submit_dialog(_DialogOperation.TARGET_SAVE, work)

    @Slot(str)
    def _archive_target(self, target_id: str) -> None:
        if self._closing:
            return
        dialog = self._ensure_data_dialog()
        dialog.set_loading(True, "正在完成並封存養成目標…")

        def work() -> _DialogResult:
            self._planning.set_cultivation_target_completed(
                target_id,
                True,
                data_set_id=self._data_set_id,
            )
            return _DialogResult(
                _DialogOperation.TARGET_ARCHIVE,
                bundle=self._load_data_bundle(),
                selected_target_id=target_id,
            )

        self._submit_dialog(_DialogOperation.TARGET_ARCHIVE, work)

    def _saved_waveplates(self) -> int:
        value = self._settings.get(DAILY_WAVEPLATES_KEY, DEFAULT_DAILY_WAVEPLATES)
        if type(value) is int and 0 <= value <= 9_999:
            return value
        return DEFAULT_DAILY_WAVEPLATES

    def _load_data_bundle(self) -> PlanningDataBundle:
        materials = self._planning.list_materials(data_set_id=self._data_set_id)
        inventory = self._planning.list_inventory_counts(data_set_id=self._data_set_id)
        targets = self._planning.list_cultivation_targets(
            data_set_id=self._data_set_id,
            include_completed=True,
        )
        characters = self._characters.list_characters(
            data_set_id=self._data_set_id,
            include_archived=True,
        )
        count_by_material = {item.material_id: item.quantity for item in inventory}
        material_by_id = {item.id: item for item in materials}
        return PlanningDataBundle(
            materials=tuple(
                MaterialView(
                    identifier=item.id,
                    code=item.code,
                    name=item.name,
                    category=item.category,
                    waveplate_cost_per_run=item.stamina_cost_per_run or 0,
                    average_output=item.average_yield or 0.0,
                    open_weekdays=item.open_days,
                    weekly_limit=item.weekly_limit,
                    source_status=item.source_status,
                    version=item.source_version,
                    inventory_count=(
                        None
                        if item.id not in count_by_material
                        else _view_quantity(count_by_material[item.id])
                    ),
                    notes=item.notes or "",
                )
                for item in materials
            ),
            characters=tuple(
                PlanningCharacterChoiceView(item.id, _planning_character_label(item))
                for item in characters
            ),
            targets=tuple(
                CultivationTargetView(
                    identifier=item.id,
                    character_id=item.character_id,
                    character_name=item.character_name,
                    target_type=item.target_type,
                    target_key=item.target_key,
                    current_value=_manual_value_text(item.current_value),
                    target_value=_manual_value_text(item.target_value),
                    priority=item.priority,
                    completed=item.is_completed,
                    due_date=item.due_date,
                    notes=item.notes or "",
                    required_materials=tuple(
                        RequiredMaterialView(
                            material_id=requirement.material_id,
                            material_name=material_by_id[requirement.material_id].name,
                            quantity=_view_quantity(requirement.required_quantity),
                        )
                        for requirement in item.required_materials
                    ),
                )
                for item in targets
            ),
        )

    def _build_bundle(
        self,
        plan_date: date,
        waveplates: int,
        *,
        persist: bool,
        stored_plan: DailyPlanSnapshot | None = None,
    ) -> _Bundle:
        farming = build_today_farming(
            self._characters,
            self._profiles,
            data_set_id=self._data_set_id,
            tacet_cache_directory=self._tacet_cache_directory,
            as_of=plan_date,
        )
        candidates = tuple(row for row in farming.farming.recommendations if row.points > 0)
        dashboard = (
            f"技能待升級 {len(farming.skills)} 項 · 無音區: {candidates[0].zone.name}"
            if candidates
            else f"技能待升級 {len(farming.skills)} 項 · 無音區暫無已確認缺口"
        )
        activity_notices = (
            None if self._activity_notices is None else self._activity_notices(plan_date)
        )
        if not self._legacy_resource_planning:
            return _Bundle(
                TodayPlanSnapshot(
                    plan_date=plan_date,
                    available_waveplates=waveplates,
                    farming=farming,
                    activity_notices=activity_notices,
                ),
                dashboard,
            )
        materials = self._planning.list_materials(data_set_id=self._data_set_id)
        inventory = self._planning.list_inventory_counts(data_set_id=self._data_set_id)
        targets = self._planning.list_cultivation_targets(
            data_set_id=self._data_set_id,
            include_completed=True,
        )
        evaluations = self._character_evaluations(targets)
        queue = build_breadth_first_queue(
            tuple(_planning_state(item, targets) for item in evaluations)
        )
        target_rank = {
            action.cultivation_target_id: action.rank
            for action in queue
            if action.cultivation_target_id is not None
        }
        domain_targets = tuple(
            _resource_target(target, target_rank.get(target.id)) for target in targets
        )
        domain_materials = tuple(_domain_material(item) for item in materials)
        domain_inventory = tuple(
            InventoryCount(item.material_id, _decimal(item.quantity)) for item in inventory
        )
        requirements = tuple(
            MaterialRequirement(
                target.id, requirement.material_id, _decimal(requirement.required_quantity)
            )
            for target in targets
            for requirement in target.required_materials
        )
        result = build_daily_plan(
            DomainDailyPlanRequest(
                plan_date=plan_date,
                daily_waveplates=waveplates,
                targets=domain_targets,
                materials=domain_materials,
                requirements=requirements,
                inventory=domain_inventory,
                weekly_usage=self._weekly_usage(plan_date),
            )
        )
        recommendations = (
            _stored_recommendations(stored_plan, materials, targets)
            if stored_plan is not None
            else tuple(_recommendation(action) for action in result.actions)
        )
        if stored_plan is None and not recommendations:
            recommendations = tuple(_queue_recommendation(action) for action in queue[:2])
        plan_generation_id = (
            stored_plan.generation_id
            if stored_plan is not None
            and not stored_plan.is_archived
            and any(item.status == "planned" for item in stored_plan.items)
            else None
        )
        if persist:
            if recommendations:
                items = _plan_items(
                    result.actions,
                    queue,
                    result.assumptions,
                    waveplates,
                    {item.id: item for item in materials},
                )
                stored_plan = self._planning.replace_daily_plan(
                    data_set_id=self._data_set_id,
                    plan_date=plan_date,
                    items=items,
                )
                plan_generation_id = stored_plan.generation_id
            else:
                current = self._planning.get_current_daily_plan(
                    plan_date,
                    data_set_id=self._data_set_id,
                )
                if current is not None:
                    self._planning.archive_daily_plan(
                        current.generation_id,
                        data_set_id=self._data_set_id,
                    )
        snapshot = TodayPlanSnapshot(
            plan_date=plan_date,
            available_waveplates=waveplates,
            first_priority=recommendations[0] if recommendations else None,
            second_priority=recommendations[1] if len(recommendations) > 1 else None,
            weekly_shortages=_shortage_views(materials, inventory, targets, result),
            character_summaries=tuple(
                CharacterTargetSummaryView(
                    character_id=item.detail.summary.id,
                    character_name=item.detail.summary.name,
                    completion_percent=item.completion.overall_percent,
                    target_reached=item.completion.at_goal,
                    next_step=item.next_action,
                )
                for item in evaluations
            ),
            inventory=_inventory_views(materials, inventory),
            weekly_summary=_weekly_summary(result.weekly_shortages, result.notices),
            plan_generation_id=plan_generation_id,
            farming=farming,
            activity_notices=activity_notices,
        )
        return _Bundle(snapshot, dashboard)

    def _load_initial_bundle(self, plan_date: date, waveplates: int) -> _Bundle:
        if not self._legacy_resource_planning:
            return self._build_bundle(plan_date, waveplates, persist=False)
        current = self._planning.get_current_daily_plan(plan_date, data_set_id=self._data_set_id)
        stored_plan = (
            current
            if current is not None and any(item.status == "planned" for item in current.items)
            else None
        )
        if stored_plan is not None:
            waveplates = _stored_waveplates(stored_plan, waveplates)
        return self._build_bundle(
            plan_date,
            waveplates,
            persist=False,
            stored_plan=stored_plan,
        )

    def _weekly_usage(self, plan_date: date) -> tuple[WeeklyMaterialUsage, ...]:
        week_start = plan_date - timedelta(days=plan_date.isoweekday() - 1)
        week_end = week_start + timedelta(days=6)
        usage: dict[str, int] = {}
        for plan in self._planning.list_daily_plans(data_set_id=self._data_set_id):
            if not week_start <= plan.plan_date <= week_end:
                continue
            for item in plan.items:
                was_completed = item.status == "completed" or (
                    item.status == "archived"
                    and item.assumptions.get("archived_from_status") == "completed"
                )
                if was_completed and item.material_id is not None:
                    usage[item.material_id] = usage.get(item.material_id, 0) + item.recommended_runs
        return tuple(
            WeeklyMaterialUsage(material_id, runs) for material_id, runs in sorted(usage.items())
        )

    def _character_evaluations(
        self,
        targets: tuple[CultivationTargetSnapshot, ...],
    ) -> tuple[_CharacterEvaluation, ...]:
        del targets
        profiles = self._profiles.list_profiles(data_set_id=self._data_set_id)
        default = self._profiles.get_default(data_set_id=self._data_set_id)
        by_id = {profile.id: profile for profile in profiles}
        summaries = self._characters.list_characters(data_set_id=self._data_set_id)
        rows: list[_CharacterEvaluation] = []
        for summary in summaries:
            if not summary.is_candidate or summary.archived_at is not None:
                continue
            detail = self._characters.get_character(summary.id, data_set_id=self._data_set_id)
            profile = by_id.get(summary.scoring_profile_id or "", default)
            passing = 70.0 if profile is None else profile.thresholds.passing
            evaluated = evaluate_character_detail(
                detail,
                passing_score=passing,
                scoring_profile_id=None if profile is None else profile.id,
                scoring_profile_version=None if profile is None else profile.version,
            )
            rows.append(
                _CharacterEvaluation(
                    detail,
                    evaluated.completion,
                    evaluated.next_action,
                    passing,
                )
            )
        rows.sort(
            key=lambda item: (
                item.detail.summary.priority,
                item.completion.overall_percent,
                item.detail.summary.name.casefold(),
            )
        )
        return tuple(rows)

    def _submit(self, operation: _Operation, work: Callable[[], _Bundle]) -> None:
        if self._closing:
            return
        with self._lock:
            self._latest_request_id += 1
            request_id = self._latest_request_id

        def run() -> _JobResult:
            try:
                return _JobResult(operation, request_id, bundle=work())
            except Exception as exc:
                return _JobResult(
                    operation,
                    request_id,
                    error=f"{type(exc).__name__}: {exc}",
                )

        future = cast(
            Future[object], self._executor.submit(run, read_only=operation is _Operation.LOAD)
        )
        with self._lock:
            self._futures.add(future)

        def completed(done: Future[object]) -> None:
            with self._lock:
                self._futures.discard(done)
            if not self._closing:
                self._job_completed.emit(done.result())

        future.add_done_callback(completed)

    def _submit_dialog(
        self,
        operation: _DialogOperation,
        work: Callable[[], PlanningDataBundle | _DialogResult],
    ) -> None:
        if self._closing:
            return

        def run() -> _DialogResult:
            try:
                value = work()
                if isinstance(value, _DialogResult):
                    return value
                return _DialogResult(operation, bundle=value)
            except Exception as exc:
                return _DialogResult(operation, error=f"{type(exc).__name__}: {exc}")

        future = cast(
            Future[object], self._executor.submit(run, read_only=operation is _DialogOperation.LOAD)
        )
        with self._lock:
            self._futures.add(future)

        def completed(done: Future[object]) -> None:
            with self._lock:
                self._futures.discard(done)
            if not self._closing:
                self._dialog_completed.emit(done.result())

        future.add_done_callback(completed)

    @Slot(object)
    def _handle_result(self, payload: object) -> None:
        if self._closing or not isinstance(payload, _JobResult):
            return
        if payload.request_id != self._latest_request_id:
            return
        if payload.error is not None or payload.bundle is None:
            if payload.operation is _Operation.INVENTORY:
                self._page.inventory_model.clear_pending()
            self._page.show_error(payload.error or "規劃結果不完整")
            return
        self._page.set_snapshot(payload.bundle.snapshot)
        if payload.operation is _Operation.INVENTORY:
            self._page.show_status("素材庫存已保存, 今日規劃已重新計算。")
        elif payload.operation is _Operation.GENERATE:
            self._page.show_status(
                "今日規劃已保存。"
                if self._legacy_resource_planning
                else "今日技能與無音區建議已更新。"
            )
        elif payload.operation is _Operation.COMPLETE:
            self._page.show_status("已標記今日規劃完成; 請把實際掉落量更新到素材庫存。")
        self.dashboard_priority_changed.emit(payload.bundle.dashboard_priority)

    @Slot(object)
    def _handle_dialog_result(self, payload: object) -> None:
        if self._closing or not isinstance(payload, _DialogResult):
            return
        dialog = self._data_dialog
        if dialog is None:
            return
        if payload.error is not None or payload.bundle is None:
            dialog.show_error(payload.error or "規劃資料不完整")
            return
        dialog.set_bundle(payload.bundle)
        if payload.selected_material_id is not None:
            dialog.select_material(payload.selected_material_id)
            material = next(
                (
                    item
                    for item in payload.bundle.materials
                    if item.identifier == payload.selected_material_id
                ),
                None,
            )
            if material is not None:
                dialog.apply_material(material)
        if payload.selected_target_id is not None:
            dialog.select_target(payload.selected_target_id)
            target = next(
                (
                    item
                    for item in payload.bundle.targets
                    if item.identifier == payload.selected_target_id
                ),
                None,
            )
            if target is not None:
                dialog.apply_target(target)
        if payload.operation is not _DialogOperation.LOAD:
            dialog.show_status("本機規劃資料已保存。")
            self.generate_plan(self._page.current_request())


def _planning_state(
    item: _CharacterEvaluation,
    targets: tuple[CultivationTargetSnapshot, ...],
) -> CharacterPlanningState:
    detail = item.detail
    targets_for_character = tuple(
        target for target in targets if target.character_id == detail.summary.id
    )
    echo_targets = {
        _slot_from_target(target): target
        for target in targets_for_character
        if _slot_from_target(target) is not None
    }
    echo_slots: list[PlanningEchoSlot] = []
    assessments = {slot.slot_index: slot for slot in item.completion.echo_slots}
    for slot in detail.echo_slots:
        target = echo_targets.get(slot.slot_index)
        if slot.echo_id is None:
            echo_slots.append(
                PlanningEchoSlot(
                    slot.slot_index,
                    False,
                    cultivation_target_id=None if target is None else target.id,
                    material_id=_first_material_id(target),
                )
            )
            continue
        assessment = assessments[slot.slot_index]
        echo_slots.append(
            PlanningEchoSlot(
                slot_index=slot.slot_index,
                is_filled=True,
                set_is_valid=_validity(
                    assessment.issues,
                    unknown=BlockerCode.ECHO_SET_UNKNOWN,
                    invalid=BlockerCode.ECHO_SET_INVALID,
                ),
                main_stat_is_valid=_validity(
                    assessment.issues,
                    unknown=BlockerCode.ECHO_MAIN_STAT_UNKNOWN,
                    invalid=BlockerCode.ECHO_MAIN_STAT_INVALID,
                ),
                score=assessment.score,
                cultivation_target_id=None if target is None else target.id,
                material_id=_first_material_id(target),
            )
        )
    functional = tuple(
        threshold
        for target in targets_for_character
        if target.target_type.casefold() in {"threshold", "functional_threshold"}
        if (threshold := _functional_threshold(target)) is not None
    )
    weapon_target = next(
        (
            target
            for target in targets_for_character
            if target.target_type.casefold() in {"weapon", "weapon_level"}
        ),
        None,
    )
    weapon_goal = _positive_int(
        None if weapon_target is None else weapon_target.target_value,
        _positive_int(detail.cultivation_goal.get("weapon_level_target"), 90),
    )
    weapon_current = 0 if detail.weapon is None else detail.weapon.level
    weapon_tasks = (
        CultivationTaskProgress(
            "weapon-level",
            "武器等級",
            weapon_current,
            weapon_goal,
            cultivation_target_id=None if weapon_target is None else weapon_target.id,
            material_id=_first_material_id(weapon_target),
        ),
    )
    skill_tasks = tuple(
        CultivationTaskProgress(
            skill.skill_key,
            skill.name,
            skill.current_level,
            skill.target_level,
            cultivation_target_id=(
                None
                if (target := _skill_target(skill.skill_key, targets_for_character)) is None
                else target.id
            ),
            material_id=_first_material_id(target),
        )
        for skill in detail.skills
    )
    character_level_target = _task_target(targets_for_character, {"character", "character_level"})
    weapon_rank_target = _task_target(
        targets_for_character,
        {"weapon_rank", "weapon_resonance", "weapon_resonance_rank"},
    )
    small_nodes_target = _task_target(
        targets_for_character, {"attribute_nodes", "small_stat_nodes"}
    )
    cultivation_tasks = (
        CultivationTaskProgress(
            "character-level",
            "角色等級",
            detail.summary.level,
            _positive_int(
                None if character_level_target is None else character_level_target.target_value,
                _positive_int(detail.cultivation_goal.get("character_level_target"), 90),
            ),
            cultivation_target_id=(
                None if character_level_target is None else character_level_target.id
            ),
            material_id=_first_material_id(character_level_target),
        ),
        CultivationTaskProgress(
            "weapon-rank",
            "武器諧振階級",
            0 if detail.weapon is None else detail.weapon.resonance_rank,
            _positive_int(
                None if weapon_rank_target is None else weapon_rank_target.target_value,
                _positive_int(detail.cultivation_goal.get("weapon_rank_target"), 1),
            ),
            cultivation_target_id=(None if weapon_rank_target is None else weapon_rank_target.id),
            material_id=_first_material_id(weapon_rank_target),
        ),
        CultivationTaskProgress(
            "small-stat-nodes",
            "小型屬性節點",
            sum(detail.small_stat_nodes),
            _positive_int(
                None if small_nodes_target is None else small_nodes_target.target_value,
                len(detail.small_stat_nodes),
            ),
            cultivation_target_id=(None if small_nodes_target is None else small_nodes_target.id),
            material_id=_first_material_id(small_nodes_target),
        ),
    )
    return CharacterPlanningState(
        character_id=detail.summary.id,
        character_name=detail.summary.name,
        user_priority=detail.summary.priority,
        is_candidate=detail.summary.is_candidate,
        is_current_goal_reached=item.completion.at_goal,
        echo_slots=tuple(echo_slots),
        functional_thresholds=functional,
        weapon_tasks=weapon_tasks,
        skill_tasks=skill_tasks,
        cultivation_tasks=cultivation_tasks,
        echo_passing_score=item.passing_score,
    )


def _resource_target(
    target: CultivationTargetSnapshot,
    queue_rank: int | None,
) -> ResourcePlanningTarget:
    subject = target.character_name or "帳號"
    label = target.target_key or target.target_type
    reason = target.notes or f"{subject} 的「{label}」尚未達到本機設定目標。"
    return ResourcePlanningTarget(
        target_id=target.id,
        label=f"{subject}: {label}",
        queue_rank=queue_rank or (10_000 + target.priority * 100),
        reason=reason,
        is_completed=target.is_completed,
        character_id=target.character_id,
        cultivation_target_id=target.id,
        due_date=target.due_date,
    )


def _domain_material(item: MaterialSnapshot) -> MaterialDefinition:
    try:
        source_status = StaticDataSourceStatus(item.source_status)
    except ValueError:
        source_status = StaticDataSourceStatus.UNKNOWN
    return MaterialDefinition(
        material_id=item.id,
        name=item.name,
        category=item.category,
        stamina_cost_per_run=item.stamina_cost_per_run or None,
        average_yield=(
            None
            if item.average_yield is None or item.average_yield <= 0
            else _decimal(item.average_yield)
        ),
        open_weekdays=item.open_days or None,
        weekly_limit=item.weekly_limit,
        source_status=source_status,
        source_version=item.source_version,
    )


def _recommendation(action: Any) -> PlanRecommendationView:
    return PlanRecommendationView(
        identifier=f"{action.target_id}:{action.material_id or 'manual'}",
        title=f"{action.target_label} / {action.material_name}",
        category=(
            "需要人工確認" if action.status is DailyActionStatus.NEEDS_CONFIRMATION else "素材刷取"
        ),
        suggested_runs=action.recommended_runs,
        waveplate_cost=action.stamina_cost,
        reason=action.reason,
        character_name=None,
    )


def _queue_recommendation(action: ProgressionAction) -> PlanRecommendationView:
    return PlanRecommendationView(
        identifier=action.action_id,
        title=action.title,
        category="需要人工確認" if action.requires_manual_confirmation else "養成佇列",
        suggested_runs=None,
        waveplate_cost=None,
        reason=action.reason,
        character_name=action.character_name,
    )


def _stored_recommendations(
    plan: DailyPlanSnapshot,
    materials: tuple[MaterialSnapshot, ...],
    targets: tuple[CultivationTargetSnapshot, ...],
) -> tuple[PlanRecommendationView, ...]:
    material_by_id = {item.id: item for item in materials}
    target_by_id = {item.id: item for item in targets}
    rows: list[PlanRecommendationView] = []
    for item in plan.items[:2]:
        assumptions = item.assumptions
        target = (
            None
            if item.cultivation_target_id is None
            else target_by_id.get(item.cultivation_target_id)
        )
        material = None if item.material_id is None else material_by_id.get(item.material_id)
        character_name = None if target is None else target.character_name
        target_label = (
            None
            if target is None
            else f"{target.character_name or '帳號'}: {target.target_key or target.target_type}"
        )
        fallback_title = "已儲存養成項目"
        if target_label is not None and material is not None:
            fallback_title = f"{target_label} / {material.name}"
        elif target_label is not None:
            fallback_title = target_label
        elif material is not None:
            fallback_title = material.name
        requires_confirmation = assumptions.get("requires_manual_confirmation") is True
        is_queue_item = isinstance(assumptions.get("queue_action_id"), str)
        rows.append(
            PlanRecommendationView(
                identifier=item.id,
                title=_assumption_text(assumptions, "display_title") or fallback_title,
                category=(
                    _assumption_text(assumptions, "display_category")
                    or ("需要人工確認" if requires_confirmation else "本機已儲存規劃")
                ),
                suggested_runs=(
                    None if is_queue_item or requires_confirmation else item.recommended_runs
                ),
                waveplate_cost=(
                    None if is_queue_item or requires_confirmation else item.stamina_cost
                ),
                reason=item.reason,
                character_name=(_assumption_text(assumptions, "character_name") or character_name),
            )
        )
    return tuple(rows)


def _stored_waveplates(plan: DailyPlanSnapshot, fallback: int) -> int:
    for item in plan.items:
        value = item.assumptions.get("daily_waveplates")
        if type(value) is int and 0 <= value <= 9_999:
            return value
    return fallback


def _material_provenance(material: MaterialSnapshot | None) -> dict[str, object] | None:
    if material is None:
        return None
    return {
        "id": material.id,
        "code": material.code,
        "name": material.name,
        "category": material.category,
        "stamina_cost_per_run": material.stamina_cost_per_run,
        "average_yield": material.average_yield,
        "open_days": list(material.open_days),
        "weekly_limit": material.weekly_limit,
        "source_status": material.source_status,
        "source_version": material.source_version,
    }


def _plan_items(
    actions: tuple[Any, ...],
    queue: tuple[ProgressionAction, ...],
    assumptions: tuple[str, ...],
    waveplates: int,
    materials: Mapping[str, MaterialSnapshot],
) -> tuple[DailyPlanItemDefinition, ...]:
    common: dict[str, Any] = {
        "daily_waveplates": waveplates,
        "assumptions": list(assumptions),
    }
    if actions:
        return tuple(
            DailyPlanItemDefinition(
                cultivation_target_id=action.cultivation_target_id,
                material_id=action.material_id,
                recommended_runs=action.recommended_runs or 0,
                stamina_cost=action.stamina_cost or 0,
                reason=action.reason,
                status="planned",
                estimated_completion_date=action.estimated_completion_date,
                assumptions={
                    **common,
                    "requires_manual_confirmation": action.status
                    is DailyActionStatus.NEEDS_CONFIRMATION,
                    "display_title": f"{action.target_label} / {action.material_name}",
                    "display_category": (
                        "需要人工確認"
                        if action.status is DailyActionStatus.NEEDS_CONFIRMATION
                        else "素材刷取"
                    ),
                    "material_snapshot": _material_provenance(materials.get(action.material_id)),
                },
            )
            for action in actions
        )
    return tuple(
        DailyPlanItemDefinition(
            cultivation_target_id=action.cultivation_target_id,
            material_id=action.material_id,
            reason=action.reason,
            status="planned",
            assumptions={
                **common,
                "queue_action_id": action.action_id,
                "requires_manual_confirmation": action.requires_manual_confirmation,
                "display_title": action.title,
                "display_category": (
                    "需要人工確認" if action.requires_manual_confirmation else "養成佇列"
                ),
                "character_name": action.character_name,
                "material_snapshot": _material_provenance(materials.get(action.material_id or "")),
            },
        )
        for action in queue[:2]
    )


def _shortage_views(
    materials: tuple[MaterialSnapshot, ...],
    inventory: tuple[InventoryCountSnapshot, ...],
    targets: tuple[CultivationTargetSnapshot, ...],
    result: Any,
) -> tuple[WeeklyShortageView, ...]:
    material_by_id = {item.id: item for item in materials}
    inventory_by_id = {item.material_id: item.quantity for item in inventory}
    required: dict[str, float] = {}
    characters: dict[str, set[str]] = {}
    estimates = {item.target_id: item.estimated_date for item in result.target_estimates}
    completion_dates: dict[str, list[date]] = {}
    for target in targets:
        if target.is_completed:
            continue
        for requirement in target.required_materials:
            required[requirement.material_id] = (
                required.get(requirement.material_id, 0.0) + requirement.required_quantity
            )
            if target.character_name:
                characters.setdefault(requirement.material_id, set()).add(target.character_name)
            estimated = estimates.get(target.id)
            if estimated is not None:
                completion_dates.setdefault(requirement.material_id, []).append(estimated)
    rows: list[WeeklyShortageView] = []
    for shortage in result.weekly_shortages:
        material = material_by_id[shortage.material_id]
        rows.append(
            WeeklyShortageView(
                material_id=shortage.material_id,
                material_name=material.name,
                current_count=_view_quantity(inventory_by_id.get(shortage.material_id, 0.0)),
                required_count=_view_quantity(required.get(shortage.material_id, 0.0)),
                character_names=tuple(sorted(characters.get(shortage.material_id, set()))),
                expected_completion_date=(
                    max(completion_dates[shortage.material_id])
                    if completion_dates.get(shortage.material_id)
                    else None
                ),
                current_count_confirmed=shortage.material_id in inventory_by_id,
            )
        )
    return tuple(rows)


def _inventory_views(
    materials: tuple[MaterialSnapshot, ...],
    inventory: tuple[InventoryCountSnapshot, ...],
) -> tuple[InventoryItemView, ...]:
    count_by_id = {item.material_id: item.quantity for item in inventory}
    return tuple(
        InventoryItemView(
            material_id=item.id,
            material_name=item.name,
            category=item.category,
            count=_view_quantity(count_by_id.get(item.id, 0.0)),
            confirmed=item.id in count_by_id,
        )
        for item in materials
    )


def _weekly_summary(shortages: tuple[Any, ...], notices: tuple[Any, ...]) -> str:
    if not shortages and not notices:
        return "本週已沒有可辨識的素材缺口。"
    parts = []
    if shortages:
        parts.append(f"本週仍有 {len(shortages)} 種素材缺口")
    if notices:
        parts.append(f"{len(notices)} 項資料需要確認")
    return "; ".join(parts) + "。"


def _dashboard_text(recommendation: PlanRecommendationView | None) -> str:
    if recommendation is None:
        return "目前沒有可執行的本機養成項目"
    if recommendation.suggested_runs is not None:
        return f"{recommendation.title}: {recommendation.suggested_runs} 次"
    return recommendation.title


def _planning_character_label(character: CharacterSummary) -> str:
    if character.archived_at is not None:
        return f"{character.name} (已封存)"
    if not character.is_candidate:
        return f"{character.name} (未列入常用)"
    return character.name


def _validity(
    issues: tuple[BlockerCode, ...],
    *,
    unknown: BlockerCode,
    invalid: BlockerCode,
) -> bool | None:
    if unknown in issues:
        return None
    return invalid not in issues


def _slot_from_target(target: CultivationTargetSnapshot) -> int | None:
    if target.target_type.casefold() not in {"echo", "echo_slot"}:
        return None
    key = (target.target_key or "").casefold().replace("_", "-")
    for prefix in ("slot-", "echo-slot-"):
        if key.startswith(prefix) and key[len(prefix) :].isdigit():
            value = int(key[len(prefix) :])
            return value if 1 <= value <= 5 else None
    return None


def _first_material_id(target: CultivationTargetSnapshot | None) -> str | None:
    if target is None or not target.required_materials:
        return None
    return target.required_materials[0].material_id


def _task_target(
    targets: tuple[CultivationTargetSnapshot, ...],
    target_types: set[str],
) -> CultivationTargetSnapshot | None:
    return next(
        (
            target
            for target in targets
            if target.target_type.casefold() in target_types and not target.is_completed
        ),
        None,
    )


def _skill_target(
    skill_key: str,
    targets: tuple[CultivationTargetSnapshot, ...],
) -> CultivationTargetSnapshot | None:
    return next(
        (
            target
            for target in targets
            if target.target_type.casefold() in {"skill", "skill_level"}
            and (target.target_key or "").casefold() == skill_key.casefold()
        ),
        None,
    )


def _functional_threshold(target: CultivationTargetSnapshot) -> FunctionalThreshold | None:
    raw_target = target.target_value
    raw_current = target.current_value
    direction = ThresholdDirection.AT_LEAST
    unit: str | None = None
    if isinstance(raw_target, Mapping):
        unit_value = raw_target.get("unit")
        unit = str(unit_value).strip() if unit_value is not None else None
        if raw_target.get("direction") == ThresholdDirection.AT_MOST.value:
            direction = ThresholdDirection.AT_MOST
        raw_target = raw_target.get("value")
    if isinstance(raw_current, Mapping):
        raw_current = raw_current.get("value")
    target_value = _finite_number(raw_target)
    if target_value is None:
        return None
    return FunctionalThreshold(
        key=target.target_key or target.id,
        label=target.target_key or "功能門檻",
        current=_finite_number(raw_current),
        target=target_value,
        unit=unit,
        direction=direction,
        cultivation_target_id=target.id,
        material_id=_first_material_id(target),
    )


def _positive_int(value: object, default: int) -> int:
    return value if type(value) is int and value > 0 else default


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number and abs(number) != float("inf") else None


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _parse_manual_value(value: str, *, allow_blank: bool) -> object:
    normalized = value.strip()
    if not normalized:
        if allow_blank:
            return None
        raise ValueError("目標值不可空白")
    try:
        return json.loads(normalized)
    except json.JSONDecodeError:
        return normalized


def _manual_value_text(value: object) -> str:
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _assumption_text(values: Mapping[str, object], key: str) -> str | None:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _view_quantity(value: float) -> float:
    return max(0.0, round(value, 4))


__all__ = ["DAILY_WAVEPLATES_KEY", "TodayPlanPresenter"]
