from __future__ import annotations

from copy import deepcopy
from threading import Event
from time import monotonic, sleep
from typing import Any, cast

import numpy as np
import pytest

from wuwa_assistant.capture import (
    CaptureConfig,
    FakeCaptureBackend,
    MonitorInfo,
    RoiPacket,
    RoiPacketKind,
)
from wuwa_assistant.live import (
    AnalysisResult,
    FakeRoiAnalyzer,
    LiveCaptureRuntime,
    LiveEchoRoiAnalyzer,
    LiveRecognitionCoordinator,
)
from wuwa_assistant.models import (
    EchoObservation,
    EchoStat,
    OcrToken,
    PageClassification,
    PageType,
    StatKind,
)
from wuwa_assistant.ocr import FakeOcrEngine

from .helpers import packet


def _token(text: str, x: int = 0, y: int = 0, confidence: float = 0.99) -> OcrToken:
    return OcrToken(text, confidence, (x, y, x + 100, y + 30))


def test_real_live_analyzer_classifies_then_parses_content_without_frame() -> None:
    panel_tokens = [
        _token("不熄獵手", 30, 45),
        _token("+5", 30, 115),
        _token("生命", 90, 195),
        _token("8.2%", 530, 195),
        _token("生命", 90, 250),
        _token("820", 535, 250),
        _token("暴擊", 90, 310),
        _token("7.5%", 535, 310),
        *[
            _token(f"強化至+{level}並啟用輔音屬性", 90, 370 + index * 57)
            for index, level in enumerate((10, 15, 20, 25))
        ],
    ]
    ocr = FakeOcrEngine(
        [
            [_token("聲骸強化")],
            [_token("+5")],
            [_token("強化")],
            [_token("聲骸強化")],
            [_token("+5")],
            [_token("強化")],
            panel_tokens,
        ]
    )
    analyzer = LiveEchoRoiAnalyzer(ocr)

    probe_result = analyzer.analyze(packet(1, 0.0, "probe-state"))
    content_result = analyzer.analyze(
        packet(
            2,
            0.7,
            "content-state",
            kind=RoiPacketKind.CONTENT,
            page_type=PageType.ECHO_ENHANCEMENT.value,
        )
    )

    assert probe_result.page_type == PageType.ECHO_ENHANCEMENT.value
    assert probe_result.request_content is True
    assert content_result.observation["name"] == "不熄獵手"
    assert content_result.observation["level"] == 5
    assert content_result.observation["source_file"] == "content-state"
    assert content_result.observation["events"]
    assert not _contains_array(content_result.observation)


def test_live_analyzer_commits_only_fresh_state_and_marks_semantic_duplicates() -> None:
    class StaticClassifier:
        def classify_rois(self, images: object) -> PageClassification:
            del images
            return PageClassification(PageType.ECHO_ENHANCEMENT, 0.99)

    class ObservationParser:
        def __init__(self, observations: list[EchoObservation]) -> None:
            self._observations = observations

        def parse_rois(self, *args: object, **kwargs: object) -> EchoObservation:
            del args, kwargs
            return deepcopy(self._observations.pop(0))

    original = EchoObservation(
        page_type=PageType.ECHO_ENHANCEMENT,
        name="不熄獵手",
        level=5,
        main_stats=[
            EchoStat(StatKind.PRIMARY, "生命", 8.2, "%", 0.99, 0.99, 0.99),
            EchoStat(StatKind.FIXED, "生命", 820, None, 0.99, 0.99, 0.99),
        ],
        sub_stats=[
            EchoStat(
                StatKind.SUBSTAT,
                "暴擊",
                7.5,
                "%",
                0.99,
                0.99,
                0.99,
                slot_index=0,
            )
        ],
        locked_substat_slots=4,
        confidence={"page_type": 0.99, "level": 0.99},
    )
    no_event_change = deepcopy(original)
    no_event_change.locked_substat_slots = 3
    analyzer = LiveEchoRoiAnalyzer(FakeOcrEngine())
    analyzer.classifier = cast(Any, StaticClassifier())
    analyzer.parser = cast(
        Any,
        ObservationParser(
            [original, original, no_event_change, no_event_change],
        ),
    )

    first = analyzer.analyze(
        packet(
            1,
            0.0,
            "stale-content",
            kind=RoiPacketKind.CONTENT,
            page_type=PageType.ECHO_ENHANCEMENT.value,
        )
    )
    assert first.requires_commit is True
    assert analyzer.tracker.sessions == {}
    assert analyzer.finalize_analysis(first, accept=False)
    assert analyzer.tracker.sessions == {}

    accepted = analyzer.analyze(
        packet(
            2,
            0.5,
            "accepted-content",
            kind=RoiPacketKind.CONTENT,
            page_type=PageType.ECHO_ENHANCEMENT.value,
        )
    )
    current_session = accepted.observation["current_session"]
    assert isinstance(current_session, dict)
    assert current_session["local_session_id"] == "echo-session-0001"
    assert analyzer.finalize_analysis(accepted, accept=True)
    assert tuple(analyzer.tracker.sessions) == ("echo-session-0001",)

    changed_without_event = analyzer.analyze(
        packet(
            3,
            1.0,
            "changed-content",
            kind=RoiPacketKind.CONTENT,
            page_type=PageType.ECHO_ENHANCEMENT.value,
        )
    )
    assert changed_without_event.observation["events"] == []
    assert changed_without_event.unchanged is False
    assert analyzer.finalize_analysis(changed_without_event, accept=True)

    duplicate = analyzer.analyze(
        packet(
            4,
            1.5,
            "duplicate-content",
            kind=RoiPacketKind.CONTENT,
            page_type=PageType.ECHO_ENHANCEMENT.value,
        )
    )
    assert duplicate.observation["events"] == []
    assert duplicate.unchanged is True
    assert duplicate.requires_commit is False
    assert analyzer.finalize_analysis(duplicate, accept=True)
    assert len(analyzer.tracker.sessions["echo-session-0001"].observations) == 2


