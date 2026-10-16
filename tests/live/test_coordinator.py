from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType

import cv2
import numpy as np
import pytest

from wuwa_assistant.capture import RoiPacket
from wuwa_assistant.live import (
    AnalysisResult,
    FakeRoiAnalyzer,
    LiveRecognitionCoordinator,
    LiveStatus,
    PacketDisposition,
    RoiPacketKind,
)

from .helpers import accepted_result, packet


def test_transient_unknown_header_retries_without_pixel_change_or_restart() -> None:
    class RecoveringAnalyzer:
        calls = 0

        def analyze(self, item: RoiPacket) -> AnalysisResult:
            del item
            self.calls += 1
            return accepted_result(relevant=self.calls > 1, request_content=self.calls > 1)

    analyzer = RecoveringAnalyzer()
    results = []
    coordinator = LiveRecognitionCoordinator(analyzer, on_result=results.append)
    token = coordinator.start()
    try:
        for seq, now in enumerate((0.0, 0.3, 0.7), 1):
            coordinator.submit(token, packet(seq, now, "same-page"))
        assert coordinator.wait_until_idle()
        assert analyzer.calls == 1 and not results[0].analysis.relevant
        for seq, now in enumerate((1.0, 1.5, 2.0), 4):
            coordinator.submit(token, packet(seq, now, "same-page"))
        assert coordinator.wait_until_idle() and analyzer.calls == 1
        coordinator.submit(token, packet(7, 3.5, "same-page"))
        assert coordinator.wait_until_idle()
        assert analyzer.calls == 2 and results[-1].analysis.relevant
    finally:
        coordinator.stop()


def _selection_packet(
    sequence: int,
    captured_at: float,
    *,
    selected_cell: int,
    artwork_variant: int = 0,
    status_pattern: str = "status",
    discarded: bool = False,
) -> RoiPacket:
    base = packet(
        sequence,
        captured_at,
        f"selection-{sequence}",
        kind=RoiPacketKind.CONTENT,
        page_type="echo_selection",
        state_pattern="same-visible-text",
        roi_patterns={
            name: f"{status_pattern}-{name}-{sequence}"
            for name in (
                "selection_card_status_1_1",
                "selection_card_status_1_2",
                "selection_card_status_1_3",
                "selection_card_status_2_1",
                "selection_card_status_2_2",
                "selection_card_status_2_3",
                "selection_card_status_3_1",
                "selection_card_status_3_2",
                "selection_card_status_3_3",
                "selection_card_status_4_1",
                "selection_card_status_4_2",
                "selection_card_status_4_3",
                "selection_card_status_5_1",
                "selection_card_status_5_2",
                "selection_card_status_5_3",
            )
        },
    )
    images = dict(base.images)
    grid = np.zeros_like(images["selection_grid"])
    index = selected_cell - 1
    row, column = divmod(index, 3)
    x = 5 + 179 * column
    y = 8 + 220 * row
    cv2.rectangle(grid, (x, y), (x + 151, y + 189), (40, 190, 235), 5)
    # A non-uniform center gives pHash a meaningful card-art marker.
    cv2.circle(
        grid,
        (x + 92, y + 85),
        26,
        (80 + artwork_variant * 30, 150, 230),
        -1,
    )
    if artwork_variant:
        cv2.line(grid, (x + 55, y + 45), (x + 135, y + 125), (255, 255, 255), 8)
    images["selection_grid"] = grid
    toggle = np.zeros_like(images["selection_discard_toggle"])
    toggle_color = (30, 30, 230) if discarded else (230, 230, 230)
    cv2.circle(toggle, (35, 33), 18, toggle_color, 4)
    images["selection_discard_toggle"] = toggle
    return replace(base, images=MappingProxyType(images))


