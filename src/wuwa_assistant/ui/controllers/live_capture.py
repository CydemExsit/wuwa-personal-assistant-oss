"""Qt signal bridge for passive monitor capture and local ROI recognition.

The controller never receives a full monitor frame or ROI array.  The existing
``LiveCaptureRuntime`` owns capture and OCR worker threads; this bridge only converts
its array-free callback DTOs into immutable, Qt-safe view data.  Monitor discovery uses
a short-lived backend created, listed, and closed entirely inside one worker thread so
MSS thread affinity is preserved.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from enum import StrEnum
from math import isfinite
from threading import RLock, Thread, current_thread
from time import monotonic
from typing import Protocol, cast

from PySide6.QtCore import QObject, Signal, SignalInstance

from wuwa_assistant.capture import (
    CaptureBackend,
    CaptureConfig,
    CaptureStatus,
    MonitorInfo,
    MssMonitorCaptureBackend,
)
from wuwa_assistant.display import DEFAULT_RESOLUTION
from wuwa_assistant.live.analyzer import LiveEchoRoiAnalyzer
from wuwa_assistant.live.coordinator import RoiAnalyzer
from wuwa_assistant.live.dto import (
    GenerationToken,
    JsonValue,
    LiveRecognitionResult,
    LiveRuntimeError,
    LiveStatus,
    LiveStatusUpdate,
)
from wuwa_assistant.live.runtime import LiveCaptureRuntime
from wuwa_assistant.ocr.rapidocr_engine import RapidOcrEngine


@dataclass(frozen=True, slots=True)
class LiveCaptureSettings:
    """Validated values supplied by the user when recognition is started."""

    monitor_index: int
    sample_hz: float = 5.0
    stable_duration_seconds: float = 0.60
    confidence_threshold: float = 0.85
    minimum_stable_samples: int = 3
    manual_mode: bool = False
    resolution: str = DEFAULT_RESOLUTION

    def __post_init__(self) -> None:
        # Reuse the backend contract so UI and runtime validation cannot drift.
        CaptureConfig(self.monitor_index, self.sample_hz, self.resolution)
        if not isinstance(self.manual_mode, bool):
            raise ValueError("manual_mode must be a boolean")
        if (
            isinstance(self.stable_duration_seconds, bool)
            or not isfinite(self.stable_duration_seconds)
            or not 0.50 <= self.stable_duration_seconds <= 0.75
        ):
            raise ValueError("stable_duration_seconds must be within 0.50..0.75")
        if (
            isinstance(self.confidence_threshold, bool)
            or not isfinite(self.confidence_threshold)
            or not 0.0 <= self.confidence_threshold <= 1.0
        ):
            raise ValueError("confidence_threshold must be within [0, 1]")
        if isinstance(self.minimum_stable_samples, bool) or self.minimum_stable_samples < 2:
            raise ValueError("minimum_stable_samples must be at least 2")


@dataclass(frozen=True, slots=True)
class MonitorDescriptor:
    """Pixel-free physical-monitor metadata suitable for a Qt combo box."""

    identifier: str
    index: int
    label: str
    left: int
    top: int
    width: int
    height: int
    is_primary: bool
    is_compatible: bool

    @classmethod
    def from_monitor(
        cls,
        monitor: MonitorInfo,
        *,
        position_label: str = "中央",
    ) -> MonitorDescriptor:
        parts = [f"{position_label}螢幕 (顯示器 {monitor.index})"]
        if monitor.name:
            parts.append(monitor.name)
        parts.append(f"{monitor.width}x{monitor.height}")
        if monitor.is_primary:
            parts.append("Windows 主要螢幕")
        if not monitor.is_supported_size:
            parts.append("支援 1920x1080 / 2560x1440")
        return cls(
            identifier=f"monitor-{monitor.index}",
            index=monitor.index,
            label=" · ".join(parts),
            left=monitor.left,
            top=monitor.top,
            width=monitor.width,
            height=monitor.height,
            is_primary=monitor.is_primary,
            is_compatible=monitor.is_supported_size,
        )

    @classmethod
    def from_monitors(
        cls,
        monitors: Sequence[MonitorInfo],
    ) -> tuple[MonitorDescriptor, ...]:
        """Build labels relative to the user's current horizontal layout."""

        if not monitors:
            return ()
        horizontal_centers = tuple(monitor.left * 2 + monitor.width for monitor in monitors)
        leftmost = min(horizontal_centers)
        rightmost = max(horizontal_centers)
        descriptors: list[MonitorDescriptor] = []
        for monitor, center in zip(monitors, horizontal_centers, strict=True):
            if leftmost == rightmost:
                position = "中央"
            elif center == leftmost:
                position = "左側"
            elif center == rightmost:
                position = "右側"
            else:
                position = "中央"
            descriptors.append(cls.from_monitor(monitor, position_label=position))
        return tuple(descriptors)


