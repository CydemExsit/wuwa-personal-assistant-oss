"""Selection-content starvation regressions using pixels, not injected fingerprints.

The page can classify perfectly while a translucent content crop never passes
the all-fields pHash gate. Reclassification and manual reset must eventually
read that content, without weakening selected-card/slot/discard identity guards.
"""

from __future__ import annotations

from dataclasses import replace
from itertools import pairwise

import cv2
import numpy as np
import pytest

from wuwa_assistant.capture import (
    CaptureConfig,
    ExtractedRois,
    FakeCaptureBackend,
    RoiPacket,
    RoiPacketKind,
)
from wuwa_assistant.live import AnalysisResult, LiveCaptureRuntime, LiveRecognitionResult
from wuwa_assistant.vision.preprocess import preprocess_for_hash
from wuwa_assistant.vision.stability import perceptual_hash

from .helpers import packet


def _selection_packet(
    sequence: int,
    *,
    animate_background: bool,
    bright_background: bool = False,
    changing_guard: str | None = None,
) -> RoiPacket:
    base = packet(
        sequence,
        sequence * 0.2,
        "stable-list",
        kind=RoiPacketKind.CONTENT,
        page_type="echo_selection",
    )
    images = dict(base.images)
    panel = np.full_like(images["selection_substat_panel"], 20)
    x = (sequence % 6) * 65 if animate_background else 0
    # Fixed, readable glyphs with a moving dark patch in the unused lower rows.
    # CLAHE/Otsu promotes the background to foreground; the raw pHash changes
    # by 16+ bits while the actual substat line is exactly unchanged.
    patch_color = (190, 190, 190) if bright_background else (105, 95, 85)
    cv2.rectangle(panel, (x, 80), (x + 160, 244), patch_color, -1)
    cv2.putText(panel, "CRIT 8.1%", (30, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (235, 235, 235), 2)
    images["selection_substat_panel"] = panel

    state = sequence % 2
    column = state if changing_guard == "selected_cell" else 0
    x = 5 + 179 * column
    y = 8
    grid = np.zeros_like(images["selection_grid"])
    cv2.rectangle(grid, (x, y), (x + 151, y + 189), (40, 190, 235), 5)
    cv2.circle(grid, (x + 92, y + 85), 26, (80, 150, 230), -1)
    if changing_guard == "selected_artwork" and state:
        cv2.line(grid, (x + 55, y + 45), (x + 135, y + 125), (255, 255, 255), 8)
    images["selection_grid"] = grid

    toggle = np.zeros_like(images["selection_discard_toggle"])
    color = (30, 30, 230) if changing_guard == "discard" and state else (230, 230, 230)
    cv2.circle(toggle, (35, 33), 18, color, 4)
    images["selection_discard_toggle"] = toggle

    strip = np.zeros_like(images["selection_equipped_slots"])
    if changing_guard == "equipped_slot":
        center_y, radius = (99, 75) if state else (465, 55)
        cv2.circle(strip, (112, center_y), radius, (120, 220, 240), 4)
    images["selection_equipped_slots"] = strip
    return replace(base, images=images)


class _SelectionAnalyzer:
    """Keep semantic OCR fixed so only the real visual scheduler is exercised."""

    page = "echo_selection"

    def analyze(self, item: RoiPacket) -> AnalysisResult:
        if self.page != "echo_selection":
            return AnalysisResult(self.page, False, 0.99, {}, request_content=False)
        observation = (
            {"name": "不熄獵手", "level": 5, "cost": 1, "is_equipped": False}
            if item.kind is RoiPacketKind.CONTENT
            else {"page_type": "echo_selection"}
        )
        return AnalysisResult("echo_selection", True, 0.99, observation, request_content=True)


class _RuntimeHarness:
    def __init__(self) -> None:
        self.results: list[LiveRecognitionResult] = []
        self.analyzer = _SelectionAnalyzer()
        self.runtime = LiveCaptureRuntime(
            FakeCaptureBackend([]),
            CaptureConfig(1),
            self.analyzer,
            selection_only=True,
            on_result=self.results.append,
        )
        self.token = self.runtime.coordinator.start()
        self.sequence = 0

    def sample(
        self,
        *,
        animate_background: bool = True,
        bright_background: bool = False,
        changing_guard: str | None = None,
        animate_header: bool = False,
    ) -> None:
        self.sequence += 1
        probe = packet(self.sequence, self.sequence * 0.2, "same-header")
        header = np.full_like(probe.images["page_header"], 20)
        x = (self.sequence % 4) * 55 if animate_header else 0
        cv2.rectangle(header, (x, 50), (x + 100, 99), (105, 95, 85), -1)
        cv2.putText(
            header, "COST 12/12", (10, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (235, 235, 235), 2
        )
        probe = replace(probe, images={**probe.images, "page_header": header})
        content = (
            _selection_packet(
                self.sequence,
                animate_background=animate_background,
                bright_background=bright_background,
                changing_guard=changing_guard,
            )
            if self.runtime.content_page_type == "echo_selection"
            else None
        )
        if content is not None:
            content = replace(content, images={**content.images, "page_header": header})
        routed = self.runtime._route_packet(ExtractedRois(probe, content))
        self.runtime.coordinator.submit(self.token, routed)
        # Drain each exact packet, avoiding wall-clock sleeps and worker races.
        assert self.runtime.coordinator.wait_until_idle()

    def content_results(self, *, after: int = -1) -> list[LiveRecognitionResult]:
        return [
            result
            for result in self.results
            if result.packet_kind is RoiPacketKind.CONTENT and result.source_sequence > after
        ]

    def classify_selection(self) -> None:
        for _ in range(10):
            self.sample(animate_background=False)
            if self.runtime.content_page_type == "echo_selection":
                break
        assert self.runtime.content_page_type == "echo_selection"
        assert not self.content_results()


def test_starvation_fixture_has_static_glyphs_but_changing_visual_hashes() -> None:
    panels = [
        _selection_packet(sequence, animate_background=True).images["selection_substat_panel"]
        for sequence in (1, 2, 3)
    ]
    assert all(np.array_equal(panels[0][:80], panel[:80]) for panel in panels[1:])
    hashes = [perceptual_hash(preprocess_for_hash(panel)) for panel in panels]
    assert all((left ^ right).bit_count() > 4 for left, right in pairwise(hashes))


@pytest.mark.parametrize("manual_recheck", [False, True], ids=["automatic", "manual-recheck"])
@pytest.mark.parametrize(
    "bright_background", [False, True], ids=["dark-world", "bright-decoration"]
)
def test_selection_background_animation_cannot_starve_content_forever(
    manual_recheck: bool, bright_background: bool
) -> None:
    harness = _RuntimeHarness()
    runtime = harness.runtime
    try:
        cutoff = -1
        if manual_recheck:
            for _ in range(12):
                harness.sample(animate_background=False)
            assert harness.content_results()
            cutoff = runtime.request_recheck()
            assert cutoff is not None
            assert runtime.coordinator.generation == harness.token
        # Eight seconds of fixed page/core/identity evidence with a noisy text
        # crop used to emit only successful probes forever, including after reset.
        for _ in range(40):
            harness.sample(bright_background=bright_background)
        assert harness.content_results(after=cutoff), (
            "Confirmed selection page never reached content OCR despite unchanged "
            "readable text and stable selected-card, artwork, discard and slot evidence"
        )
        assert runtime.coordinator.generation == harness.token
        assert runtime.coordinator.stats.analysis_errors == 0
        assert runtime.coordinator.stats.callback_errors == 0
    finally:
        assert runtime.coordinator.stop()


@pytest.mark.parametrize("guard", ["selected_cell", "selected_artwork", "discard", "equipped_slot"])
def test_starvation_recovery_must_not_ignore_changing_identity_guards(guard: str) -> None:
    harness = _RuntimeHarness()
    try:
        for _ in range(40):
            harness.sample(changing_guard=guard)
        assert not harness.content_results(), "An unstable identity was submitted as stable content"
        # Once the identity settles, ongoing background animation must not leave
        # this candidate trapped in the same probe-only loop.
        for _ in range(40):
            harness.sample()
        assert harness.content_results()
    finally:
        assert harness.runtime.coordinator.stop()


def test_confirmed_selection_recovers_when_same_header_background_keeps_changing() -> None:
    harness = _RuntimeHarness()
    try:
        harness.classify_selection()
        # The page label and every content field are static. Only the dark
        # background under the fixed header changes; it must not route back to
        # an unprocessable, perpetually unstable PROBE every time.
        for _ in range(40):
            harness.sample(animate_background=False, animate_header=True)
        assert harness.content_results(), "Header pHash jitter blocked all confirmed-list content"
    finally:
        assert harness.runtime.coordinator.stop()


@pytest.mark.parametrize("page", ["unknown", "echo_enhancement"])
def test_header_jitter_recovery_rejects_pages_that_no_longer_classify_as_selection(
    page: str,
) -> None:
    harness = _RuntimeHarness()
    try:
        harness.classify_selection()
        cutoff = harness.sequence
        harness.analyzer.page = page
        # A stale list layout is allowed to be reclassified, never accepted as
        # a list merely because the previous successful header said selection.
        for _ in range(40):
            harness.sample(animate_header=True)
        fresh_results = [result for result in harness.results if result.source_sequence > cutoff]
        assert fresh_results, "Recovery did not reclassify the changed page at all"
        assert all(result.analysis.page_type == page for result in fresh_results)
        assert all(not result.analysis.relevant for result in fresh_results)
        assert all("name" not in result.analysis.observation for result in fresh_results)
        assert harness.runtime.content_page_type is None
    finally:
        assert harness.runtime.coordinator.stop()
