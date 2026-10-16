from __future__ import annotations

from threading import Event, Lock

from wuwa_assistant.capture import RoiPacket
from wuwa_assistant.live import (
    AnalysisResult,
    GenerationToken,
    LiveRecognitionCoordinator,
    PacketDisposition,
)

from .helpers import BlockingAnalyzer, accepted_result, packet


class _TransactionalBlockingAnalyzer:
    def __init__(self) -> None:
        self.first_started = Event()
        self.release_first = Event()
        self._lock = Lock()
        self._calls: list[str] = []
        self._prepared: dict[int, str] = {}
        self.committed: list[str] = []
        self.discarded: list[str] = []

    def analyze(self, roi_packet: RoiPacket) -> AnalysisResult:
        assert roi_packet.source_file is not None
        with self._lock:
            self._calls.append(roi_packet.source_file)
            call_number = len(self._calls)
        if call_number == 1:
            self.first_started.set()
            if not self.release_first.wait(3.0):
                raise TimeoutError("test did not release the first analyzer call")
        base = accepted_result()
        result = AnalysisResult(
            base.page_type,
            base.relevant,
            base.confidence,
            base.observation,
            requires_commit=True,
        )
        with self._lock:
            self._prepared[id(result)] = roi_packet.source_file
        return result

    def finalize_analysis(self, result: AnalysisResult, *, accept: bool) -> bool:
        with self._lock:
            key = self._prepared.pop(id(result))
            (self.committed if accept else self.discarded).append(key)
        return True


def _schedule(
    coordinator: LiveRecognitionCoordinator,
    token_value: GenerationToken,
    *,
    fingerprint: str,
    first_sequence: int,
    first_time: float,
) -> PacketDisposition:
    coordinator.submit(
        token_value,
        packet(first_sequence, first_time, fingerprint),
    )
    return coordinator.submit(
        token_value,
        packet(first_sequence + 1, first_time + 0.5, fingerprint),
    )


def test_active_one_pending_one_replaces_pending_and_drops_superseded_result() -> None:
    analyzer = BlockingAnalyzer()
    results = []
    coordinator = LiveRecognitionCoordinator(
        analyzer,
        stable_duration_seconds=0.50,
        minimum_stable_samples=2,
        on_result=results.append,
    )
    token = coordinator.start()

    assert (
        _schedule(
            coordinator,
            token,
            fingerprint="state-a",
            first_sequence=1,
            first_time=0.0,
        )
        is PacketDisposition.QUEUED
    )
    assert analyzer.first_started.wait(1.0)
    assert (
        _schedule(
            coordinator,
            token,
            fingerprint="state-b",
            first_sequence=3,
            first_time=0.6,
        )
        is PacketDisposition.QUEUED
    )
    assert (
        _schedule(
            coordinator,
            token,
            fingerprint="state-c",
            first_sequence=5,
            first_time=1.2,
        )
        is PacketDisposition.REPLACED_PENDING
    )

    analyzer.release_first.set()
    assert coordinator.wait_until_idle()
    assert analyzer.calls == ("state-a", "state-c")
    assert [result.source_sequence for result in results] == [6]
    assert coordinator.stats.pending_replaced == 1
    assert coordinator.stats.superseded_results_dropped == 1
    assert coordinator.stop()


def test_superseded_semantic_state_is_committed_and_emitted_in_order() -> None:
    analyzer = _TransactionalBlockingAnalyzer()
    results = []
    coordinator = LiveRecognitionCoordinator(
        analyzer,
        stable_duration_seconds=0.50,
        minimum_stable_samples=2,
        on_result=results.append,
    )
    token = coordinator.start()

    _schedule(
        coordinator,
        token,
        fingerprint="state-a",
        first_sequence=1,
        first_time=0.0,
    )
    assert analyzer.first_started.wait(1.0)
    _schedule(
        coordinator,
        token,
        fingerprint="state-b",
        first_sequence=3,
        first_time=0.6,
    )

    analyzer.release_first.set()
    assert coordinator.wait_until_idle()
    assert analyzer.discarded == []
    assert analyzer.committed == ["state-a", "state-b"]
    assert [result.source_sequence for result in results] == [2, 4]
    assert coordinator.stop()


def test_stop_cancels_active_result_clears_pending_and_joins_cleanly() -> None:
    analyzer = BlockingAnalyzer()
    results = []
    coordinator = LiveRecognitionCoordinator(
        analyzer,
        stable_duration_seconds=0.50,
        minimum_stable_samples=2,
        on_result=results.append,
    )
    token = coordinator.start()
    _schedule(
        coordinator,
        token,
        fingerprint="state-a",
        first_sequence=1,
        first_time=0.0,
    )
    assert analyzer.first_started.wait(1.0)
    _schedule(
        coordinator,
        token,
        fingerprint="state-b",
        first_sequence=3,
        first_time=0.6,
    )

    assert not coordinator.stop(wait=False)
    analyzer.release_first.set()
    assert coordinator.stop(wait=True, timeout=2.0)
    assert not coordinator.worker_alive
    assert analyzer.calls == ("state-a",)
    assert results == []
    assert coordinator.stats.canceled_results_dropped == 1


def test_callback_failure_does_not_kill_single_worker() -> None:
    analyzer = BlockingAnalyzer()
    analyzer.release_first.set()
    callback_seen = Event()

    def broken_callback(result: object) -> None:
        del result
        callback_seen.set()
        raise RuntimeError("consumer failed")

    coordinator = LiveRecognitionCoordinator(
        analyzer,
        stable_duration_seconds=0.50,
        minimum_stable_samples=2,
        on_result=broken_callback,
    )
    token = coordinator.start()
    _schedule(
        coordinator,
        token,
        fingerprint="state-a",
        first_sequence=1,
        first_time=0.0,
    )

    assert coordinator.wait_until_idle()
    assert callback_seen.is_set()
    assert coordinator.stats.callback_errors == 1
    assert coordinator.worker_alive
    assert coordinator.stop()