@dataclass(frozen=True, slots=True)
class CaptureStatusView:
    """One user-facing capture state without worker-owned objects."""

    status: CaptureStatus
    reason: str
    generation: int | None = None
    source_sequence: int | None = None


@dataclass(frozen=True, slots=True)
class RecognitionPayload:
    """JSON-only recognition result emitted to widgets on the Qt event loop."""

    generation: int
    source_sequence: int
    captured_at: str
    completed_at: float
    packet_kind: str
    state_fingerprint: str
    page_type: str
    relevant: bool
    confidence: float | None
    observation: Mapping[str, JsonValue]
    unchanged: bool = False

    @classmethod
    def from_runtime(cls, result: LiveRecognitionResult) -> RecognitionPayload:
        # AnalysisResult already validates and deep-copies JSON values.  Copy its top
        # level once more so UI code cannot mutate the coordinator-owned mapping.
        observation = cast(dict[str, JsonValue], deepcopy(dict(result.analysis.observation)))
        return cls(
            generation=result.generation,
            source_sequence=result.source_sequence,
            captured_at=result.captured_at,
            completed_at=result.completed_at,
            packet_kind=result.packet_kind.value,
            state_fingerprint=result.state_fingerprint,
            page_type=result.analysis.page_type,
            relevant=result.analysis.relevant,
            confidence=result.analysis.confidence,
            unchanged=result.analysis.unchanged,
            observation=observation,
        )


class ControllerErrorCode(StrEnum):
    MONITOR_ENUMERATION_FAILED = "monitor_enumeration_failed"
    START_REJECTED = "start_rejected"
    START_FAILED = "start_failed"
    STOP_FAILED = "stop_failed"
    CAPTURE_FAILED = "capture_failed"
    OCR_UNAVAILABLE = "ocr_unavailable"


@dataclass(frozen=True, slots=True)
class CaptureControllerError:
    """Actionable error data; it never contains exception objects or image data."""

    code: ControllerErrorCode
    title: str
    message: str
    error_type: str
    recoverable: bool
    generation: int | None = None
    source_sequence: int | None = None


class _RuntimeControl(Protocol):
    @property
    def is_running(self) -> bool: ...

    def start(self) -> GenerationToken: ...

    def stop(self, *, timeout: float = 8.0) -> bool: ...

    def request_recheck(self) -> int | None: ...


type _StatusCallback = Callable[[LiveStatusUpdate], None]
type _ResultCallback = Callable[[LiveRecognitionResult], None]
type _ErrorCallback = Callable[[LiveRuntimeError], None]


class RuntimeFactory(Protocol):
    def __call__(
        self,
        backend: CaptureBackend,
        config: CaptureConfig,
        analyzer: RoiAnalyzer,
        *,
        stable_duration_seconds: float,
        minimum_stable_samples: int,
        confidence_threshold: float,
        manual_mode: bool,
        on_status: _StatusCallback,
        on_result: _ResultCallback,
        on_error: _ErrorCallback,
    ) -> _RuntimeControl: ...


type BackendFactory = Callable[[], CaptureBackend]
type AnalyzerFactory = Callable[[], RoiAnalyzer]

_APPLICATION_LOGGER = logging.getLogger("wuwa_assistant")
_LOG_PAGE_TYPES = frozenset(
    {
        "character_echo_overview",
        "echo_selection",
        "echo_enhancement",
        "enhancement_material_selection",
        "unknown",
    }
)


