from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import fields, is_dataclass
from queue import Queue
from threading import Event, Lock, Thread, get_ident
from time import monotonic, sleep
from typing import Any, cast

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from wuwa_assistant.capture import (
    CaptureBackend,
    CaptureConfig,
    CapturedFrame,
    FrameLeaseAudit,
    MonitorInfo,
    RoiPacket,
    RoiPacketKind,
)
from wuwa_assistant.live import (
    AnalysisResult,
    GenerationToken,
    LiveRecognitionResult,
    LiveRuntimeError,
    LiveStatus,
    LiveStatusUpdate,
    RoiAnalyzer,
)
from wuwa_assistant.live.analyzer import LiveEchoRoiAnalyzer
from wuwa_assistant.ocr.rapidocr_engine import RapidOcrEngine
from wuwa_assistant.ui.controllers.live_capture import (
    CaptureControllerError,
    CaptureStatusView,
    ControllerErrorCode,
    LiveCaptureController,
    LiveCaptureSettings,
    MonitorDescriptor,
    RecognitionPayload,
)

type StatusCallback = Callable[[LiveStatusUpdate], None]
type ResultCallback = Callable[[LiveRecognitionResult], None]
type ErrorCallback = Callable[[LiveRuntimeError], None]


class DiscoveryBackend:
    def __init__(
        self,
        calls: Queue[tuple[str, int]],
        *,
        list_gate: Event | None = None,
    ) -> None:
        self._calls = calls
        self._list_gate = list_gate
        self._audit = FrameLeaseAudit()
        self._calls.put(("created", get_ident()))

    @property
    def audit(self) -> FrameLeaseAudit:
        return self._audit

    def list_monitors(self) -> tuple[MonitorInfo, ...]:
        if self._list_gate is not None:
            assert self._list_gate.wait(3)
        self._calls.put(("listed", get_ident()))
        return (
            MonitorInfo(1, 0, 0, 2560, 1440, is_primary=True, name="Display 1"),
            MonitorInfo(2, -1920, 0, 1920, 1080, name="Display 2"),
        )

    def capture_monitor(self, monitor_index: int) -> CapturedFrame:
        del monitor_index
        raise AssertionError("monitor discovery must not capture a frame")

    def close(self) -> None:
        self._calls.put(("closed", get_ident()))


class IdleBackend:
    def __init__(self) -> None:
        self._audit = FrameLeaseAudit()

    @property
    def audit(self) -> FrameLeaseAudit:
        return self._audit

    def list_monitors(self) -> tuple[MonitorInfo, ...]:
        return (MonitorInfo(1, 0, 0, 2560, 1440),)

    def capture_monitor(self, monitor_index: int) -> CapturedFrame:
        del monitor_index
        raise AssertionError("stub runtime must not request a frame")

    def close(self) -> None:
        return None


class NoopAnalyzer:
    def analyze(self, packet: RoiPacket) -> AnalysisResult:
        del packet
        raise AssertionError("stub runtime must not invoke its analyzer")


