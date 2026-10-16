"""Background controller for safe first-run setup and one-shot monitor tests."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

import numpy as np
from PySide6.QtCore import QObject, Qt, Signal, Slot

from wuwa_assistant.application.onboarding import (
    OnboardingCommand,
    OnboardingMode,
    OnboardingService,
    OnboardingState,
)
from wuwa_assistant.capture import CaptureBackend, MonitorInfo, MssMonitorCaptureBackend
from wuwa_assistant.display import DEFAULT_RESOLUTION, RESOLUTIONS
from wuwa_assistant.models import PageType
from wuwa_assistant.ocr import OcrBackendError, OcrEngine, RapidOcrEngine
from wuwa_assistant.ui.pages.onboarding import (
    CaptureTestRequest,
    MonitorChoice,
    OnboardingRequest,
    OnboardingWizard,
)
from wuwa_assistant.vision.classifier import ENHANCEMENT_STATE_ROI, PageClassifier
from wuwa_assistant.vision.rois import MATERIAL_CTA_ROI, PAGE_HEADER_ROI

type CaptureBackendFactory = Callable[[], CaptureBackend]
type OcrEngineFactory = Callable[[], OcrEngine]


class _Operation(StrEnum):
    DISCOVER = "discover"
    TEST = "test"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class _JobResult:
    operation: _Operation
    payload: object | None = None
    identifier: str | None = None
    error: str | None = None
    resolution: str | None = None


@dataclass(frozen=True, slots=True)
class _CaptureTestOutcome:
    monitor: MonitorInfo
    page_type: PageType
    confidence: float
    brightness: float
    variance: float


class OnboardingController(QObject):
    """Keep MSS, frame leases, and database writes away from the Qt UI thread."""

    _job_completed = Signal(object)

    def __init__(
        self,
        *,
        service: OnboardingService,
        wizard: OnboardingWizard,
        backend_factory: CaptureBackendFactory = MssMonitorCaptureBackend,
        ocr_factory: OcrEngineFactory = RapidOcrEngine,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._wizard = wizard
        self._backend_factory = backend_factory
        self._ocr_factory = ocr_factory
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wuwa-onboarding")
        self._monitors: dict[str, MonitorInfo] = {}
        self._futures: set[Future[object]] = set()
        self._closing = False
        self._job_completed.connect(self._handle_result, Qt.ConnectionType.QueuedConnection)
        wizard.capture_test_requested.connect(self.test_capture)
        wizard.completion_requested.connect(self.complete)

    def initialize(self) -> None:
        self._submit(_Operation.DISCOVER, self._discover_monitors)

    def shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._executor.shutdown(wait=True, cancel_futures=True)

    @Slot(object)
    def test_capture(self, value: object) -> None:
        if self._closing or not isinstance(value, CaptureTestRequest):
            return
        monitor = self._monitors.get(value.monitor_identifier)
        if monitor is None:
            self._wizard.set_capture_test_result(
                value.monitor_identifier,
                False,
                "選取的顯示器已不存在, 請重新偵測。",
            )
            self.initialize()
            return
        self._submit(
            _Operation.TEST,
            lambda: self._test_monitor(monitor, value.resolution),
            identifier=value.monitor_identifier,
            resolution=value.resolution,
        )

    @Slot(object)
    def complete(self, value: object) -> None:
        if self._closing or not isinstance(value, OnboardingRequest):
            return
        command = OnboardingCommand(
            monitor_identifier=value.monitor_identifier,
            data_path=value.data_path,
            mode=OnboardingMode(value.initial_mode),
            safety_confirmed=value.safety_confirmed,
            capture_test_confirmed=value.capture_test_confirmed,
            data_path_confirmed=value.data_path_confirmed,
            character_name=value.character_name,
            scoring_template_kind=value.scoring_template_kind,
            display_mode=value.display_mode,
            resolution=value.resolution,
        )
        self._submit(_Operation.COMPLETE, lambda: self._service.complete(command))

    def _discover_monitors(self) -> tuple[MonitorInfo, ...]:
        backend = self._backend_factory()
        try:
            return backend.list_monitors()
        finally:
            backend.close()

    def _test_monitor(
        self, expected: MonitorInfo, resolution: str = DEFAULT_RESOLUTION
    ) -> _CaptureTestOutcome:
        backend = self._backend_factory()
        try:
            available = {item.index: item for item in backend.list_monitors()}
            current = available.get(expected.index)
            if current is None or current.stable_key != expected.stable_key:
                raise RuntimeError("顯示器配置已變更, 請重新選擇顯示器")
            if (current.width, current.height) != RESOLUTIONS[resolution]:
                raise RuntimeError(
                    f"所選 {resolution} 與螢幕 {current.width}x{current.height} 不符, "
                    "請調整解析度選項"
                )
            with backend.capture_monitor(current.index) as frame:
                # Copy only the three bounded classifier probes.  The full monitor
                # frame is released before OCR begins and no array leaves this worker.
                rois = {
                    PAGE_HEADER_ROI.name: frame.copy_roi(PAGE_HEADER_ROI),
                    ENHANCEMENT_STATE_ROI.name: frame.copy_roi(ENHANCEMENT_STATE_ROI),
                    MATERIAL_CTA_ROI.name: frame.copy_roi(MATERIAL_CTA_ROI),
                }
            backend.audit.assert_all_released()
            header = rois[PAGE_HEADER_ROI.name]
            brightness = float(np.mean(header))
            variance = float(np.var(header))
            try:
                classification = PageClassifier(self._ocr_factory()).classify_rois(
                    rois,
                    source_file="onboarding-monitor-test",
                )
            except (ImportError, OcrBackendError) as exc:
                raise RuntimeError("本機 OCR 模型無法使用; 請重新安裝完整封裝後再測試。") from exc
            finally:
                rois.clear()
            return _CaptureTestOutcome(
                monitor=current,
                page_type=classification.page_type,
                confidence=classification.confidence,
                brightness=brightness,
                variance=variance,
            )
        finally:
            backend.close()

    def _submit(
        self,
        operation: _Operation,
        work: Callable[[], object],
        *,
        identifier: str | None = None,
        resolution: str | None = None,
    ) -> None:
        if self._closing:
            return

        def run() -> object:
            try:
                return _JobResult(
                    operation, payload=work(), identifier=identifier, resolution=resolution
                )
            except Exception as exc:  # UI boundary: convert local failures to a safe DTO.
                return _JobResult(
                    operation,
                    identifier=identifier,
                    resolution=resolution,
                    error=f"{type(exc).__name__}: {exc}",
                )

        future = cast(Future[object], self._executor.submit(run))
        self._futures.add(future)

        def finished(item: Future[object]) -> None:
            self._futures.discard(item)
            if not self._closing:
                self._job_completed.emit(item.result())

        future.add_done_callback(finished)

    @Slot(object)
    def _handle_result(self, value: object) -> None:
        if self._closing or not isinstance(value, _JobResult):
            return
        if value.error is not None:
            if value.operation is _Operation.DISCOVER:
                self._wizard.set_monitor_discovery_error(f"顯示器偵測失敗: {value.error}")
            elif value.operation is _Operation.TEST and value.identifier is not None:
                self._wizard.set_capture_test_result(
                    value.identifier,
                    False,
                    f"被動擷取測試失敗: {value.error}",
                    value.resolution,
                )
            else:
                self._wizard.show_error(f"首次設定失敗: {value.error}")
            return

        if value.operation is _Operation.DISCOVER:
            monitors = value.payload
            if not isinstance(monitors, tuple) or not all(
                isinstance(item, MonitorInfo) for item in monitors
            ):
                self._wizard.set_monitor_discovery_error("顯示器偵測結果格式不正確。")
                return
            self._monitors = {f"monitor-{item.index}": item for item in monitors}
            self._wizard.set_monitors(_monitor_choices(monitors))
        elif value.operation is _Operation.TEST and value.identifier is not None:
            outcome = value.payload
            if not isinstance(outcome, _CaptureTestOutcome):
                self._wizard.set_capture_test_result(
                    value.identifier,
                    False,
                    "被動擷取測試沒有回傳有效結果。",
                )
                return
            self._wizard.set_capture_test_result(
                value.identifier,
                _capture_test_is_usable(outcome),
                _capture_test_message(outcome),
                value.resolution,
            )
        elif value.operation is _Operation.COMPLETE:
            if not isinstance(value.payload, OnboardingState) or not value.payload.completed:
                self._wizard.show_error("首次設定沒有產生有效的完成狀態。")
                return
            self._wizard.complete_success("首次設定已完成, 正在進入總覽。")


def _monitor_choices(monitors: Sequence[MonitorInfo]) -> tuple[MonitorChoice, ...]:
    horizontal = sorted(monitors, key=lambda item: (item.left, item.top, item.index))
    positions: dict[int, str] = {}
    if len(horizontal) == 1:
        positions[horizontal[0].index] = "唯一"
    elif len({item.left for item in horizontal}) > 1:
        for order_index, monitor in enumerate(horizontal):
            if order_index == 0:
                positions[monitor.index] = "左側"
            elif order_index == len(horizontal) - 1:
                positions[monitor.index] = "右側"
            else:
                positions[monitor.index] = "中間"
    else:
        vertical = sorted(monitors, key=lambda item: (item.top, item.index))
        for order_index, monitor in enumerate(vertical):
            positions[monitor.index] = "上方" if order_index == 0 else "下方"

    choices: list[MonitorChoice] = []
    for monitor in monitors:
        position_label = positions.get(monitor.index, "")
        primary = " · 主要螢幕" if monitor.is_primary else ""
        compatibility = "" if monitor.is_supported_size else " · 不支援此解析度"
        choices.append(
            MonitorChoice(
                f"monitor-{monitor.index}",
                (
                    f"{position_label}螢幕 (Windows 顯示器 {monitor.index}){primary} · "
                    f"{monitor.width}x{monitor.height}{compatibility}"
                ),
                width=monitor.width,
                height=monitor.height,
                is_primary=monitor.is_primary,
            )
        )
    return tuple(choices)


def _capture_test_message(outcome: _CaptureTestOutcome) -> str:
    page_labels = {
        PageType.CHARACTER_ECHO_OVERVIEW: "角色聲骸總覽",
        PageType.ECHO_SELECTION: "聲骸選擇",
        PageType.ECHO_ENHANCEMENT: "聲骸強化",
        PageType.ENHANCEMENT_MATERIAL_SELECTION: "強化素材選擇",
    }
    label = page_labels.get(outcome.page_type)
    released = "完整畫面已立即釋放, 沒有寫入硬碟。"
    if not _capture_test_is_usable(outcome):
        return (
            "擷取到的測試區域幾乎全黑或沒有畫面細節。"
            "請確認遊戲沒有最小化, 或改選另一個螢幕後再測試。"
            f"{released}"
        )
    if label is not None:
        return f"已選到遊戲畫面: 辨識為「{label}」({outcome.confidence:.0%})。{released}"
    return (
        "可以擷取這個螢幕, 但目前未看到鳴潮聲骸頁面。"
        f"請把遊戲切到聲骸強化頁再測試; 也可以先進入工具。{released}"
    )


def _capture_test_is_usable(outcome: _CaptureTestOutcome) -> bool:
    return outcome.brightness >= 2.0 and outcome.variance >= 4.0


__all__ = ["CaptureBackendFactory", "OcrEngineFactory", "OnboardingController"]
