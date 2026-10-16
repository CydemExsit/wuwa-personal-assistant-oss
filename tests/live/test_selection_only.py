from __future__ import annotations

from copy import deepcopy
from threading import Event, get_ident
from typing import Any, cast

from wuwa_assistant.capture import CaptureConfig, ExtractedRois, FakeCaptureBackend, RoiPacket
from wuwa_assistant.live import (
    AnalysisResult,
    LiveCaptureRuntime,
    LiveEchoRoiAnalyzer,
    LiveRecognitionCoordinator,
    RoiPacketKind,
)
from wuwa_assistant.models import EchoObservation, OcrToken, PageClassification, PageType
from wuwa_assistant.ocr import FakeOcrEngine
from wuwa_assistant.vision.rois import PAGE_HEADER_ROI

from .helpers import packet


def test_selection_only_skips_enhancement_content_and_reads_only_header() -> None:
    ocr = FakeOcrEngine([[OcrToken("聲骸強化", 0.99, (0, 0, 100, 30))]])
    analyzer = LiveEchoRoiAnalyzer(ocr, selection_only=True)
    result = analyzer.analyze(
        packet(1, 0, "enhancement", kind=RoiPacketKind.CONTENT, page_type="echo_selection")
    )
    assert not result.relevant and not result.request_content
    assert result.observation["events"] == []
    assert analyzer.tracker.sessions == {}
    # Fake OCR has only one response; reading enhancement state/materials would
    # either exhaust it or manufacture parser evidence, neither is permitted.
    assert result.confidence == 0


def test_recheck_refreshes_same_echo_without_new_tracker_session() -> None:
    class Classifier:
        def classify_rois(self, images: object) -> PageClassification:
            return PageClassification(PageType.ECHO_SELECTION, 0.99)

    class Parser:
        def parse_rois(self, *args: object, **kwargs: object) -> EchoObservation:
            return EchoObservation(
                page_type=PageType.ECHO_SELECTION,
                name="不熄獵手",
                level=25,
                cost=1,
                confidence={"page_type": 0.99},
            )

    analyzer = LiveEchoRoiAnalyzer(FakeOcrEngine(), selection_only=True)
    analyzer.classifier = cast(Any, Classifier())
    analyzer.parser = cast(Any, Parser())

    def analyze(sequence: int) -> AnalysisResult:
        return analyzer.analyze(
            packet(
                sequence, sequence, "same", kind=RoiPacketKind.CONTENT, page_type="echo_selection"
            )
        )

    original = analyze(1)
    assert analyzer.finalize_analysis(original, accept=True)
    sessions = deepcopy(analyzer.tracker.sessions)
    assert analyze(2).unchanged
    analyzer.request_recheck()
    canceled = analyze(3)
    assert not canceled.unchanged
    assert analyzer.finalize_analysis(canceled, accept=False)
    refreshed = analyze(4)
    assert not refreshed.unchanged and refreshed.observation["events"] == []
    assert analyzer.finalize_analysis(refreshed, accept=True)
    assert analyze(5).unchanged
    assert analyzer.tracker.sessions == sessions


def test_recheck_cancels_active_and_pending_without_restarting_worker() -> None:
    class BlockingAnalyzer:
        def __init__(self) -> None:
            self.started = Event()
            self.release = Event()
            self.thread_ids: list[int] = []
            self.finalized: list[bool] = []

        def analyze(self, item: RoiPacket) -> AnalysisResult:
            self.thread_ids.append(get_ident())
            if len(self.thread_ids) == 1:
                self.started.set()
                assert self.release.wait(3)
            return AnalysisResult(
                "echo_selection", True, 0.99, {"name": "test"}, requires_commit=True
            )

        def finalize_analysis(self, result: AnalysisResult, *, accept: bool) -> bool:
            self.finalized.append(accept)
            return True

    analyzer = BlockingAnalyzer()
    results = []
    coordinator = LiveRecognitionCoordinator(
        analyzer, minimum_stable_samples=2, stable_duration_seconds=0.5, on_result=results.append
    )
    token = coordinator.start()
    try:
        for sequence, timestamp, label in [(1, 0.0, "old"), (2, 0.5, "old")]:
            coordinator.submit(token, packet(sequence, timestamp, label))
        assert analyzer.started.wait(1)
        coordinator.submit(token, packet(3, 0.6, "pending"))
        coordinator.submit(token, packet(4, 1.1, "pending"))
        assert coordinator.request_recheck() == 4
        assert coordinator.generation == token and coordinator.worker_alive
        coordinator.submit(token, packet(5, 1.2, "fresh"))
        coordinator.submit(token, packet(6, 1.7, "fresh"))
        analyzer.release.set()
        assert coordinator.wait_until_idle()
        assert [result.source_sequence for result in results] == [6]
        assert analyzer.finalized == [False, True]
        assert len(analyzer.thread_ids) == 2 and len(set(analyzer.thread_ids)) == 1
        assert coordinator.stats.canceled_results_dropped == 1
    finally:
        analyzer.release.set()
        assert coordinator.stop()
    assert coordinator.request_recheck() is None


def test_selection_round_trips_recover_even_when_header_hash_never_changes() -> None:
    class PageAnalyzer:
        page = "echo_selection"

        def analyze(self, item: RoiPacket) -> AnalysisResult:
            relevant = self.page == "echo_selection"
            return AnalysisResult(self.page, relevant, 0.99, {}, request_content=relevant)

    analyzer = PageAnalyzer()
    results = []
    runtime = LiveCaptureRuntime(
        FakeCaptureBackend([]),
        CaptureConfig(1),
        analyzer,
        selection_only=True,
        on_result=results.append,
    )
    token = runtime.coordinator.start()
    sequence = 0

    def sample(*, animated: bool = False) -> None:
        nonlocal sequence
        sequence += 1
        timestamp = sequence * 0.25
        probe = packet(sequence, timestamp, "same-header")
        content = None
        if runtime.content_page_type is not None:
            content = packet(
                sequence,
                timestamp,
                "same-header",
                kind=RoiPacketKind.CONTENT,
                page_type=runtime.content_page_type,
                state_pattern=f"animation-{sequence}" if animated else "stable-list",
                roi_patterns={PAGE_HEADER_ROI.name: "same-header"},
            )
        runtime.coordinator.submit(token, runtime._route_packet(ExtractedRois(probe, content)))
        assert runtime.coordinator.wait_until_idle()

    try:
        for _ in range(10):
            sample()
        assert results[-1].packet_kind is RoiPacketKind.CONTENT
        for _ in range(3):
            analyzer.page = "echo_enhancement"
            for _ in range(18):
                sample(animated=True)
            assert runtime.content_page_type is None
            assert results[-1].packet_kind is RoiPacketKind.PROBE
            analyzer.page = "echo_selection"
            for _ in range(20):
                sample()
            assert runtime.content_page_type == "echo_selection"
            assert results[-1].packet_kind is RoiPacketKind.CONTENT
        assert all(
            result.analysis.page_type == "echo_selection" or not result.analysis.relevant
            for result in results
            if result.packet_kind is RoiPacketKind.CONTENT
        )
        previous_count = len(results)
        previous = results[-1]
        cutoff = runtime.request_recheck()
        assert cutoff is not None and cutoff >= previous.source_sequence
        runtime._handle_result(previous)  # callback already queued before reset
        assert len(results) == previous_count
        assert runtime.content_page_type is None
        for _ in range(10):
            sample()
        assert results[-1].source_sequence > cutoff
        assert results[-1].packet_kind is RoiPacketKind.CONTENT
    finally:
        assert runtime.coordinator.stop()