def test_duration_and_sample_stability_then_duplicate_suppression() -> None:
    statuses = []
    results = []
    analyzer = FakeRoiAnalyzer({"state-a": accepted_result()})
    coordinator = LiveRecognitionCoordinator(
        analyzer,
        stable_duration_seconds=0.50,
        minimum_stable_samples=3,
        on_status=statuses.append,
        on_result=results.append,
    )
    token = coordinator.start()

    assert coordinator.submit(token, packet(1, 0.00, "state-a")) is PacketDisposition.OBSERVING
    assert coordinator.submit(token, packet(2, 0.25, "state-a")) is PacketDisposition.OBSERVING
    assert coordinator.submit(token, packet(3, 0.49, "state-a")) is PacketDisposition.OBSERVING
    assert coordinator.submit(token, packet(4, 0.50, "state-a")) is PacketDisposition.QUEUED
    assert coordinator.wait_until_idle()
    assert len(results) == 1
    assert results[0].source_sequence == 4
    assert not hasattr(results[0], "packet")
    assert analyzer.calls == ("state-a",)

    assert (
        coordinator.submit(token, packet(5, 0.75, "state-a")) is PacketDisposition.DROPPED_DUPLICATE
    )
    assert coordinator.stats.duplicate_states_dropped == 1
    assert [update.status for update in statuses] == [
        LiveStatus.WAITING_FOR_PAGE,
        LiveStatus.TRANSITIONING,
        LiveStatus.STABLE,
        LiveStatus.RECOGNIZING,
        LiveStatus.STABLE,
    ]
    assert coordinator.stop()
    assert coordinator.status is LiveStatus.STOPPED
    assert not coordinator.worker_alive


def test_stale_generation_and_out_of_order_packets_are_dropped() -> None:
    analyzer = FakeRoiAnalyzer({}, default=accepted_result())
    coordinator = LiveRecognitionCoordinator(analyzer)
    old_token = coordinator.start()
    assert coordinator.stop()
    current_token = coordinator.start()

    assert (
        coordinator.submit(old_token, packet(1, 0.0, "old"))
        is PacketDisposition.DROPPED_STALE_GENERATION
    )
    assert coordinator.submit(current_token, packet(1, 0.0, "new")) is PacketDisposition.OBSERVING
    assert (
        coordinator.submit(current_token, packet(1, 0.1, "new"))
        is PacketDisposition.DROPPED_OUT_OF_ORDER
    )
    assert coordinator.stats.stale_generation_dropped == 1
    assert coordinator.stats.out_of_order_dropped == 1
    assert coordinator.stop()


def test_low_confidence_and_irrelevant_results_drive_structured_status() -> None:
    statuses = []
    results = []
    analyzer = FakeRoiAnalyzer(
        {
            "low": accepted_result(confidence=0.70),
            "other": accepted_result(confidence=0.99, relevant=False),
        }
    )
    coordinator = LiveRecognitionCoordinator(
        analyzer,
        stable_duration_seconds=0.50,
        minimum_stable_samples=2,
        on_status=statuses.append,
        on_result=results.append,
    )
    token = coordinator.start()

    coordinator.submit(token, packet(1, 0.0, "low"))
    coordinator.submit(token, packet(2, 0.5, "low"))
    assert coordinator.wait_until_idle()
    assert coordinator.status is LiveStatus.LOW_CONFIDENCE

    coordinator.submit(token, packet(3, 0.6, "other"))
    coordinator.submit(token, packet(4, 1.1, "other"))
    assert coordinator.wait_until_idle()
    assert coordinator.status is LiveStatus.WAITING_FOR_PAGE
    assert len(results) == 2
    assert statuses[-1].reason == "stable page is not an echo enhancement page"
    assert coordinator.stop()


def test_probe_classification_can_request_content_with_the_same_visual_state() -> None:
    results = []
    analyzer = FakeRoiAnalyzer(
        {
            "probe-same": accepted_result(request_content=True),
            "content-same": accepted_result(),
        }
    )
    coordinator = LiveRecognitionCoordinator(
        analyzer,
        stable_duration_seconds=0.50,
        minimum_stable_samples=2,
        on_result=results.append,
    )
    token = coordinator.start()

    coordinator.submit(token, packet(1, 0.0, "probe-same", state_pattern="same"))
    coordinator.submit(token, packet(2, 0.5, "probe-same", state_pattern="same"))
    assert coordinator.wait_until_idle()
    assert coordinator.status is LiveStatus.TRANSITIONING
    assert results[-1].analysis.request_content

    coordinator.submit(
        token,
        packet(
            3,
            0.6,
            "content-same",
            kind=RoiPacketKind.CONTENT,
            page_type="echo_enhancement",
            state_pattern="same",
        ),
    )
    coordinator.submit(
        token,
        packet(
            4,
            1.1,
            "content-same",
            kind=RoiPacketKind.CONTENT,
            page_type="echo_enhancement",
            state_pattern="same",
        ),
    )
    assert coordinator.wait_until_idle()

    assert [result.packet_kind for result in results] == [
        RoiPacketKind.PROBE,
        RoiPacketKind.CONTENT,
    ]
    assert analyzer.calls == ("probe-same", "content-same")
    assert coordinator.stop()


