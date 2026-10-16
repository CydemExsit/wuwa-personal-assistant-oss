from __future__ import annotations

from threading import Event
from time import monotonic, sleep

import numpy as np
import pytest

from wuwa_assistant.capture import (
    CaptureConfig,
    CapturedFrame,
    FrameLeaseAudit,
    MonitorInfo,
    RoiPacket,
    RoiPacketKind,
)
from wuwa_assistant.live import AnalysisResult, LiveCaptureRuntime, LiveStatus


class RepeatingBackend:
    def __init__(self) -> None:
        self.audit = FrameLeaseAudit()
        self.count = 0
        self.closed = False

    def list_monitors(self) -> tuple[MonitorInfo, ...]:
        return (MonitorInfo(1, 0, 0, 2560, 1440),)

    def capture_monitor(self, monitor_index: int) -> CapturedFrame:
        self.count += 1
        return CapturedFrame.now(
            np.zeros((1440, 2560, 3), dtype=np.uint8),
            monitor_index,
            sequence=self.count,
            audit=self.audit,
        )

    def close(self) -> None:
        self.closed = True


class ManualAnalyzer:
    def __init__(self) -> None:
        self.calls = 0
        self.reset_count = 0
        self.content_count = 0
        self.page = "echo_selection"
        self.entered = Event()
        self.release = Event()
        self.block = False

    def reset(self) -> None:
        self.reset_count += 1

    def analyze(self, packet: RoiPacket) -> AnalysisResult:
        self.calls += 1
        self.entered.set()
        if self.block:
            assert self.release.wait(3)
        if packet.kind is RoiPacketKind.CONTENT:
            self.content_count += 1
        return AnalysisResult(
            self.page,
            self.page == "echo_selection",
            0.99,
            {"name": "夢魘·刺玫菇", "level": 25},
            request_content=self.page == "echo_selection",
        )


def _wait_until(predicate, seconds: float = 4) -> None:  # type: ignore[no-untyped-def]
    deadline = monotonic() + seconds
    while not predicate() and monotonic() < deadline:
        sleep(0.01)
    assert predicate()


def test_manual_requests_stop_capturing_and_reuse_session_until_next_click() -> None:
    backend, analyzer = RepeatingBackend(), ManualAnalyzer()
    updates, results, errors = [], [], []
    runtime = LiveCaptureRuntime(
        backend,
        CaptureConfig(1, 8),
        analyzer,
        manual_mode=True,
        selection_only=True,
        stable_duration_seconds=0.5,
        minimum_stable_samples=2,
        on_status=updates.append,
        on_result=results.append,
        on_error=errors.append,
    )
    try:
        token = runtime.start()
        _wait_until(lambda: updates[-1].status is LiveStatus.WAITING_FOR_REQUEST)
        counts = backend.count, analyzer.calls
        sleep(0.3)
        assert (backend.count, analyzer.calls) == counts
        assert analyzer.content_count == 1
        assert runtime.is_running
        fence = runtime.request_recheck()
        assert fence is not None
        _wait_until(
            lambda: (
                analyzer.content_count == 2 and updates[-1].status is LiveStatus.WAITING_FOR_REQUEST
            )
        )
        assert analyzer.reset_count == 1
        assert {item.generation for item in results} == {token.value}
        assert results[-1].source_sequence > fence
        counts = backend.count, analyzer.calls
        sleep(0.3)
        assert (backend.count, analyzer.calls) == counts
        assert errors == []
    finally:
        assert runtime.stop(timeout=2)
    backend.audit.assert_all_released()
    assert backend.closed


def test_manual_wrong_page_finishes_without_repeated_background_ocr() -> None:
    backend, analyzer = RepeatingBackend(), ManualAnalyzer()
    analyzer.page = "echo_enhancement"
    updates = []
    runtime = LiveCaptureRuntime(
        backend,
        CaptureConfig(1, 8),
        analyzer,
        manual_mode=True,
        selection_only=True,
        stable_duration_seconds=0.5,
        on_status=updates.append,
    )
    try:
        runtime.start()
        _wait_until(lambda: updates[-1].status is LiveStatus.WAITING_FOR_REQUEST)
        assert updates[-1].reason == "manual request needs echo selection page"
        counts = backend.count, analyzer.calls
        sleep(0.3)
        assert (backend.count, analyzer.calls) == counts
        assert analyzer.content_count == 0
    finally:
        assert runtime.stop(timeout=2)
    backend.audit.assert_all_released()


def test_manual_timeout_fences_slow_ocr_and_can_retry() -> None:
    backend, analyzer = RepeatingBackend(), ManualAnalyzer()
    analyzer.block = True
    updates, results = [], []
    runtime = LiveCaptureRuntime(
        backend,
        CaptureConfig(1, 8),
        analyzer,
        manual_mode=True,
        selection_only=True,
        manual_timeout_seconds=0.9,
        stable_duration_seconds=0.5,
        on_status=updates.append,
        on_result=results.append,
    )
    try:
        runtime.start()
        assert analyzer.entered.wait(2)
        _wait_until(lambda: updates[-1].status is LiveStatus.WAITING_FOR_REQUEST)
        assert updates[-1].reason == "manual request timed out"
        count = backend.count
        analyzer.release.set()
        assert runtime.coordinator.wait_until_idle(timeout=2)
        sleep(0.2)
        assert results == []
        assert backend.count == count
        assert updates[-1].status is LiveStatus.WAITING_FOR_REQUEST
        analyzer.block = False
        analyzer.page = "unknown"
        assert runtime.request_recheck() is not None
        _wait_until(lambda: bool(results))
        _wait_until(lambda: updates[-1].status is LiveStatus.WAITING_FOR_REQUEST)
        assert updates[-1].reason == "manual request needs echo selection page"
    finally:
        analyzer.release.set()
        assert runtime.stop(timeout=2)
    backend.audit.assert_all_released()


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
def test_manual_timeout_validation(timeout: float) -> None:
    with pytest.raises(ValueError):
        LiveCaptureRuntime(
            RepeatingBackend(), CaptureConfig(1), ManualAnalyzer(), manual_timeout_seconds=timeout
        )