class StubRuntime:
    def __init__(
        self,
        on_status: StatusCallback,
        on_result: ResultCallback,
        on_error: ErrorCallback,
        *,
        emit_result: bool,
        emitted_error: LiveRuntimeError | None,
    ) -> None:
        self._on_status = on_status
        self._on_result = on_result
        self._on_error = on_error
        self._emit_result = emit_result
        self._emitted_error = emitted_error
        self._lock = Lock()
        self._running = False
        self.stop_gate = Event()
        self.stop_entered = Event()
        self.callback_thread_id: int | None = None
        self._callback_thread: Thread | None = None
        self.recheck_count = 0

    def request_recheck(self) -> int:
        self.recheck_count += 1
        return 7

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def start(self) -> GenerationToken:
        with self._lock:
            self._running = True
        worker = Thread(target=self._emit_callbacks, name="stub-live-runtime", daemon=False)
        self._callback_thread = worker
        worker.start()
        return GenerationToken(1)

    def stop(self, *, timeout: float = 8.0) -> bool:
        self.stop_entered.set()
        if not self.stop_gate.wait(timeout):
            return False
        worker = self._callback_thread
        if worker is not None:
            worker.join(timeout)
        with self._lock:
            self._running = False
        self._on_status(
            LiveStatusUpdate(
                status=LiveStatus.STOPPED,
                generation=1,
                emitted_at=monotonic(),
                reason="stub stopped",
            )
        )
        return True

    def _emit_callbacks(self) -> None:
        self.callback_thread_id = get_ident()
        self._on_status(
            LiveStatusUpdate(
                status=LiveStatus.WAITING_FOR_PAGE,
                generation=1,
                emitted_at=monotonic(),
                reason="stub waiting",
            )
        )
        if self._emit_result:
            self._on_result(_recognition_result())
        if self._emitted_error is not None:
            self._on_error(self._emitted_error)


class RuntimeHarness:
    def __init__(
        self,
        *,
        emit_result: bool = False,
        emitted_error: LiveRuntimeError | None = None,
    ) -> None:
        self.emit_result = emit_result
        self.emitted_error = emitted_error
        self.runtimes: list[StubRuntime] = []
        self.analyzers: list[RoiAnalyzer] = []
        self.configs: list[CaptureConfig] = []

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
        on_status: StatusCallback,
        on_result: ResultCallback,
        on_error: ErrorCallback,
    ) -> StubRuntime:
        assert isinstance(backend, IdleBackend)
        assert stable_duration_seconds == 0.6
        assert minimum_stable_samples == 3
        assert confidence_threshold == 0.85
        assert isinstance(manual_mode, bool)
        runtime = StubRuntime(
            on_status,
            on_result,
            on_error,
            emit_result=self.emit_result,
            emitted_error=self.emitted_error,
        )
        self.runtimes.append(runtime)
        self.analyzers.append(analyzer)
        self.configs.append(config)
        return runtime


def _recognition_result() -> LiveRecognitionResult:
    return LiveRecognitionResult(
        generation=1,
        source_sequence=7,
        captured_at="2026-08-01T10:00:00+00:00",
        completed_at=12.0,
        packet_kind=RoiPacketKind.CONTENT,
        state_fingerprint="content:abc123",
        analysis=AnalysisResult(
            page_type="echo_enhancement",
            relevant=True,
            confidence=0.93,
            observation={
                "name": "不熄獵手",
                "level": 15,
                "sub_stats": [{"name": "暴擊", "value": 7.5, "unit": "%"}],
            },
        ),
    )


def test_recognition_payload_exposes_semantic_unchanged_flag() -> None:
    runtime_result = _recognition_result()
    analysis = runtime_result.analysis
    unchanged_result = LiveRecognitionResult(
        generation=runtime_result.generation,
        source_sequence=runtime_result.source_sequence,
        captured_at=runtime_result.captured_at,
        completed_at=runtime_result.completed_at,
        packet_kind=runtime_result.packet_kind,
        state_fingerprint=runtime_result.state_fingerprint,
        analysis=AnalysisResult(
            analysis.page_type,
            analysis.relevant,
            analysis.confidence,
            analysis.observation,
            unchanged=True,
        ),
    )

    assert RecognitionPayload.from_runtime(unchanged_result).unchanged is True


def _wait_until(
    qapp: QApplication,
    predicate: Callable[[], bool],
    *,
    timeout_seconds: float = 3.0,
) -> None:
    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        sleep(0.01)
    raise AssertionError("condition was not satisfied before timeout")