@pytest.mark.parametrize(
    ("kind", "page_type", "animated_rois"),
    [
        (RoiPacketKind.PROBE, None, ("enhancement_state", "material_cta")),
        (
            RoiPacketKind.CONTENT,
            "echo_enhancement",
            ("echo_info_panel", "enhancement_state", "material_cta"),
        ),
        (
            RoiPacketKind.CONTENT,
            "echo_selection",
            (
                "selection_detail",
                "selection_grid",
                "enhancement_state",
                "material_cta",
                "selection_set_effects",
                "selection_equipped_slots",
            ),
        ),
    ],
)
def test_parser_and_animated_rois_do_not_restart_stability(
    kind: RoiPacketKind,
    page_type: str | None,
    animated_rois: tuple[str, ...],
) -> None:
    analyzer = FakeRoiAnalyzer({}, default=accepted_result())
    coordinator = LiveRecognitionCoordinator(
        analyzer,
        stable_duration_seconds=0.50,
        minimum_stable_samples=3,
    )
    token = coordinator.start()

    for sequence, captured_at in enumerate((0.0, 0.25, 0.50), start=1):
        disposition = coordinator.submit(
            token,
            packet(
                sequence,
                captured_at,
                f"animation-{sequence}",
                kind=kind,
                page_type=page_type,
                state_pattern="stable-semantic-fields",
                roi_patterns={name: f"moving-{name}-{sequence}" for name in animated_rois},
            ),
        )

    assert disposition is PacketDisposition.QUEUED
    assert coordinator.wait_until_idle()
    assert coordinator.stats.unstable_samples == 2
    assert coordinator.stats.scheduled == 1
    assert coordinator.stop()


def test_animated_selection_card_statuses_do_not_block_content_ocr() -> None:
    analyzer = FakeRoiAnalyzer({}, default=accepted_result())
    coordinator = LiveRecognitionCoordinator(
        analyzer,
        stable_duration_seconds=0.60,
        minimum_stable_samples=3,
    )
    token = coordinator.start()

    dispositions = [
        coordinator.submit(
            token,
            _selection_packet(
                sequence,
                captured_at,
                selected_cell=1,
                status_pattern=f"animated-status-{sequence}",
            ),
        )
        for sequence, captured_at in enumerate((0.0, 0.2, 0.4, 0.6), start=1)
    ]

    assert dispositions[-1] is PacketDisposition.QUEUED
    assert coordinator.wait_until_idle()
    assert coordinator.stats.scheduled == 1
    assert coordinator.stop()


@pytest.mark.parametrize(
    ("first_cell", "second_cell", "second_artwork"),
    [(1, 2, 0), (13, 14, 0), (1, 1, 1)],
)
def test_selection_cell_or_selected_artwork_change_creates_a_new_state(
    first_cell: int,
    second_cell: int,
    second_artwork: int,
) -> None:
    analyzer = FakeRoiAnalyzer({}, default=accepted_result())
    coordinator = LiveRecognitionCoordinator(
        analyzer,
        stable_duration_seconds=0.60,
        minimum_stable_samples=3,
    )
    token = coordinator.start()

    for sequence, captured_at in enumerate((0.0, 0.2, 0.4, 0.6), start=1):
        disposition = coordinator.submit(
            token,
            _selection_packet(sequence, captured_at, selected_cell=first_cell),
        )
    assert disposition is PacketDisposition.QUEUED
    assert coordinator.wait_until_idle()

    for sequence, captured_at in enumerate((0.8, 1.0, 1.2, 1.41), start=5):
        disposition = coordinator.submit(
            token,
            _selection_packet(
                sequence,
                captured_at,
                selected_cell=second_cell,
                artwork_variant=second_artwork,
            ),
        )
    assert disposition is PacketDisposition.QUEUED
    assert coordinator.wait_until_idle()
    assert coordinator.stats.scheduled == 2
    assert coordinator.stop()