def test_empty_selection_overlay_does_not_mutate_or_inherit_the_previous_session() -> None:
    class SelectionClassifier:
        def classify_rois(self, images: object) -> PageClassification:
            return PageClassification(PageType.ECHO_SELECTION, 0.99)

    original = EchoObservation(page_type=PageType.ECHO_SELECTION, name="不熄獵手", level=25, cost=1)
    observations = [original, EchoObservation(page_type=PageType.ECHO_SELECTION), original]

    class SelectionParser:
        def parse_rois(self, *args: object, **kwargs: object) -> EchoObservation:
            return deepcopy(observations.pop(0))

    analyzer = LiveEchoRoiAnalyzer(FakeOcrEngine())
    analyzer.classifier = cast(Any, SelectionClassifier())
    analyzer.parser = cast(Any, SelectionParser())

    def analyze(sequence: int) -> AnalysisResult:
        return analyzer.analyze(
            packet(
                sequence,
                float(sequence),
                "same-echo",
                kind=RoiPacketKind.CONTENT,
                page_type=PageType.ECHO_SELECTION.value,
            )
        )

    first = analyze(1)
    assert analyzer.finalize_analysis(first, accept=True)
    before = deepcopy(analyzer.tracker.sessions)
    rejected = analyze(2)
    assert rejected.relevant is False
    assert rejected.request_content is True
    assert rejected.requires_commit is False
    assert rejected.observation["events"] == []
    assert "current_session" not in rejected.observation
    assert analyzer.tracker.sessions == before
    resumed = analyze(3)
    assert resumed.unchanged is True
    assert analyzer.tracker.sessions == before


