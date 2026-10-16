"""Non-blocking presenter for the manual, offline gacha planner."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from threading import RLock
from typing import cast
from zoneinfo import ZoneInfo

from PySide6.QtCore import QObject, Qt, Signal, Slot

from wuwa_assistant.application.gacha import (
    GachaIncomeDefinition,
    GachaPlanningService,
    GachaStatusDefinition,
    GachaTargetDefinition,
    GachaTargetSnapshot,
    GachaWorkspaceSnapshot,
    parse_manual_pull_requirements,
)
from wuwa_assistant.gacha.models import ForecastScenario, PullType
from wuwa_assistant.ui.controllers.background_executor import BackgroundExecutor
from wuwa_assistant.ui.pages.gacha import (
    GachaIncomeSaveRequest,
    GachaIncomeView,
    GachaPage,
    GachaProjectionView,
    GachaSnapshot,
    GachaStatusSaveRequest,
    GachaStatusView,
    GachaTargetSaveRequest,
    GachaTargetView,
)


class _Operation(StrEnum):
    LOAD = "load"
    STATUS_SAVE = "status_save"
    TARGET_SAVE = "target_save"
    TARGET_DELETE = "target_delete"
    INCOME_SAVE = "income_save"
    INCOME_DELETE = "income_delete"


@dataclass(frozen=True, slots=True)
class _JobResult:
    operation: _Operation
    request_id: int
    workspace: GachaWorkspaceSnapshot | None = None
    error: str | None = None


class GachaPresenter(QObject):
    """Keep SQLite planning work off the UI thread and publish immutable DTOs."""

    _job_completed = Signal(object)

    def __init__(
        self,
        *,
        service: GachaPlanningService,
        page: GachaPage,
        data_set_id: str,
        today: Callable[[], date] = lambda: datetime.now(ZoneInfo("Asia/Taipei")).date(),
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._page = page
        self._data_set_id = data_set_id
        self._today = today
        self._executor = BackgroundExecutor(max_workers=1, thread_name_prefix="wuwa-gacha-db")
        self._lock = RLock()
        self._futures: set[Future[object]] = set()
        self._latest_request_id = 0
        self._closing = False

        self._job_completed.connect(self._handle_result, Qt.ConnectionType.QueuedConnection)
        page.refresh_requested.connect(self.refresh)
        page.status_save_requested.connect(self.save_status)
        page.target_save_requested.connect(self.save_target)
        page.target_delete_requested.connect(self.delete_target)
        page.income_save_requested.connect(self.save_income)
        page.income_delete_requested.connect(self.delete_income)

    def initialize(self) -> None:
        if self._closing:
            return
        self._page.set_loading(True, "正在載入本機抽卡資料…")
        self._submit(_Operation.LOAD, self._load_workspace)

    @Slot()
    def refresh(self) -> None:
        if self._closing:
            return
        self._page.set_loading(True, "正在重新整理抽卡規劃…")
        self._submit(_Operation.LOAD, self._load_workspace)

    @Slot(object)
    def save_status(self, payload: object) -> None:
        if self._closing:
            return
        if not isinstance(payload, GachaStatusSaveRequest):
            self._page.show_error("抽卡資源資料格式不正確。")
            return
        definition = GachaStatusDefinition(
            astrite=payload.astrite,
            limited_character_pulls=payload.character_pulls,
            limited_weapon_pulls=payload.weapon_pulls,
            character_pity=payload.character_pity,
            weapon_pity=payload.weapon_pity,
            character_guaranteed=payload.character_guaranteed,
            weapon_guaranteed=payload.weapon_guaranteed,
        )
        self._page.set_loading(True, "正在保存目前抽卡資源…")

        def save() -> GachaWorkspaceSnapshot:
            self._service.save_status(data_set_id=self._data_set_id, definition=definition)
            return self._load_workspace()

        self._submit(_Operation.STATUS_SAVE, save)

    @Slot(object)
    def save_target(self, payload: object) -> None:
        if self._closing:
            return
        if not isinstance(payload, GachaTargetSaveRequest):
            self._page.show_error("抽卡目標資料格式不正確。")
            return
        request = payload
        self._page.set_loading(True, "正在保存抽卡目標…")

        def save() -> GachaWorkspaceSnapshot:
            scenario_json: dict[str, object] = {}
            if request.identifier is not None:
                current = self._service.get_target(
                    request.identifier,
                    data_set_id=self._data_set_id,
                )
                scenario_json = deepcopy(current.scenario_json)
            scenario_json["required_pulls"] = _requirements_json(request)
            definition = GachaTargetDefinition(
                target_type=request.target_type,
                name=request.name,
                target_copies=request.copies,
                target_version=request.target_version,
                target_date=request.target_date,
                priority=request.priority,
                status=request.status,
                scenario_json=scenario_json,
                notes=request.notes or None,
            )
            if request.identifier is None:
                self._service.create_target(
                    data_set_id=self._data_set_id,
                    definition=definition,
                )
            else:
                self._service.update_target(
                    request.identifier,
                    data_set_id=self._data_set_id,
                    definition=definition,
                )
            return self._load_workspace()

        self._submit(_Operation.TARGET_SAVE, save)

    @Slot(str)
    def delete_target(self, target_id: str) -> None:
        if self._closing:
            return
        if not target_id.strip():
            self._page.show_error("找不到要刪除的抽卡目標。")
            return
        self._page.set_loading(True, "正在刪除抽卡目標…")

        def delete() -> GachaWorkspaceSnapshot:
            self._service.delete_target(target_id, data_set_id=self._data_set_id)
            return self._load_workspace()

        self._submit(_Operation.TARGET_DELETE, delete)

    @Slot(object)
    def save_income(self, payload: object) -> None:
        if self._closing:
            return
        if not isinstance(payload, GachaIncomeSaveRequest):
            self._page.show_error("抽卡收入資料格式不正確。")
            return
        request = payload
        self._page.set_loading(True, "正在保存預估收入…")

        def save() -> GachaWorkspaceSnapshot:
            recurrence_rule_json: dict[str, object] | None = None
            if request.identifier is not None:
                current = self._service.get_income(
                    request.identifier,
                    data_set_id=self._data_set_id,
                )
                recurrence_rule_json = deepcopy(current.recurrence_rule_json)
            definition = GachaIncomeDefinition(
                label=request.label,
                expected_date=request.expected_date,
                astrite_amount=request.astrite_amount,
                pull_amount=request.pull_amount,
                pull_type=request.pull_type,
                scenario=request.scenario,
                recurrence_rule_json=recurrence_rule_json,
                is_received=request.received,
                notes=request.notes or None,
            )
            if request.identifier is None:
                self._service.create_income(
                    data_set_id=self._data_set_id,
                    definition=definition,
                )
            else:
                self._service.update_income(
                    request.identifier,
                    data_set_id=self._data_set_id,
                    definition=definition,
                )
            return self._load_workspace()

        self._submit(_Operation.INCOME_SAVE, save)

    @Slot(str)
    def delete_income(self, income_id: str) -> None:
        if self._closing:
            return
        if not income_id.strip():
            self._page.show_error("找不到要刪除的預估收入。")
            return
        self._page.set_loading(True, "正在刪除預估收入…")

        def delete() -> GachaWorkspaceSnapshot:
            self._service.delete_income(income_id, data_set_id=self._data_set_id)
            return self._load_workspace()

        self._submit(_Operation.INCOME_DELETE, delete)

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

    def _load_workspace(self) -> GachaWorkspaceSnapshot:
        as_of = self._today()
        if type(as_of) is not date:
            raise ValueError("today must return a date")
        return self._service.load_workspace(data_set_id=self._data_set_id, as_of=as_of)

    def _submit(
        self,
        operation: _Operation,
        work: Callable[[], GachaWorkspaceSnapshot],
    ) -> None:
        if self._closing:
            return
        with self._lock:
            self._latest_request_id += 1
            request_id = self._latest_request_id

        def run() -> _JobResult:
            try:
                return _JobResult(operation, request_id, workspace=work())
            except Exception as exc:  # UI boundary turns local storage errors into text.
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

    @Slot(object)
    def _handle_result(self, payload: object) -> None:
        if self._closing or not isinstance(payload, _JobResult):
            return
        if payload.request_id != self._latest_request_id:
            return
        if payload.error is not None or payload.workspace is None:
            self._page.show_error(payload.error or "抽卡規劃結果不完整。")
            return
        self._page.set_snapshot(_view_snapshot(payload.workspace))
        message = _operation_message(payload.operation)
        if message is not None:
            self._page.show_status(message)


def _requirements_json(request: GachaTargetSaveRequest) -> dict[str, int]:
    return {
        scenario: value
        for scenario, value in (
            ("conservative", request.conservative_required),
            ("typical", request.typical_required),
            ("optimistic", request.optimistic_required),
        )
        if value is not None
    }


def _view_snapshot(workspace: GachaWorkspaceSnapshot) -> GachaSnapshot:
    status = workspace.status
    status_view = (
        GachaStatusView()
        if status is None
        else GachaStatusView(
            astrite=status.astrite,
            character_pulls=status.limited_character_pulls,
            weapon_pulls=status.limited_weapon_pulls,
            character_pity=status.character_pity,
            weapon_pity=status.weapon_pity,
            character_guaranteed=status.character_guaranteed,
            weapon_guaranteed=status.weapon_guaranteed,
        )
    )
    targets = tuple(_target_view(item) for item in workspace.targets)
    incomes = tuple(
        GachaIncomeView(
            identifier=item.id,
            label=item.label,
            expected_date=item.expected_date,
            astrite_amount=item.astrite_amount,
            pull_amount=item.pull_amount,
            pull_type=item.pull_type,
            scenario=item.scenario,
            received=item.is_received,
            notes=item.notes or "",
        )
        for item in workspace.incomes
    )
    projections = tuple(_projection_view(workspace, scenario) for scenario in ForecastScenario)
    warnings = tuple(
        f"{_scenario_label(item.scenario)}{item.message}" for item in workspace.plan.warnings
    )
    return GachaSnapshot(
        status=status_view,
        targets=targets,
        incomes=incomes,
        projections=projections,
        warnings=warnings,
    )


def _target_view(item: GachaTargetSnapshot) -> GachaTargetView:
    requirements = parse_manual_pull_requirements(item.scenario_json)
    return GachaTargetView(
        identifier=item.id,
        target_type=item.target_type,
        name=item.name,
        copies=item.target_copies,
        target_version=item.target_version,
        target_date=item.target_date,
        priority=item.priority,
        status=item.status,
        conservative_required=requirements.conservative,
        typical_required=requirements.typical,
        optimistic_required=requirements.optimistic,
        notes=item.notes or "",
    )


def _projection_view(
    workspace: GachaWorkspaceSnapshot,
    scenario: ForecastScenario,
) -> GachaProjectionView:
    plan = workspace.plan.scenario(scenario)
    character_funded = sum(
        allocation.funded_pulls or 0
        for allocation in plan.allocations
        if allocation.target_type is PullType.CHARACTER
    )
    weapon_funded = sum(
        allocation.funded_pulls or 0
        for allocation in plan.allocations
        if allocation.target_type is PullType.WEAPON
    )
    shortfall = sum(allocation.missing_pulls or 0 for allocation in plan.allocations)
    unknown_target_count = sum(allocation.required_pulls is None for allocation in plan.allocations)
    warning_count = sum(
        warning.scenario is None or warning.scenario is scenario
        for warning in workspace.plan.warnings
    )
    return GachaProjectionView(
        scenario=scenario.value,
        character_available=character_funded,
        weapon_available=weapon_funded,
        unallocated_astrite=plan.remaining_resources.astrite,
        shortfall=shortfall,
        warning_count=warning_count,
        remaining_character_pulls=plan.remaining_resources.character_pulls,
        remaining_weapon_pulls=plan.remaining_resources.weapon_pulls,
        unknown_target_count=unknown_target_count,
    )


def _scenario_label(scenario: ForecastScenario | None) -> str:
    if scenario is None:
        return ""
    return {
        ForecastScenario.CONSERVATIVE: "最保守: ",
        ForecastScenario.TYPICAL: "一般: ",
        ForecastScenario.OPTIMISTIC: "樂觀: ",
    }[scenario]


def _operation_message(operation: _Operation) -> str | None:
    return {
        _Operation.LOAD: None,
        _Operation.STATUS_SAVE: "目前抽卡資源已保存。",
        _Operation.TARGET_SAVE: "抽卡目標已保存; 三種情境只使用人工需求抽數。",
        _Operation.TARGET_DELETE: "抽卡目標已刪除。",
        _Operation.INCOME_SAVE: "預估收入已保存。",
        _Operation.INCOME_DELETE: "預估收入已刪除。",
    }[operation]


__all__ = ["GachaPresenter"]
