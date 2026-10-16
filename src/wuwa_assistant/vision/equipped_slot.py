"""Same-frame evidence for the selected echo's permanent equipment position.

A gold ring alone identifies the currently browsed equipment position, which can
remain highlighted while an unrelated inventory card is previewed.  A position
is returned only when a unique highlighted circle also uniquely matches the
selected card's artwork.  Callers must additionally require explicit equipped
owner text; these pixels do not establish a character identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import atan2, degrees, hypot
from typing import Final

import cv2
import numpy as np
from numpy.typing import NDArray

from .rois import SELECTION_EQUIPPED_SLOTS_ROI, SELECTION_GRID_ROI

# Coordinates relative to SELECTION_EQUIPPED_SLOTS_ROI, measured at 2560x1440.
_RING_CENTERS_Y: Final = (99, 321, 465, 609, 753)
_ART_CENTERS_Y: Final = (100, 315, 459, 603, 747)
_RING_CENTER_X: Final = 112
_ART_CENTER_X: Final = 110


@dataclass(frozen=True, slots=True)
class EquippedSlotDetection:
    slot_index: int | None = None
    confidence: float | None = None
    evidence: str | None = None


def _valid_bgr(image: object, height: int, width: int) -> bool:
    return (
        isinstance(image, np.ndarray)
        and image.dtype == np.uint8
        and image.shape == (height, width, 3)
    )


def highlighted_equipped_slot(strip: NDArray[np.uint8]) -> int | None:
    """Return only a unique navigation highlight, never an echo assignment.

    The live scheduler uses this bounded marker instead of hashing the entire
    equipment strip.  Artwork/background animation therefore cannot reset its
    stability timer.  Assignments require ``detect_selected_equipped_slot``.
    """

    if not _valid_bgr(
        strip, SELECTION_EQUIPPED_SLOTS_ROI.height, SELECTION_EQUIPPED_SLOTS_ROI.width
    ):
        return None
    hsv = cv2.cvtColor(strip, cv2.COLOR_BGR2HSV)
    gold = cv2.inRange(hsv, np.array((15, 30, 140), np.uint8), np.array((45, 255, 255), np.uint8))
    candidates: list[int] = []
    for slot_index, center_y in enumerate(_RING_CENTERS_Y, start=1):
        inner, outer = (70, 80) if slot_index == 1 else (51, 60)
        patch = gold[
            center_y - outer : center_y + outer + 1,
            _RING_CENTER_X - outer : _RING_CENTER_X + outer + 1,
        ]
        y, x = np.indices(patch.shape)
        distance = np.hypot(x - outer, y - outer)
        angle = np.arctan2(y - outer, x - outer)
        annulus = (distance >= inner) & (distance <= outer)
        sectors = []
        for sector in range(12):
            area = (
                annulus
                & (angle >= -np.pi + sector * np.pi / 6)
                & (angle < -np.pi + (sector + 1) * np.pi / 6)
            )
            sectors.append(float(np.count_nonzero(patch[area])) / np.count_nonzero(area))
        # COST numerals and bright artwork can cover one or two sectors.  A
        # selected ring must have broad support around the circumference; lower
        # sectors may be obscured by the level/footer and elemental badge.
        if sum(value >= 0.20 for value in sectors) >= 8 and np.mean(sectors) >= 0.22:
            candidates.append(slot_index)
    return candidates[0] if len(candidates) == 1 else None


def detect_selected_equipped_slot(
    strip: NDArray[np.uint8],
    grid: NDArray[np.uint8],
    selected_card_bbox: tuple[int, int, int, int] | None,
) -> EquippedSlotDetection:
    """Require an active ring and unique geometrically verified artwork match.

    No state or image is retained, and slot numbers are never derived from the
    inventory grid's cell number.  Repeated artwork in two equipment positions,
    partial/missing outlines and feature-poor images all remain unknown.
    """

    active_slot = highlighted_equipped_slot(strip)
    if (
        active_slot is None
        or selected_card_bbox is None
        or not _valid_bgr(grid, SELECTION_GRID_ROI.height, SELECTION_GRID_ROI.width)
    ):
        return EquippedSlotDetection()
    x, y, width, height = selected_card_bbox
    if (
        min(x, y) < 0
        or not 130 <= width <= 175
        or not 170 <= height <= 220
        or x + width > grid.shape[1]
        or y + height > grid.shape[0]
    ):
        return EquippedSlotDetection()

    card = grid[y : y + height, x : x + width]
    card_mask = np.zeros((height, width), np.uint8)
    card_mask[8 : height - 35, 8 : width - 8] = 255
    card_mask[:48, :48] = 0  # exclude the owner portrait, outline and level footer
    extractor = cv2.SIFT.create(nfeatures=150, contrastThreshold=0.015)
    keypoints, descriptors = extractor.detectAndCompute(
        cv2.cvtColor(card, cv2.COLOR_BGR2GRAY), card_mask
    )
    if descriptors is None or len(keypoints) < 8:
        return EquippedSlotDetection()

    matches: list[tuple[int, int]] = []
    for slot_index, center_y in enumerate(_ART_CENTERS_Y, start=1):
        radius = 78 if slot_index == 1 else 58
        artwork = strip[
            center_y - radius : center_y + radius, _ART_CENTER_X - radius : _ART_CENTER_X + radius
        ]
        mask = np.zeros(artwork.shape[:2], np.uint8)
        cv2.circle(mask, (radius, radius), radius - 10, 255, -1)
        target_points, target_descriptors = extractor.detectAndCompute(
            cv2.cvtColor(artwork, cv2.COLOR_BGR2GRAY), mask
        )
        if target_descriptors is None or len(target_points) < 8:
            continue
        pairs = cv2.BFMatcher().knnMatch(descriptors, target_descriptors, k=2)
        good = [first for first, second in pairs if first.distance < 0.70 * second.distance]
        if len(good) < 8:
            continue
        source = np.asarray([keypoints[item.queryIdx].pt for item in good], dtype=np.float32)
        target = np.asarray([target_points[item.trainIdx].pt for item in good], dtype=np.float32)
        transform, inliers = cv2.estimateAffinePartial2D(
            source, target, method=cv2.RANSAC, ransacReprojThreshold=2.0
        )
        if transform is None or inliers is None:
            continue
        selected = inliers.ravel().astype(bool)
        count = int(np.count_nonzero(selected))
        points = source[selected]
        scale = hypot(float(transform[0, 0]), float(transform[1, 0]))
        rotation = abs(degrees(atan2(float(transform[1, 0]), float(transform[0, 0]))))
        expected_scale = (0.88, 1.08) if slot_index == 1 else (0.65, 0.83)
        if (
            count < 8
            or count / len(good) < 0.75
            or len(np.unique(np.round(points), axis=0)) < 8
            or cv2.contourArea(cv2.convexHull(points)) < 500.0
            or min(np.ptp(points, axis=0)) < 24.0
            or not expected_scale[0] <= scale <= expected_scale[1]
            or rotation > 6.0
            or not -12 <= transform[0, 2] <= 28
            or not -12 <= transform[1, 2] <= 35
        ):
            continue
        matches.append((slot_index, count))

    if len(matches) != 1 or matches[0][0] != active_slot:
        return EquippedSlotDetection()
    # This is a calibrated evidence score, not a probability or physical echo ID.
    return EquippedSlotDetection(active_slot, 0.95, "active_ring_and_unique_artwork_match")


__all__ = ["EquippedSlotDetection", "detect_selected_equipped_slot", "highlighted_equipped_slot"]
