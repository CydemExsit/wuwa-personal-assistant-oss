"""Recovery must not expose or commit a single unstable OCR snapshot."""

from __future__ import annotations

from dataclasses import replace
from itertools import pairwise
from threading import Event
from typing import Any, cast

import cv2
import pytest

from wuwa_assistant.live import LiveEchoRoiAnalyzer, LiveRecognitionCoordinator
from wuwa_assistant.models import EchoObservation, EchoStat, PageClassification, PageType, StatKind
from wuwa_assistant.ocr import FakeOcrEngine

from .test_selection_starvation import _RuntimeHarness, _selection_packet


class _Classifier:
    page = PageType.ECHO_SELECTION

    def classify_rois(self, images: object) -> PageClassification:
        return PageClassification(self.page, 0.99)


class _Parser:
    calls = 0
    value = 8.1
    block_call = -1

    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()

    def parse_rois(self, *args: object, **kwargs: object) -> EchoObservation:
        self.calls += 1
        if self.calls == self.block_call:
            self.started.set()
            assert self.release.wait(5)
        return EchoObservation(
            page_type=PageType.ECHO_SELECTION,
            name="不熄獵手",
            level=5,
            cost=1,
            is_equipped=False,
            sub_stats=[
                EchoStat(
                    stat_kind=StatKind.SUBSTAT,
                    name="暴擊",
                    value=self.value,
                    unit="%",
                    confidence=0.99,
                    name_confidence=0.99,
                    value_confidence=0.99,
                )
            ],
            # Vary confidence/time-like metadata without changing semantic data.
            confidence={"page_type": 0.99 - self.calls / 1000},
        )


class _Harness:
    def __init__(self) -> None:
        self.analyzer = LiveEchoRoiAnalyzer(FakeOcrEngine(), selection_only=True)
        self.classifier = _Classifier()
        self.parser = _Parser()
        self.analyzer.classifier = cast(Any, self.classifier)
        self.analyzer.parser = cast(Any, self.parser)
        self.results = []
        self.coordinator = LiveRecognitionCoordinator(
            self.analyzer,
            selection_recovery=True,
            on_result=self.results.append,
        )
        self.token = self.coordinator.start()
        self.sequence = 0

    def sample(self, *, drain: bool = True, guard: str | None = None) -> None:
        self.sequence += 1
        self.coordinator.submit(
            self.token,
            _selection_packet(
                self.sequence,
                animate_background=True,
                bright_background=True,
                changing_guard=guard,
            ),
        )
        if drain:
            assert self.coordinator.wait_until_idle()

    def next_read(self) -> None:
        count = self.parser.calls
        for _ in range(30):
            self.sample()
            if self.parser.calls > count:
                return
        raise AssertionError("Recovery did not read content")

    def close(self) -> None:
        self.parser.release.set()
        assert self.coordinator.stop()
        assert self.coordinator.stats.analysis_errors == 0
        assert self.coordinator.stats.callback_errors == 0


def test_recovery_requires_consecutive_semantic_agreement_before_tracker_commit() -> None:
    h = _Harness()
    try:
        h.next_read()
        assert not h.results and not h.analyzer.tracker.sessions
        h.parser.value = 9.3
        h.next_read()
        assert not h.results and not h.analyzer.tracker.sessions
        h.next_read()
        assert len(h.results) == 1 and len(h.analyzer.tracker.sessions) == 1
        assert h.results[0].analysis.observation["sub_stats"][0]["value"] == 9.3
        first_session = h.analyzer.tracker.current_session.local_session_id
        cutoff = h.coordinator.request_recheck()
        assert cutoff == h.sequence
        h.next_read()
        assert len(h.results) == 1
        h.next_read()
        assert len(h.results) == 2 and len(h.analyzer.tracker.sessions) == 1
        assert h.analyzer.tracker.current_session.local_session_id == first_session
    finally:
        h.close()