def _assert_array_free(value: object) -> None:
    assert not isinstance(value, (np.ndarray, RoiPacket, CapturedFrame))
    if is_dataclass(value) and not isinstance(value, type):
        for field in fields(value):
            _assert_array_free(getattr(value, field.name))
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _assert_array_free(key)
            _assert_array_free(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_array_free(item)


def test_monitor_refresh_is_async_and_preserves_backend_thread_affinity(
    qapp: QApplication,
) -> None:
    calls: Queue[tuple[str, int]] = Queue()
    list_gate = Event()
    controller = LiveCaptureController(
        backend_factory=lambda: DiscoveryBackend(calls, list_gate=list_gate)
    )
    spy = QSignalSpy(controller.monitors_changed)
    caller_thread = get_ident()

    assert controller.refresh_monitors()
    _wait_until(qapp, lambda: calls.qsize() == 1)
    assert not controller.refresh_monitors()
    list_gate.set()
    _wait_until(qapp, lambda: spy.count() > 0)

    descriptors = cast(tuple[MonitorDescriptor, ...], spy.at(0)[0])
    assert [item.identifier for item in descriptors] == ["monitor-1", "monitor-2"]
    assert descriptors[0].label == (
        "右側螢幕 (顯示器 1) · Display 1 · 2560x1440 · Windows 主要螢幕"
    )
    assert descriptors[1].label == ("左側螢幕 (顯示器 2) · Display 2 · 1920x1080")
    assert descriptors[0].is_compatible
    assert descriptors[1].is_compatible
    call_records = [calls.get(timeout=1) for _ in range(3)]
    worker_ids = {thread_id for _name, thread_id in call_records}
    assert len(worker_ids) == 1
    assert caller_thread not in worker_ids
    assert [name for name, _thread_id in call_records] == ["created", "listed", "closed"]
    assert controller.shutdown()


def test_start_emits_only_structured_payloads_from_background_callbacks(
    qapp: QApplication,
) -> None:
    harness = RuntimeHarness(emit_result=True)
    controller = LiveCaptureController(
        backend_factory=IdleBackend,
        analyzer_factory=NoopAnalyzer,
        runtime_factory=harness,
    )
    status_spy = QSignalSpy(controller.status_changed)
    result_spy = QSignalSpy(controller.recognition_ready)
    running_spy = QSignalSpy(controller.running_changed)
    ui_thread = get_ident()

    assert controller.start(LiveCaptureSettings(monitor_index=1))
    _wait_until(qapp, lambda: result_spy.count() > 0)

    assert controller.is_running
    assert harness.configs == [CaptureConfig(monitor_index=1, sample_hz=5.0)]
    assert harness.runtimes[0].callback_thread_id != ui_thread
    payload = cast(RecognitionPayload, result_spy.at(0)[0])
    assert payload.page_type == "echo_enhancement"
    assert payload.observation["level"] == 15
    _assert_array_free(payload)
    assert any(
        cast(CaptureStatusView, status_spy.at(index)[0]).status.value == "waiting_for_echo_page"
        for index in range(status_spy.count())
    )
    assert [running_spy.at(index)[0] for index in range(running_spy.count())] == [True]
    waiting = next(
        cast(CaptureStatusView, status_spy.at(index)[0])
        for index in range(status_spy.count())
        if cast(CaptureStatusView, status_spy.at(index)[0]).status.value == "waiting_for_echo_page"
    )
    assert "等待聲骸清單畫面" in waiting.reason
    assert "stub waiting" not in waiting.reason

    harness.runtimes[0].stop_gate.set()
    assert controller.stop()
    _wait_until(qapp, lambda: not controller.is_running)
    assert controller.shutdown()


def test_monitor_descriptors_use_left_center_right_layout_labels() -> None:
    descriptors = MonitorDescriptor.from_monitors(
        (
            MonitorInfo(7, 0, 0, 2560, 1440, is_primary=True),
            MonitorInfo(3, -2560, 0, 2560, 1440, name="Left panel"),
            MonitorInfo(9, 2560, 0, 2560, 1440, name="Right panel"),
        )
    )

    assert [item.identifier for item in descriptors] == ["monitor-7", "monitor-3", "monitor-9"]
    assert descriptors[0].label.startswith("中央螢幕 (顯示器 7)")
    assert descriptors[1].label.startswith("左側螢幕 (顯示器 3) · Left panel")
    assert descriptors[2].label.startswith("右側螢幕 (顯示器 9) · Right panel")


def test_application_log_records_only_capture_metadata(
    qapp: QApplication,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_error = r"D:\private\models\secret.onnx 不熄獵手 bundled model is missing"
    runtime_error = LiveRuntimeError(
        generation=1,
        source_sequence=8,
        emitted_at=10.0,
        error_type="OcrBackendError",
        message=secret_error,
    )
    harness = RuntimeHarness(emit_result=True, emitted_error=runtime_error)
    controller = LiveCaptureController(
        backend_factory=IdleBackend,
        analyzer_factory=NoopAnalyzer,
        runtime_factory=harness,
    )
    monitor_spy = QSignalSpy(controller.monitors_changed)
    result_spy = QSignalSpy(controller.recognition_ready)
    error_spy = QSignalSpy(controller.error_occurred)
    caplog.set_level("INFO", logger="wuwa_assistant")

    assert controller.refresh_monitors()
    _wait_until(qapp, lambda: monitor_spy.count() == 1)
    assert controller.start(LiveCaptureSettings(monitor_index=1))
    _wait_until(qapp, lambda: result_spy.count() == 1 and error_spy.count() == 1)

    harness.runtimes[0].stop_gate.set()
    assert controller.stop()
    _wait_until(qapp, lambda: not controller.is_running)
    # The worker clears is_running before emitting its final status and log.
    # Wait for the record under test before taking a snapshot of the logs.
    _wait_until(
        qapp,
        lambda: any(
            record.name == "wuwa_assistant"
            and record.getMessage() == "capture_stop_completed generation=1 success=true"
            for record in caplog.records
        ),
    )
    assert controller.shutdown()

    messages = "\n".join(
        record.getMessage() for record in caplog.records if record.name == "wuwa_assistant"
    )
    assert "monitor_enumeration_succeeded count=1" in messages
    assert (
        "monitor_detected index=1 left=0 top=0 width=2560 height=1440 primary=False compatible=True"
    ) in messages
    assert "capture_start_requested monitor_index=1" in messages
    assert "capture_runtime_started generation=1 monitor_index=1" in messages
    assert "capture_status_transition" in messages
    assert (
        "recognition_result generation=1 sequence=7 packet_kind=content "
        "page_type=echo_enhancement relevant=True confidence=0.93000"
    ) in messages
    assert (
        "capture_runtime_error error_type=OcrBackendError generation=1 sequence=8 "
        "code=ocr_unavailable"
    ) in messages
    assert "capture_stop_completed generation=1 success=true" in messages
    for forbidden in (
        "不熄獵手",
        "暴擊",
        "secret.onnx",
        "D:\\private",
        "content:abc123",
        "state_fingerprint",
        "observation",
    ):
        assert forbidden not in messages


def test_stop_is_nonblocking_and_controller_can_restart(qapp: QApplication) -> None:
    harness = RuntimeHarness()
    controller = LiveCaptureController(
        backend_factory=IdleBackend,
        analyzer_factory=NoopAnalyzer,
        runtime_factory=harness,
    )
    errors = QSignalSpy(controller.error_occurred)

    assert controller.start(LiveCaptureSettings(monitor_index=1))
    assert controller.stop()
    assert harness.runtimes[0].stop_entered.wait(1)
    assert not controller.start(LiveCaptureSettings(monitor_index=1))
    assert cast(CaptureControllerError, errors.at(0)[0]).code is ControllerErrorCode.START_REJECTED

    harness.runtimes[0].stop_gate.set()
    _wait_until(qapp, lambda: not controller.is_running)
    _wait_until(qapp, lambda: controller.status.value == "stopped")

    assert controller.start(LiveCaptureSettings(monitor_index=1, sample_hz=4.0))
    assert len(harness.runtimes) == 2
    harness.runtimes[1].stop_gate.set()
    assert controller.stop()
    _wait_until(qapp, lambda: not controller.is_running)
    assert controller.shutdown()


def test_missing_local_ocr_model_becomes_actionable_error(qapp: QApplication) -> None:
    runtime_error = LiveRuntimeError(
        generation=1,
        source_sequence=4,
        emitted_at=10.0,
        error_type="OcrBackendError",
        message="bundled model file is missing",
    )
    harness = RuntimeHarness(emitted_error=runtime_error)
    controller = LiveCaptureController(
        backend_factory=IdleBackend,
        analyzer_factory=NoopAnalyzer,
        runtime_factory=harness,
    )
    spy = QSignalSpy(controller.error_occurred)

    assert controller.start(LiveCaptureSettings(monitor_index=1))
    _wait_until(qapp, lambda: spy.count() > 0)

    error = cast(CaptureControllerError, spy.at(0)[0])
    assert error.code is ControllerErrorCode.OCR_UNAVAILABLE
    assert not error.recoverable
    assert "不會自動下載" in error.message
    _assert_array_free(error)
    harness.runtimes[0].stop_gate.set()
    assert controller.stop()
    _wait_until(qapp, lambda: not controller.is_running)
    assert controller.shutdown()


def test_default_analyzer_constructs_rapidocr_lazily(qapp: QApplication) -> None:
    harness = RuntimeHarness()
    controller = LiveCaptureController(
        backend_factory=IdleBackend,
        runtime_factory=harness,
    )

    assert controller.start(LiveCaptureSettings(monitor_index=1))
    analyzer = cast(LiveEchoRoiAnalyzer, harness.analyzers[0])
    engine = cast(RapidOcrEngine, analyzer.classifier.ocr)
    assert not engine.is_initialized

    harness.runtimes[0].stop_gate.set()
    assert controller.stop()
    _wait_until(qapp, lambda: not controller.is_running)
    assert controller.shutdown()


def test_restarted_mode_rejects_old_queued_payload_even_if_local_runtime_generation_repeats(
    qapp: QApplication,
) -> None:
    harness = RuntimeHarness(emit_result=True)
    controller = LiveCaptureController(
        backend_factory=IdleBackend, analyzer_factory=NoopAnalyzer, runtime_factory=harness
    )
    spy = QSignalSpy(controller.recognition_ready)
    assert controller.start(LiveCaptureSettings(1))
    _wait_until(qapp, lambda: spy.count() == 1)
    old = spy.at(0)[0]
    assert controller.accepts_recognition(old)
    harness.runtimes[0].stop_gate.set()
    assert controller.stop()
    _wait_until(qapp, lambda: not controller.is_running)
    assert controller.start(LiveCaptureSettings(1, manual_mode=True))
    _wait_until(qapp, lambda: spy.count() == 2)
    assert not controller.accepts_recognition(old)
    assert controller.accepts_recognition(spy.at(1)[0])
    harness.runtimes[1].stop_gate.set()
    assert controller.shutdown()


def test_start_and_stop_do_not_hold_controller_lock_while_querying_runtime(
    qapp: QApplication,
) -> None:
    harness = RuntimeHarness()
    controller = LiveCaptureController(
        backend_factory=IdleBackend, analyzer_factory=NoopAnalyzer, runtime_factory=harness
    )
    assert controller.start(LiveCaptureSettings(1))
    checked = Event()

    class CallbackRuntime:
        @property
        def is_running(self) -> bool:
            def callback() -> None:
                controller._handle_runtime_status(
                    1, LiveStatusUpdate(LiveStatus.STABLE, 1, monotonic(), "test")
                )
                checked.set()

            worker = Thread(target=callback)
            worker.start()
            assert checked.wait(1), "runtime status callback deadlocked against controller lock"
            worker.join()
            return True

        def stop(self, *, timeout: float = 8.0) -> bool:
            return harness.runtimes[0].stop(timeout=timeout)

    controller._runtime = CallbackRuntime()  # type: ignore[assignment]
    assert not controller.start(LiveCaptureSettings(1))
    harness.runtimes[0].stop_gate.set()
    assert controller.stop()
    _wait_until(qapp, lambda: not controller.is_running)
    assert controller.shutdown()


def test_early_coordinator_stopped_cannot_enable_restart_before_cleanup(qapp: QApplication) -> None:
    harness = RuntimeHarness()
    controller = LiveCaptureController(
        backend_factory=IdleBackend, analyzer_factory=NoopAnalyzer, runtime_factory=harness
    )
    assert controller.start(LiveCaptureSettings(1))
    runtime = harness.runtimes[0]
    original_stop = runtime.stop
    invalidated = Event()

    def early_stop(*, timeout: float = 8.0) -> bool:
        runtime._on_status(LiveStatusUpdate(LiveStatus.STOPPED, 1, monotonic(), "jobs invalidated"))
        invalidated.set()
        return original_stop(timeout=timeout)

    runtime.stop = early_stop
    try:
        assert controller.stop()
        _wait_until(qapp, invalidated.is_set)
        assert controller.is_running
        assert controller.status.value == "stopping"
        assert not controller.start(LiveCaptureSettings(1, manual_mode=True))
        runtime.stop_gate.set()
        _wait_until(
            qapp, lambda: not controller.is_running and controller.status.value == "stopped"
        )
        assert controller.start(LiveCaptureSettings(1, manual_mode=True))
        harness.runtimes[1].stop_gate.set()
    finally:
        runtime.stop_gate.set()
        assert controller.shutdown()


def test_failed_stop_keeps_runtime_guard_but_allows_retry(qapp: QApplication) -> None:
    harness = RuntimeHarness()
    controller = LiveCaptureController(
        backend_factory=IdleBackend, analyzer_factory=NoopAnalyzer, runtime_factory=harness
    )
    assert controller.start(LiveCaptureSettings(1))
    runtime = harness.runtimes[0]
    original_stop = runtime.stop
    runtime.stop = lambda **kwargs: False
    try:
        assert controller.stop()
        _wait_until(qapp, lambda: controller.status.value == "error")
        assert controller.is_running
        assert not controller.start(LiveCaptureSettings(1))
        runtime.stop = original_stop
        runtime.stop_gate.set()
        assert controller.stop()
        _wait_until(
            qapp, lambda: not controller.is_running and controller.status.value == "stopped"
        )
        assert controller.start(LiveCaptureSettings(1, manual_mode=True))
        harness.runtimes[1].stop_gate.set()
    finally:
        runtime.stop = original_stop
        runtime.stop_gate.set()
        assert controller.shutdown()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"monitor_index": 0}, "physical monitor"),
        ({"monitor_index": 1, "sample_hz": 9.0}, "between 2 and 8"),
        ({"monitor_index": 1, "stable_duration_seconds": 0.4}, "0.50..0.75"),
        ({"monitor_index": 1, "confidence_threshold": 1.1}, r"within \[0, 1\]"),
        ({"monitor_index": 1, "minimum_stable_samples": 1}, "at least 2"),
    ],
)
def test_live_capture_settings_fail_closed(
    kwargs: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        LiveCaptureSettings(**kwargs)


def test_shutdown_suppresses_late_monitor_signal(qapp: QApplication) -> None:
    release = Event()

    class SlowBackend(DiscoveryBackend):
        def list_monitors(self) -> tuple[MonitorInfo, ...]:
            release.wait(2)
            return super().list_monitors()

    calls: Queue[tuple[str, int]] = Queue()
    controller = LiveCaptureController(backend_factory=lambda: SlowBackend(calls))
    spy = QSignalSpy(controller.monitors_changed)

    assert controller.refresh_monitors()
    release.set()
    assert controller.shutdown(timeout=3.0)
    QCoreApplication.processEvents()
    assert spy.count() == 0