def test_selection_discard_toggle_change_creates_a_new_state() -> None:
    analyzer = FakeRoiAnalyzer({}, default=accepted_result())
    coordinator = LiveRecognitionCoordinator(
        analyzer,
        stable_duration_seconds=0.60,
        minimum_stable_samples=3,
    )
    token = coordinator.start()

    for sequence, captured_at in enumerate((0.0, 0.2, 0.4, 0.6), start=1):
        disposition = coordinator.submit(
            token,
            _selection_packet(sequence, captured_at, selected_cell=1, discarded=False),
        )
    assert disposition is PacketDisposition.QUEUED
    assert coordinator.wait_until_idle()

    for sequence, captured_at in enumerate((0.8, 1.0, 1.2, 1.41), start=5):
        disposition = coordinator.submit(
            token,
            _selection_packet(sequence, captured_at, selected_cell=1, discarded=True),
        )
    assert disposition is PacketDisposition.QUEUED
    assert coordinator.wait_until_idle()
    assert coordinator.stats.scheduled == 2
    assert coordinator.stop()


def test_active_equipment_position_change_schedules_without_hashing_strip_artwork() -> None:
    analyzer = FakeRoiAnalyzer({}, default=accepted_result())
    coordinator = LiveRecognitionCoordinator(analyzer)
    token = coordinator.start()
    try:
        for state, (center_y, radius) in enumerate(((99, 75), (465, 55))):
            for sample in range(4):
                base = _selection_packet(
                    1 + state * 4 + sample, state + sample * 0.2, selected_cell=1
                )
                images = dict(base.images)
                strip = np.full_like(images["selection_equipped_slots"], sample * 20)
                cv2.circle(strip, (112, center_y), radius, (120, 220, 240), 4)
                images["selection_equipped_slots"] = strip
                disposition = coordinator.submit(token, replace(base, images=images))
            assert disposition is PacketDisposition.QUEUED
            assert coordinator.wait_until_idle()
        assert coordinator.stats.scheduled == 2
    finally:
        coordinator.stop()


@pytest.mark.parametrize("changed_field", ["selected_card", "owner_text"])
def test_selection_change_stabilizes_while_owner_label_background_animates(
    changed_field: str,
) -> None:
    analyzer = FakeRoiAnalyzer({}, default=accepted_result())
    coordinator = LiveRecognitionCoordinator(analyzer)
    token = coordinator.start()
    try:
        for state in range(2):
            selected_cell = state + 1 if changed_field == "selected_card" else 1
            label = "NEW OWNER" if state and changed_field == "owner_text" else "OWNER"
            for sample in range(4):
                sequence = 1 + state * 4 + sample
                captured_at = state + sample * 0.2
                base = _selection_packet(sequence, captured_at, selected_cell=selected_cell)
                images = dict(base.images)
                owner = np.full_like(images["selection_equipped_owner"], 20)
                # A broad moving dark patch models the animated world behind the
                # transparent owner label, while the legible gold text stays fixed.
                x = 30 + sample * 75
                cv2.rectangle(owner, (x, 0), (x + 160, 94), (105, 95, 85), -1)
                cv2.putText(
                    owner, label, (80, 75), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (150, 225, 240), 2
                )
                images["selection_equipped_owner"] = owner
                disposition = coordinator.submit(token, replace(base, images=images))
            assert disposition is PacketDisposition.QUEUED
            assert coordinator.wait_until_idle()
        assert coordinator.stats.scheduled == 2
    finally:
        coordinator.stop()


@pytest.mark.parametrize("changed_roi", ["enhancement_level", "enhancement_substat_1"])
def test_semantic_level_or_substat_change_starts_a_new_stable_state(changed_roi: str) -> None:
    analyzer = FakeRoiAnalyzer({}, default=accepted_result())
    coordinator = LiveRecognitionCoordinator(
        analyzer,
        stable_duration_seconds=0.50,
        minimum_stable_samples=2,
    )
    token = coordinator.start()

    coordinator.submit(
        token,
        packet(
            1,
            0.0,
            "before",
            kind=RoiPacketKind.CONTENT,
            page_type="echo_enhancement",
            state_pattern="base",
        ),
    )
    assert (
        coordinator.submit(
            token,
            packet(
                2,
                0.5,
                "before",
                kind=RoiPacketKind.CONTENT,
                page_type="echo_enhancement",
                state_pattern="base",
            ),
        )
        is PacketDisposition.QUEUED
    )
    assert coordinator.wait_until_idle()

    changed = {changed_roi: f"changed-{changed_roi}"}
    assert (
        coordinator.submit(
            token,
            packet(
                3,
                0.6,
                "after",
                kind=RoiPacketKind.CONTENT,
                page_type="echo_enhancement",
                state_pattern="base",
                roi_patterns=changed,
            ),
        )
        is PacketDisposition.OBSERVING
    )
    assert (
        coordinator.submit(
            token,
            packet(
                4,
                1.1,
                "after",
                kind=RoiPacketKind.CONTENT,
                page_type="echo_enhancement",
                state_pattern="base",
                roi_patterns=changed,
            ),
        )
        is PacketDisposition.QUEUED
    )
    assert coordinator.wait_until_idle()
    assert analyzer.calls == ("before", "after")
    assert coordinator.stats.scheduled == 2
    assert coordinator.stop()