class LiveCaptureController(QObject):
    """Manual, restartable Qt facade over passive live recognition.

    ``start`` does not capture or perform OCR on the caller thread.  It creates only
    lazy objects and asks ``LiveCaptureRuntime`` to start its capture and OCR workers.
    ``stop`` is asynchronous so joining those workers cannot freeze the interface.
    ``shutdown`` is the explicit bounded wait used while the application is closing.
    """

    monitors_changed = Signal(object)
    status_changed = Signal(object)
    recognition_ready = Signal(object)
    error_occurred = Signal(object)
    running_changed = Signal(bool)

    def __init__(
        self,
        *,
        backend_factory: BackendFactory | None = None,
        analyzer_factory: AnalyzerFactory | None = None,
        runtime_factory: RuntimeFactory | None = None,
        logger: logging.Logger | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._backend_factory = backend_factory or MssMonitorCaptureBackend
        self._analyzer_factory = analyzer_factory or _default_analyzer_factory
        self._runtime_factory = runtime_factory or _default_runtime_factory
        self._logger = logger or _APPLICATION_LOGGER
        self._lock = RLock()
        self._runtime: _RuntimeControl | None = None
        self._active_run_id: int | None = None
        self._run_counter = 0
        self._running = False
        self._status = CaptureStatus.NOT_ENABLED
        self._monitor_thread: Thread | None = None
        self._stop_thread: Thread | None = None
        self._shutdown = False
        self._result_floor = -1

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def status(self) -> CaptureStatus:
        with self._lock:
            return self._status

    def refresh_monitors(self) -> bool:
        """Enumerate physical monitors asynchronously.

        The backend factory, ``list_monitors``, and ``close`` all execute in the same
        worker.  A second request is ignored while discovery is already in progress.
        """

        with self._lock:
            if self._shutdown:
                return False
            if self._monitor_thread is not None and self._monitor_thread.is_alive():
                return False
            worker = Thread(
                target=self._monitor_worker,
                name="wuwa-monitor-discovery",
                daemon=False,
            )
            self._monitor_thread = worker
        worker.start()
        self._logger.info("monitor_enumeration_started")
        return True

    def request_recheck(self) -> bool:
        """Ask the existing worker for fresh crops; never block Qt on OCR."""
        with self._lock:
            runtime = self._runtime
            if self._shutdown or not self._running or runtime is None:
                return False
        cutoff = runtime.request_recheck()
        if cutoff is None:
            return False
        with self._lock:
            if runtime is not self._runtime:
                return False
            self._result_floor = max(self._result_floor, cutoff)
        self._logger.info("capture_recheck_requested cutoff_sequence=%d", cutoff)
        self._set_status(CaptureStatus.WAITING_FOR_ECHO_PAGE, "重新辨識中, 請保持聲骸清單畫面。")
        return True

    def accepts_recognition(self, value: RecognitionPayload) -> bool:
        """Fence Qt signals emitted before a manual recheck but delivered later."""
        with self._lock:
            return (
                value.generation == self._active_run_id
                and value.source_sequence > self._result_floor
            )

    def start(self, settings: LiveCaptureSettings) -> bool:
        """Start one manually requested capture generation without blocking Qt."""

        with self._lock:
            previous_runtime = self._runtime
        # Runtime callbacks acquire this controller's lock. Never query runtime
        # state while holding it: the capture thread may own the runtime lock
        # and be publishing a status at the same time as Start/Stop on Qt.
        previous_running = previous_runtime is not None and previous_runtime.is_running
        with self._lock:
            if self._shutdown:
                error = _controller_error(
                    ControllerErrorCode.START_REJECTED,
                    "無法開始辨識",
                    "應用程式正在關閉。",
                    "ControllerShuttingDown",
                    recoverable=False,
                )
            elif self._stop_thread is not None and self._stop_thread.is_alive():
                error = _controller_error(
                    ControllerErrorCode.START_REJECTED,
                    "請稍候",
                    "上一個辨識工作仍在停止中。",
                    "StopInProgress",
                )
            elif previous_running:
                error = _controller_error(
                    ControllerErrorCode.START_REJECTED,
                    "辨識已啟用",
                    "請先停止目前的辨識工作。",
                    "AlreadyRunning",
                )
            else:
                error = None
                self._result_floor = -1
                self._run_counter += 1
                run_id = self._run_counter
                self._active_run_id = run_id

        if error is not None:
            self._log_controller_error(error)
            self.error_occurred.emit(error)
            return False

        self._logger.info(
            "capture_start_requested monitor_index=%d sample_hz=%.2f "
            "stable_wait_ms=%d confidence_threshold=%.3f minimum_stable_samples=%d",
            settings.monitor_index,
            settings.sample_hz,
            round(settings.stable_duration_seconds * 1000),
            settings.confidence_threshold,
            settings.minimum_stable_samples,
        )
        self._set_status(
            CaptureStatus.STARTING,
            "正在啟動本機被動擷取與離線 OCR。",
        )
        try:
            backend = self._backend_factory()
            analyzer = self._analyzer_factory()
            runtime = self._runtime_factory(
                backend,
                CaptureConfig(settings.monitor_index, settings.sample_hz, settings.resolution),
                analyzer,
                stable_duration_seconds=settings.stable_duration_seconds,
                minimum_stable_samples=settings.minimum_stable_samples,
                confidence_threshold=settings.confidence_threshold,
                manual_mode=settings.manual_mode,
                on_status=lambda update: self._handle_runtime_status(run_id, update),
                on_result=lambda result: self._handle_runtime_result(run_id, result),
                on_error=lambda runtime_error: self._handle_runtime_error(run_id, runtime_error),
            )
            with self._lock:
                if self._shutdown or self._active_run_id != run_id:
                    raise RuntimeError("capture start was canceled")
                self._runtime = runtime
            token = runtime.start()
        except Exception as exc:
            with self._lock:
                if self._active_run_id == run_id:
                    self._runtime = None
                    self._active_run_id = None
            self._set_running(False)
            self._set_status(CaptureStatus.ERROR, "無法啟動被動擷取。")
            payload = _exception_error(
                ControllerErrorCode.START_FAILED,
                "無法開始辨識",
                exc,
            )
            self._log_controller_error(payload)
            self.error_occurred.emit(payload)
            return False

        self._logger.info(
            "capture_runtime_started generation=%d monitor_index=%d",
            token.value,
            settings.monitor_index,
        )
        self._set_running(True)
        return True

    def stop(self) -> bool:
        """Request asynchronous stop; returns before worker joins complete."""

        with self._lock:
            if self._stop_thread is not None and self._stop_thread.is_alive():
                return False
            runtime = self._runtime
            run_id = self._active_run_id
            if runtime is None:
                self._runtime = None
                self._active_run_id = None
                worker = None
            else:
                worker = Thread(
                    target=self._stop_worker,
                    args=(run_id, runtime),
                    name="wuwa-live-stop",
                    daemon=False,
                )
                self._stop_thread = worker

        self._logger.info(
            "capture_stop_requested generation=%s runtime_active=%s",
            _optional_log_int(run_id),
            worker is not None,
        )
        self._set_status(CaptureStatus.STOPPING, "正在停止辨識並保存已確認狀態。")
        if worker is None:
            self._set_running(False)
            self._set_status(CaptureStatus.STOPPED, "辨識已停止。")
            self._logger.info(
                "capture_stop_completed generation=%s success=true",
                _optional_log_int(run_id),
            )
            return False
        worker.start()
        return True

    def shutdown(self, *, timeout: float = 8.0) -> bool:
        """Stop workers with a bounded wait while the application is closing."""

        if timeout < 0:
            raise ValueError("timeout cannot be negative")
        deadline = monotonic() + timeout
        with self._lock:
            self._shutdown = True
            runtime = self._runtime
            stop_thread = self._stop_thread
            monitor_thread = self._monitor_thread

        stopped = True
        if stop_thread is not None and stop_thread is not current_thread():
            stop_thread.join(max(0.0, deadline - monotonic()))
            stopped = stopped and not stop_thread.is_alive()
        with self._lock:
            runtime = self._runtime
        if runtime is not None and runtime.is_running:
            stopped = runtime.stop(timeout=max(0.0, deadline - monotonic())) and stopped

        if monitor_thread is not None and monitor_thread is not current_thread():
            monitor_thread.join(max(0.0, deadline - monotonic()))
            stopped = stopped and not monitor_thread.is_alive()

        with self._lock:
            if stopped:
                self._runtime = None
                self._active_run_id = None
                self._running = False
                self._status = CaptureStatus.STOPPED
        self._logger.info("capture_shutdown_completed success=%s", stopped)
        return stopped

    def _monitor_worker(self) -> None:
        backend: CaptureBackend | None = None
        descriptors: tuple[MonitorDescriptor, ...] | None = None
        try:
            backend = self._backend_factory()
            monitors = backend.list_monitors()
            descriptors = MonitorDescriptor.from_monitors(monitors)
            self._logger.info("monitor_enumeration_succeeded count=%d", len(monitors))
            for monitor in monitors:
                self._logger.info(
                    "monitor_detected index=%d left=%d top=%d width=%d height=%d "
                    "primary=%s compatible=%s",
                    monitor.index,
                    monitor.left,
                    monitor.top,
                    monitor.width,
                    monitor.height,
                    monitor.is_primary,
                    monitor.is_supported_size,
                )
        except Exception as exc:
            payload = _exception_error(
                ControllerErrorCode.MONITOR_ENUMERATION_FAILED,
                "無法讀取顯示器清單",
                exc,
            )
            self._log_controller_error(payload)
            self._emit_if_active(
                self.error_occurred,
                payload,
            )
        finally:
            if backend is not None:
                try:
                    backend.close()
                except Exception as exc:
                    payload = _exception_error(
                        ControllerErrorCode.MONITOR_ENUMERATION_FAILED,
                        "無法關閉顯示器偵測工作",
                        exc,
                    )
                    self._log_controller_error(payload)
                    self._emit_if_active(
                        self.error_occurred,
                        payload,
                    )
        if descriptors is not None:
            self._emit_if_active(self.monitors_changed, descriptors)

    def _stop_worker(self, run_id: int | None, runtime: _RuntimeControl) -> None:
        try:
            stopped = runtime.stop(timeout=30.0)
            if not stopped:
                raise TimeoutError("capture or OCR worker did not stop within 30 seconds")
        except Exception as exc:
            payload = _exception_error(
                ControllerErrorCode.STOP_FAILED,
                "辨識未能正常停止",
                exc,
                generation=run_id,
            )
            with self._lock:
                # No join is in progress now. Keep the runtime reference so a
                # new Start still refuses live native work, but allow another
                # Stop after the failure instead of retaining a phantom join.
                self._stop_thread = None
            self._log_controller_error(payload)
            self._emit_if_active(
                self.error_occurred,
                payload,
            )
            self._set_status(CaptureStatus.ERROR, "停止辨識時發生錯誤。")
            return

        with self._lock:
            if self._runtime is runtime:
                self._runtime = None
                self._active_run_id = None
            self._stop_thread = None

        self._set_running(False)
        self._set_status(CaptureStatus.STOPPED, "辨識已停止。")
        self._logger.info(
            "capture_stop_completed generation=%s success=true",
            _optional_log_int(run_id),
        )

    def _handle_runtime_status(self, run_id: int, update: LiveStatusUpdate) -> None:
        if not self._is_current_run(run_id):
            return
        with self._lock:
            if self._stop_thread is not None and self._stop_thread.is_alive():
                # Coordinator STOPPED invalidates jobs before native OCR and
                # backend cleanup have joined. Only the stop worker may mark
                # the UI restartable after those joins actually complete.
                return
        mapped = _CAPTURE_STATUS_BY_LIVE_STATUS[update.status]
        self._set_status(
            mapped,
            _localize_status_reason(update),
            generation=run_id,
            source_sequence=update.source_sequence,
        )
        if update.status is LiveStatus.STOPPED:
            self._set_running(False)

    def _handle_runtime_result(self, run_id: int, result: LiveRecognitionResult) -> None:
        if not self._is_current_run(run_id):
            return
        self._logger.info(
            "recognition_result generation=%d sequence=%d packet_kind=%s "
            "page_type=%s relevant=%s confidence=%s",
            result.generation,
            result.source_sequence,
            result.packet_kind.value,
            _allowlisted_log_token(result.analysis.page_type, _LOG_PAGE_TYPES),
            result.analysis.relevant,
            _optional_log_float(result.analysis.confidence),
        )
        if result.analysis.unchanged:
            self._logger.info(
                "recognition_semantic_unchanged generation=%d sequence=%d",
                result.generation,
                result.source_sequence,
            )
        # Each runtime starts its local generation at one. Use the controller's
        # monotonically increasing run ID so queued Qt results from the previous
        # mode cannot be ingested into a newly started capture session.
        self.recognition_ready.emit(
            replace(RecognitionPayload.from_runtime(result), generation=run_id)
        )

    def _handle_runtime_error(self, run_id: int, error: LiveRuntimeError) -> None:
        if not self._is_current_run(run_id):
            return
        if error.error_type == "OcrBackendError":
            payload = CaptureControllerError(
                code=ControllerErrorCode.OCR_UNAVAILABLE,
                title="本機 OCR 無法使用",
                message=(
                    "RapidOCR 模型缺少或無法載入; 本工具不會自動下載模型。"
                    f" 詳細資訊: {error.message}"
                ),
                error_type=error.error_type,
                recoverable=False,
                generation=run_id,
                source_sequence=error.source_sequence,
            )
        else:
            payload = CaptureControllerError(
                code=ControllerErrorCode.CAPTURE_FAILED,
                title="辨識工作發生錯誤",
                message=error.message,
                error_type=error.error_type,
                recoverable=True,
                generation=run_id,
                source_sequence=error.source_sequence,
            )
        self._logger.error(
            "capture_runtime_error error_type=%s generation=%d sequence=%s code=%s",
            error.error_type,
            error.generation,
            _optional_log_int(error.source_sequence),
            payload.code.value,
        )
        self.error_occurred.emit(payload)

    def _is_current_run(self, run_id: int) -> bool:
        with self._lock:
            return not self._shutdown and self._active_run_id == run_id

    def _set_running(self, running: bool) -> None:
        with self._lock:
            if self._running == running:
                return
            self._running = running
            should_emit = not self._shutdown
        if should_emit:
            self.running_changed.emit(running)

    def _set_status(
        self,
        status: CaptureStatus,
        reason: str,
        *,
        generation: int | None = None,
        source_sequence: int | None = None,
    ) -> None:
        with self._lock:
            previous = self._status
            self._status = status
            should_emit = not self._shutdown
        if previous is not status:
            self._logger.info(
                "capture_status_transition previous=%s current=%s generation=%s sequence=%s",
                previous.value,
                status.value,
                _optional_log_int(generation),
                _optional_log_int(source_sequence),
            )
        if should_emit:
            self.status_changed.emit(
                CaptureStatusView(
                    status=status,
                    reason=reason,
                    generation=generation,
                    source_sequence=source_sequence,
                )
            )

    def _emit_if_active(self, signal: SignalInstance, payload: object) -> None:
        with self._lock:
            should_emit = not self._shutdown
        if should_emit:
            signal.emit(payload)

    def _log_controller_error(self, error: CaptureControllerError) -> None:
        self._logger.error(
            "capture_controller_error code=%s error_type=%s recoverable=%s "
            "generation=%s sequence=%s",
            error.code.value,
            error.error_type,
            error.recoverable,
            _optional_log_int(error.generation),
            _optional_log_int(error.source_sequence),
        )


_CAPTURE_STATUS_BY_LIVE_STATUS: Mapping[LiveStatus, CaptureStatus] = {
    LiveStatus.INACTIVE: CaptureStatus.NOT_ENABLED,
    LiveStatus.WAITING_FOR_PAGE: CaptureStatus.WAITING_FOR_ECHO_PAGE,
    LiveStatus.WAITING_FOR_REQUEST: CaptureStatus.WAITING_FOR_REQUEST,
    LiveStatus.TRANSITIONING: CaptureStatus.TRANSITIONING,
    LiveStatus.STABLE: CaptureStatus.STABLE,
    LiveStatus.RECOGNIZING: CaptureStatus.RECOGNIZING,
    LiveStatus.LOW_CONFIDENCE: CaptureStatus.LOW_CONFIDENCE,
    LiveStatus.STOPPED: CaptureStatus.STOPPED,
    LiveStatus.ERROR: CaptureStatus.ERROR,
}

_STATUS_MESSAGES: Mapping[LiveStatus, str] = {
    LiveStatus.INACTIVE: "辨識未啟用。",
    LiveStatus.WAITING_FOR_PAGE: "等待聲骸清單畫面; 培養期間暫停讀取, 返回清單後自動恢復。",
    LiveStatus.WAITING_FOR_REQUEST: "目前未擷取畫面; 按「辨識目前畫面」讀取下一顆聲骸。",
    LiveStatus.TRANSITIONING: "畫面切換中, 等待辨識區域穩定。",
    LiveStatus.STABLE: "畫面已穩定。",
    LiveStatus.RECOGNIZING: "正在執行本機離線 OCR。",
    LiveStatus.LOW_CONFIDENCE: "辨識信心不足, 需要人工確認。",
    LiveStatus.STOPPED: "辨識已停止。",
    LiveStatus.ERROR: "辨識發生錯誤。",
}


def _localize_status_reason(update: LiveStatusUpdate) -> str:
    # Coordinator reasons are internal diagnostic tokens in English.  They remain
    # available to metadata instrumentation but must not leak into the Traditional
    # Chinese status copy shown in the application.
    if update.reason == "manual request timed out":
        return "本次未取得穩定結果, 已停止擷取; 請停在聲骸清單後再按「辨識目前畫面」。"
    if update.reason == "manual request needs echo selection page":
        return "目前不是聲骸清單, 已停止擷取; 返回清單後再按「辨識目前畫面」。"
    if update.reason == "background recovery waiting for matching text from a fresh frame":
        return "已讀取聲骸文字, 正在再次確認; 結果一致後會自動更新。"
    if update.reason == "selection identity stable; checking text despite background animation":
        return "正在重新讀取畫面, 確認聲骸內容。"
    return _STATUS_MESSAGES[update.status]


def _default_analyzer_factory() -> RoiAnalyzer:
    # RapidOcrEngine resolves and initializes bundled local models only on the first
    # worker-thread recognition request.  Its fail-closed path never downloads models.
    return LiveEchoRoiAnalyzer(RapidOcrEngine(), selection_only=True)


def _default_runtime_factory(
    backend: CaptureBackend,
    config: CaptureConfig,
    analyzer: RoiAnalyzer,
    *,
    stable_duration_seconds: float,
    minimum_stable_samples: int,
    confidence_threshold: float,
    manual_mode: bool = False,
    on_status: _StatusCallback,
    on_result: _ResultCallback,
    on_error: _ErrorCallback,
) -> _RuntimeControl:
    return LiveCaptureRuntime(
        backend,
        config,
        analyzer,
        stable_duration_seconds=stable_duration_seconds,
        minimum_stable_samples=minimum_stable_samples,
        confidence_threshold=confidence_threshold,
        selection_only=True,
        manual_mode=manual_mode,
        on_status=on_status,
        on_result=on_result,
        on_error=on_error,
    )


def _controller_error(
    code: ControllerErrorCode,
    title: str,
    message: str,
    error_type: str,
    *,
    recoverable: bool = True,
    generation: int | None = None,
    source_sequence: int | None = None,
) -> CaptureControllerError:
    return CaptureControllerError(
        code=code,
        title=title,
        message=message,
        error_type=error_type,
        recoverable=recoverable,
        generation=generation,
        source_sequence=source_sequence,
    )


def _exception_error(
    code: ControllerErrorCode,
    title: str,
    exc: Exception,
    *,
    generation: int | None = None,
) -> CaptureControllerError:
    return _controller_error(
        code,
        title,
        str(exc) or type(exc).__name__,
        type(exc).__name__,
        generation=generation,
    )


def _optional_log_int(value: int | None) -> str:
    return "none" if value is None else str(value)


def _optional_log_float(value: float | None) -> str:
    return "none" if value is None else f"{value:.5f}"


def _allowlisted_log_token(value: str, allowed: frozenset[str]) -> str:
    return value if value in allowed else "unrecognized"


__all__ = [
    "AnalyzerFactory",
    "BackendFactory",
    "CaptureControllerError",
    "CaptureStatusView",
    "ControllerErrorCode",
    "LiveCaptureController",
    "LiveCaptureSettings",
    "MonitorDescriptor",
    "RecognitionPayload",
    "RuntimeFactory",
]