def test_superseded_material_state_still_separates_and_consumes_echo_sessions() -> None:
    class QueueClassifier:
        def __init__(self, pages: list[PageType]) -> None:
            self._pages = pages

        def classify_rois(self, images: object) -> PageClassification:
            del images
            return PageClassification(self._pages.pop(0), 0.99)

    class ObservationParser:
        def __init__(self, observations: list[EchoObservation]) -> None:
            self._observations = observations

        def parse_rois(self, *args: object, **kwargs: object) -> EchoObservation:
            del args, kwargs
            return deepcopy(self._observations.pop(0))

    hp_at_15 = [
        EchoStat(StatKind.PRIMARY, "生命", 15.5, "%", 0.99, 0.99, 0.99),
        EchoStat(StatKind.FIXED, "生命", 1550, None, 0.99, 0.99, 0.99),
    ]
    hp_at_0 = [
        EchoStat(StatKind.PRIMARY, "生命", 4.5, "%", 0.99, 0.99, 0.99),
        EchoStat(StatKind.FIXED, "生命", 456, None, 0.99, 0.99, 0.99),
    ]
    source = EchoObservation(
        page_type=PageType.ECHO_ENHANCEMENT,
        name="不熄獵手",
        level=15,
        cost=1,
        main_stats=hp_at_15,
        sub_stats=[EchoStat(StatKind.SUBSTAT, "暴擊", 7.5, "%", 0.99, 0.99, 0.99)],
        confidence={"page_type": 0.99},
    )
    material = EchoObservation(
        page_type=PageType.ENHANCEMENT_MATERIAL_SELECTION,
        name="不熄獵手",
        level=0,
        projected_level=15,
        cost=1,
        main_stats=hp_at_0,
        projected_main_stats=hp_at_15,
        material_source_selected=True,
        material_source_level=15,
        confidence={"page_type": 0.99},
    )
    target = EchoObservation(
        page_type=PageType.ECHO_ENHANCEMENT,
        name="不熄獵手",
        level=15,
        cost=1,
        main_stats=hp_at_15,
        sub_stats=[
            EchoStat(
                StatKind.SUBSTAT,
                "共鳴技能傷害加成",
                8.6,
                "%",
                0.99,
                0.99,
                0.99,
            )
        ],
        confidence={"page_type": 0.99},
    )
    delegate = LiveEchoRoiAnalyzer(FakeOcrEngine())
    delegate.classifier = cast(
        Any,
        QueueClassifier(
            [
                PageType.ECHO_ENHANCEMENT,
                PageType.ENHANCEMENT_MATERIAL_SELECTION,
                PageType.ECHO_ENHANCEMENT,
            ]
        ),
    )
    delegate.parser = cast(Any, ObservationParser([source, material, target]))

    class BlockMaterialAnalyzer:
        def __init__(self) -> None:
            self.calls = 0
            self.material_prepared = Event()
            self.release_material = Event()

        def analyze(self, roi_packet: RoiPacket) -> AnalysisResult:
            self.calls += 1
            result = delegate.analyze(roi_packet)
            if self.calls == 2:
                self.material_prepared.set()
                if not self.release_material.wait(3.0):
                    raise TimeoutError("test did not release material analysis")
            return result

        def finalize_analysis(self, result: AnalysisResult, *, accept: bool) -> bool:
            return delegate.finalize_analysis(result, accept=accept)

    analyzer = BlockMaterialAnalyzer()
    results = []
    coordinator = LiveRecognitionCoordinator(
        analyzer,
        stable_duration_seconds=0.50,
        minimum_stable_samples=2,
        on_result=results.append,
    )
    token = coordinator.start()

    def schedule_state(
        sequence: int,
        captured_at: float,
        label: str,
        page_type: PageType,
    ) -> None:
        coordinator.submit(
            token,
            packet(
                sequence,
                captured_at,
                label,
                kind=RoiPacketKind.CONTENT,
                page_type=page_type.value,
            ),
        )
        coordinator.submit(
            token,
            packet(
                sequence + 1,
                captured_at + 0.5,
                label,
                kind=RoiPacketKind.CONTENT,
                page_type=page_type.value,
            ),
        )

    schedule_state(1, 0.0, "source", PageType.ECHO_ENHANCEMENT)
    assert coordinator.wait_until_idle()
    schedule_state(3, 0.6, "material", PageType.ENHANCEMENT_MATERIAL_SELECTION)
    assert analyzer.material_prepared.wait(1.0)
    schedule_state(5, 1.2, "target", PageType.ECHO_ENHANCEMENT)
    analyzer.release_material.set()

    assert coordinator.wait_until_idle()
    assert [result.source_sequence for result in results] == [2, 4, 6]
    assert tuple(delegate.tracker.sessions) == ("echo-session-0001", "echo-session-0002")
    source_session = delegate.tracker.sessions["echo-session-0001"]
    assert source_session.was_consumed is True
    assert source_session.consumed_into_session_id == "echo-session-0002"
    assert coordinator.stop()


@pytest.mark.parametrize("resolution", ["1920x1080", "2560x1440"])
def test_fake_capture_runtime_releases_every_frame_and_writes_no_files(
    tmp_path, resolution
) -> None:
    width, height = (int(value) for value in resolution.split("x"))
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    backend = FakeCaptureBackend(
        [frame] * 14, monitors=(MonitorInfo(1, 0, 0, width, height, True),)
    )
    results = []
    errors = []
    content_seen = Event()
    analyzer = FakeRoiAnalyzer(
        {
            "probe:unclassified": AnalysisResult(
                page_type="echo_enhancement",
                relevant=True,
                confidence=0.96,
                observation={"page_type": "echo_enhancement"},
                request_content=True,
            ),
            "content:echo_enhancement": AnalysisResult(
                page_type="echo_enhancement",
                relevant=True,
                confidence=0.97,
                observation={"name": "不熄獵手", "level": 5},
                request_content=True,
            ),
        }
    )

    def receive(result) -> None:  # type: ignore[no-untyped-def]
        results.append(result)
        if result.packet_kind is RoiPacketKind.CONTENT:
            content_seen.set()

    before = tuple(tmp_path.rglob("*"))
    runtime = LiveCaptureRuntime(
        backend,
        CaptureConfig(monitor_index=1, sample_hz=8.0, resolution=resolution),
        analyzer,
        stable_duration_seconds=0.50,
        minimum_stable_samples=2,
        on_result=receive,
        on_error=errors.append,
    )
    runtime.start()

    assert content_seen.wait(4.0)
    assert runtime.stop(timeout=4.0)
    backend.audit.assert_all_released()

    assert errors == []
    assert any(result.packet_kind is RoiPacketKind.PROBE for result in results)
    assert any(result.packet_kind is RoiPacketKind.CONTENT for result in results)
    assert all(not hasattr(result, "packet") for result in results)
    assert tuple(tmp_path.rglob("*")) == before


