from __future__ import annotations

import cv2
import numpy as np
import pytest

from wuwa_assistant.vision.preprocess import crop_roi, validate_reference_frame
from wuwa_assistant.vision.rois import (
    ENHANCEMENT_LEVEL_ROI,
    ENHANCEMENT_SUBSTAT_ROIS,
    REFERENCE_HEIGHT,
    REFERENCE_WIDTH,
)
from wuwa_assistant.vision.stability import (
    SemanticFingerprint,
    StabilityGate,
    hamming_distance,
    perceptual_hash,
    semantic_roi_fingerprint,
)


def _fingerprint(value: int) -> SemanticFingerprint:
    return SemanticFingerprint(("synthetic_field",), (value,))


def _synthetic_frame() -> np.ndarray:
    frame = np.zeros((REFERENCE_HEIGHT, REFERENCE_WIDTH, 3), dtype=np.uint8)
    roi = ENHANCEMENT_LEVEL_ROI
    cv2.putText(
        frame,
        "+15",
        (roi.x1 + 8, roi.y2 - 8),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.1,
        (245, 245, 245),
        2,
        cv2.LINE_AA,
    )
    return frame


def test_fixed_roi_crop_and_reference_validation() -> None:
    frame = _synthetic_frame()
    validate_reference_frame(frame)
    cropped = crop_roi(frame, ENHANCEMENT_SUBSTAT_ROIS[0])
    assert cropped.shape == (
        ENHANCEMENT_SUBSTAT_ROIS[0].height,
        ENHANCEMENT_SUBSTAT_ROIS[0].width,
        3,
    )

    with pytest.raises(ValueError, match="2560x1440"):
        validate_reference_frame(np.zeros((720, 1280, 3), dtype=np.uint8))


def test_phash_is_deterministic_and_hamming_uses_bits() -> None:
    image = np.zeros((96, 160), dtype=np.uint8)
    cv2.putText(image, "+15", (8, 67), cv2.FONT_HERSHEY_SIMPLEX, 1.8, 255, 3)
    assert perceptual_hash(image) == perceptual_hash(image.copy())
    assert hamming_distance(0b1010, 0b0011) == 2


def test_semantic_fingerprint_ignores_pixels_outside_requested_rois() -> None:
    original = _synthetic_frame()
    outside_changed = original.copy()
    cv2.rectangle(outside_changed, (1500, 800), (2200, 1300), (255, 255, 255), -1)

    original_hash = semantic_roi_fingerprint(original, (ENHANCEMENT_LEVEL_ROI,))
    outside_hash = semantic_roi_fingerprint(outside_changed, (ENHANCEMENT_LEVEL_ROI,))
    assert original_hash == outside_hash

    inside_changed = original.copy()
    roi = ENHANCEMENT_LEVEL_ROI
    cv2.line(
        inside_changed,
        (roi.x1, roi.y1),
        (roi.x2 - 1, roi.y2 - 1),
        (255, 255, 255),
        7,
    )
    changed_hash = semantic_roi_fingerprint(inside_changed, (ENHANCEMENT_LEVEL_ROI,))
    assert original_hash.max_distance(changed_hash) > 0


def test_stability_gate_requires_consecutive_frames_and_suppresses_duplicates() -> None:
    gate = StabilityGate(required_consecutive=3, max_hamming_distance=1)
    first = _fingerprint(0)
    near = _fingerprint(1)

    assert not gate.update(first).is_stable
    assert not gate.update(near).is_stable
    accepted = gate.update(first)
    assert accepted.is_stable
    assert accepted.became_stable
    assert accepted.should_process

    repeated = gate.update(first)
    assert repeated.is_stable
    assert not repeated.became_stable
    assert not repeated.should_process


def test_stability_gate_resets_on_change_then_accepts_new_state() -> None:
    gate = StabilityGate(required_consecutive=2, max_hamming_distance=0)
    old = _fingerprint(0)
    new = _fingerprint(0xFFFF)

    assert gate.update(old).consecutive_frames == 1
    assert gate.update(old).should_process

    changed = gate.update(new)
    assert not changed.is_stable
    assert changed.consecutive_frames == 1
    assert changed.candidate_distance == 16

    accepted = gate.update(new)
    assert accepted.is_stable
    assert accepted.should_process

    # A one-frame excursion back to the previous state does not emit anything.
    assert not gate.update(old).should_process
    returned = gate.update(new)
    assert not returned.should_process


def test_incompatible_fingerprint_layouts_are_rejected() -> None:
    left = SemanticFingerprint(("a",), (0,))
    right = SemanticFingerprint(("b",), (0,))
    with pytest.raises(ValueError, match="different ROI layouts"):
        left.max_distance(right)


def test_gate_treats_a_new_roi_layout_as_a_page_change() -> None:
    gate = StabilityGate(required_consecutive=2, max_hamming_distance=0)
    old_page = SemanticFingerprint(("old_page",), (0,))
    new_page = SemanticFingerprint(("new_page",), (0,))

    assert gate.update(old_page).consecutive_frames == 1
    assert gate.update(old_page).should_process
    page_change = gate.update(new_page)
    assert page_change.candidate_distance is None
    assert page_change.consecutive_frames == 1
    assert not page_change.is_stable
    assert gate.update(new_page).should_process