@pytest.mark.parametrize("changed_roi", ["selection_equipped_owner", "selection_primary_action"])
def test_selection_equipped_text_change_starts_a_new_stable_state(
    changed_roi: str,
) -> None:
    analyzer = FakeRoiAnalyzer({}, default=accepted_result())
    coordinator = LiveRecognitionCoordinator(
        analyzer,
        stable_duration_seconds=0.50,
        minimum_stable_samples=2,
    )
    token = coordinator.start()

    for sequence, captured_at in ((1, 0.0), (2, 0.5)):
        coordinator.submit(
            token,
            packet(
                sequence,
                captured_at,
                "before",
                kind=RoiPacketKind.CONTENT,
                page_type="echo_selection",
                state_pattern="same-details",
            ),
        )
    assert coordinator.wait_until_idle()

    changed = {changed_roi: f"changed-{changed_roi}"}
    assert (
        coordinator.submit(
            token,
            packet(
                3,
                0.6,
                "after",
                kind=RoiPacketKind.CONTENT,
                page_type="echo_selection",
                state_pattern="same-details",
                roi_patterns=changed,
            ),
        )
        is PacketDisposition.OBSERVING
    )
    assert (
        coordinator.submit(
            token,
            packet(
                4,
                1.1,
                "after",
                kind=RoiPacketKind.CONTENT,
                page_type="echo_selection",
                state_pattern="same-details",
                roi_patterns=changed,
            ),
        )
        is PacketDisposition.QUEUED
    )
    assert coordinator.wait_until_idle()
    assert analyzer.calls == ("before", "after")
    assert coordinator.stats.scheduled == 2
    assert coordinator.stop()


@pytest.mark.parametrize(
    ("kind", "page_type", "missing_name"),
    [
        (RoiPacketKind.PROBE, None, "page_header"),
        (RoiPacketKind.CONTENT, "echo_enhancement", "enhancement_level"),
    ],
)
def test_fingerprint_fails_closed_when_required_stability_roi_is_missing(
    kind: RoiPacketKind,
    page_type: str | None,
    missing_name: str,
) -> None:
    from wuwa_assistant.live.coordinator import _packet_fingerprint

    roi_packet = packet(1, 0.0, "state", kind=kind, page_type=page_type)
    incomplete = dict(roi_packet.images)
    del incomplete[missing_name]
    object.__setattr__(roi_packet, "images", MappingProxyType(incomplete))

    with pytest.raises(ValueError, match="missing required stability ROI"):
        _packet_fingerprint(roi_packet)


def test_transaction_finalize_failure_becomes_runtime_error_without_killing_worker() -> None:
    class FailingFinalizeAnalyzer:
        def analyze(self, roi_packet: RoiPacket) -> AnalysisResult:
            del roi_packet
            base = accepted_result()
            return type(base)(
                base.page_type,
                base.relevant,
                base.confidence,
                base.observation,
                requires_commit=True,
            )

        def finalize_analysis(self, result: AnalysisResult, *, accept: bool) -> bool:
            del result, accept
            raise RuntimeError("commit failed")

    errors = []
    coordinator = LiveRecognitionCoordinator(
        FailingFinalizeAnalyzer(),
        stable_duration_seconds=0.50,
        minimum_stable_samples=2,
        on_error=errors.append,
    )
    token = coordinator.start()
    coordinator.submit(token, packet(1, 0.0, "state"))
    coordinator.submit(token, packet(2, 0.5, "state"))

    assert coordinator.wait_until_idle()
    assert coordinator.status is LiveStatus.ERROR
    assert coordinator.worker_alive
    assert len(errors) == 1
    assert errors[0].error_type == "RuntimeError"
    assert coordinator.stats.analysis_errors == 1
    assert coordinator.stop()