@pytest.mark.parametrize("interruption", ["recheck", "stop", "guard-change"])
def test_second_recovery_read_cannot_commit_after_invalidation(interruption: str) -> None:
    h = _Harness()
    try:
        h.next_read()
        h.parser.block_call = 2
        for _ in range(12):
            h.sample(drain=False)
            if h.parser.started.wait(0.01):
                break
        assert h.parser.started.wait(2)
        if interruption == "recheck":
            h.coordinator.request_recheck()
        elif interruption == "stop":
            h.coordinator.stop(wait=False)
        else:
            # Invalidate identity before the new cell has become stable enough
            # to queue anything; latest-scheduled sequence alone cannot do this.
            if h.sequence % 2 == 1:
                h.sample(drain=False)
            h.sample(drain=False, guard="selected_cell")
        h.parser.release.set()
        assert h.coordinator.wait_until_idle()
        assert not h.results and not h.analyzer.tracker.sessions
        assert h.coordinator.stats.canceled_results_dropped == 1
    finally:
        h.close()


def test_other_page_between_reads_clears_semantic_confirmation() -> None:
    h = _Harness()
    try:
        h.next_read()
        h.classifier.page = PageType.UNKNOWN
        for _ in range(8):
            h.sample()
        assert h.results and all(not r.analysis.relevant for r in h.results)
        assert not h.analyzer.tracker.sessions
        h.classifier.page = PageType.ECHO_SELECTION
        h.next_read()
        assert not h.analyzer.tracker.sessions
        h.next_read()
        assert len(h.analyzer.tracker.sessions) == 1
    finally:
        h.close()


@pytest.mark.parametrize("recheck", [False, True])
def test_animated_header_cannot_starve_startup_or_manual_recheck(recheck: bool) -> None:
    h = _RuntimeHarness()
    try:
        if recheck:
            h.classify_selection()
            assert h.runtime.request_recheck() is not None
        for _ in range(40):
            h.sample(animate_header=True, bright_background=True)
        assert h.content_results()
        assert h.runtime.coordinator.stats.analysis_started < 15
    finally:
        assert h.runtime.coordinator.stop()


def test_recovery_retry_rate_is_bounded_and_can_recover_from_ocr_error() -> None:
    h = _Harness()
    original = h.parser.parse_rois
    attempts = []

    def flaky(*args: object, **kwargs: object) -> EchoObservation:
        attempts.append(h.sequence)
        if len(attempts) == 1:
            raise RuntimeError("transient OCR failure")
        return original(*args, **kwargs)

    h.analyzer.parser.parse_rois = flaky
    try:
        for _ in range(40):
            h.sample()
        assert h.results and len(h.analyzer.tracker.sessions) == 1
        assert attempts[1] - attempts[0] >= 10  # Two-second error backoff.
        assert all(b - a >= 5 for a, b in pairwise(attempts))
        assert h.coordinator.stats.analysis_errors == 1
    finally:
        assert h.coordinator.stop()


def test_connected_gold_animation_during_slow_ocr_keeps_confirmation_valid() -> None:
    h = _Harness()
    h.parser.block_call = 2

    def sample() -> None:
        h.sequence += 1
        item = _selection_packet(h.sequence, animate_background=True, bright_background=True)
        if h.sequence % 2:
            images = dict(item.images)
            grid = images["selection_grid"].copy()
            cv2.rectangle(grid, (156, 90), (178, 400), (40, 190, 235), -1)
            images["selection_grid"] = grid
            item = replace(item, images=images)
        h.coordinator.submit(h.token, item)

    try:
        h.next_read()
        for _ in range(15):
            sample()
            if h.parser.started.wait(0.01):
                break
        assert h.parser.started.wait(2)
        # Capture continues while content OCR runs, so recovery must survive
        # both disconnected and model-connected outlines on the same card.
        for _ in range(4):
            sample()
        h.parser.release.set()
        assert h.coordinator.wait_until_idle()
        assert h.results
        assert len(h.analyzer.tracker.sessions) == 1
        assert h.results[-1].analysis.observation["name"] == "不熄獵手"
    finally:
        h.close()