def test_runtime_invalid_monitor_fails_without_leaking_frame() -> None:
    backend = FakeCaptureBackend([np.zeros((1440, 2560, 3), dtype=np.uint8)])
    errors = []
    runtime = LiveCaptureRuntime(
        backend,
        CaptureConfig(monitor_index=2),
        FakeRoiAnalyzer(
            {},
            default=AnalysisResult("unknown", False, 0.0, {}, request_content=False),
        ),
        on_error=errors.append,
    )

    runtime.start()
    deadline = monotonic() + 2.0
    while runtime.is_running and monotonic() < deadline:
        sleep(0.01)
    assert runtime.stop()

    assert len(errors) == 1
    assert errors[0].error_type == "InvalidMonitorError"
    backend.audit.assert_all_released()


@pytest.mark.parametrize("changed_mid_capture", [False, True])
def test_resolution_mismatch_stops_and_releases_any_acquired_frame(changed_mid_capture) -> None:
    backend = FakeCaptureBackend([np.zeros((1080, 1920, 3), dtype=np.uint8)])
    errors = []
    failed = Event()

    def receive(error):
        errors.append(error)
        failed.set()

    runtime = LiveCaptureRuntime(
        backend,
        CaptureConfig(
            monitor_index=1, resolution="2560x1440" if changed_mid_capture else "1920x1080"
        ),
        FakeRoiAnalyzer({}),
        on_error=receive,
    )
    runtime.start()
    assert failed.wait(3.0)
    assert runtime.stop()
    assert len(errors) == 1 and errors[0].error_type == "InvalidMonitorError"
    assert backend.audit.snapshot.acquired == int(changed_mid_capture)
    backend.audit.assert_all_released()


def _contains_array(value: object) -> bool:
    if isinstance(value, np.ndarray):
        return True
    if isinstance(value, dict):
        return any(_contains_array(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_array(item) for item in value)
    return False


def test_page_recovery_ignores_animated_obsolete_content_layout() -> None:
    from wuwa_assistant.capture import ExtractedRois
    from wuwa_assistant.vision.rois import PAGE_HEADER_ROI

    class PageAnalyzer:
        page = "echo_enhancement"

        def analyze(self, packet: RoiPacket) -> AnalysisResult:
            return AnalysisResult(self.page, True, 0.99, {"name": "test"}, request_content=True)

    analyzer = PageAnalyzer()
    results = []
    runtime = LiveCaptureRuntime(
        FakeCaptureBackend([]),
        CaptureConfig(monitor_index=1),
        analyzer,
        on_result=results.append,
    )
    token = runtime.coordinator.start()
    sequence = 0
    try:
        # Full round trip, including a second return to selection. Obsolete content
        # changes every sample; only the independently observed header settles.
        for page in ["echo_enhancement", "echo_selection", "echo_enhancement", "echo_selection"]:
            analyzer.page = page
            for stage in ("classify", "content"):
                for _ in range(4):
                    sequence += 1
                    timestamp = sequence * 0.25
                    probe = packet(
                        sequence, timestamp, page, roi_patterns={PAGE_HEADER_ROI.name: page}
                    )
                    layout = runtime.content_page_type
                    content = (
                        None
                        if layout is None
                        else packet(
                            sequence,
                            timestamp,
                            page,
                            kind=RoiPacketKind.CONTENT,
                            page_type=layout,
                            state_pattern=(
                                f"animation-{sequence}" if stage == "classify" else page
                            ),
                            roi_patterns={PAGE_HEADER_ROI.name: page},
                        )
                    )
                    runtime.coordinator.submit(
                        token, runtime._route_packet(ExtractedRois(probe, content))
                    )
                assert runtime.coordinator.wait_until_idle()
            assert runtime.content_page_type == page
            assert results[-1].packet_kind is RoiPacketKind.CONTENT
            assert results[-1].analysis.page_type == page
        assert sum(r.packet_kind is RoiPacketKind.PROBE for r in results) == 4
    finally:
        assert runtime.coordinator.stop()
