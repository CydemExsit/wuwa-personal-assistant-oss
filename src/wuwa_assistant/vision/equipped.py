"""Conservative detection of an equipped badge on the selected echo card.

The echo-selection grid marks exactly one selected card with a gold outline.
An equipped card additionally has a small character portrait badge in its
top-left corner.  When that card is selected, the portrait badge itself has a
closed gold rounded-square frame.  This detector only reports a boolean when
both pieces of evidence are clear; partial badge-like shapes remain unknown.

The badge says that *some* character has equipped the echo.  It does not carry
reliable character or slot identity for this detector, so neither is inferred.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True, slots=True)
class EquippedDetection:
    """Tri-state equipped result plus conservative visual confidence."""

    is_equipped: bool | None
    confidence: float | None
    selected_card_bbox: tuple[int, int, int, int] | None = None


def detect_selected_echo_equipped(grid: NDArray[np.uint8]) -> EquippedDetection:
    """Detect whether the selected selection-grid card has a portrait badge.

    ``None`` is returned whenever the selected-card outline is not unique, or
    when the expected badge area contains a partial/ambiguous gold shape.  A
    closed gold badge frame reports ``True``; a clear absence reports ``False``.
    Echo artwork outside the fixed badge area is never used as evidence.
    """

    if not isinstance(grid, np.ndarray) or grid.ndim != 3 or grid.shape[2] != 3:
        return EquippedDetection(None, None)

    hsv = cv2.cvtColor(grid, cv2.COLOR_BGR2HSV)
    gold = cv2.inRange(
        hsv,
        np.array((15, 30, 130), dtype=np.uint8),
        np.array((40, 255, 255), dtype=np.uint8),
    )
    contours, _hierarchy = cv2.findContours(gold, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    candidates: list[tuple[int, int, int, int, float]] = []
    for contour in contours:
        x, y, width, height = cv2.boundingRect(contour)
        if not (130 <= width <= 175 and 170 <= height <= 220):
            continue
        contour_area = float(cv2.contourArea(contour))
        rectangularity = contour_area / float(width * height)
        if rectangularity >= 0.70:
            candidates.append((x, y, width, height, rectangularity))

    # The animated model can connect to the selected outline. Verify local
    # straight edges as well as global contours, and reject competing outlines
    # even when only one of them is connected to background artwork.
    recovered_outline = False
    for bounded in _bounded_outline_candidates(gold.astype(np.uint8, copy=False)):
        if not any(
            abs(bounded[0] - item[0]) <= 14 and abs(bounded[1] - item[1]) <= 14
            for item in candidates
        ):
            candidates.append(bounded)
            recovered_outline = True
    if len(candidates) != 1:
        return EquippedDetection(None, None)

    x, y, width, height, selected_rectangularity = candidates[0]
    # Offset past most of the selected card's outer outline while retaining the
    # complete 40-ish-pixel portrait badge.  The remaining thin outline fragment
    # is long and narrow, so it cannot satisfy the closed-badge geometry below.
    badge = grid[y + 3 : y + 51, x + 3 : x + 51]
    if badge.shape[:2] != (48, 48):
        return EquippedDetection(None, None, (x, y, width, height))

    badge_hsv = cv2.cvtColor(badge, cv2.COLOR_BGR2HSV)
    badge_gold = cv2.inRange(
        badge_hsv,
        np.array((15, 80, 140), dtype=np.uint8),
        np.array((40, 255, 255), dtype=np.uint8),
    )
    badge_contours, _hierarchy = cv2.findContours(
        badge_gold,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )

    strong_badges: list[tuple[float, float]] = []
    has_partial_badge = False
    for contour in badge_contours:
        badge_x, badge_y, badge_width, badge_height = cv2.boundingRect(contour)
        # The calibrated badge is anchored within a few pixels of the card's
        # top-left and measures about 36 x 37 pixels at 2560 x 1440.
        near_badge_anchor = badge_x <= 10 and badge_y <= 10
        badge_sized = 28 <= badge_width <= 44 and 28 <= badge_height <= 44
        if not (near_badge_anchor and badge_sized):
            if near_badge_anchor and 18 <= badge_width <= 44 and 18 <= badge_height <= 44:
                has_partial_badge = True
            continue

        contour_area = float(cv2.contourArea(contour))
        badge_rectangularity = contour_area / float(badge_width * badge_height)
        local_mask = badge_gold[
            badge_y : badge_y + badge_height,
            badge_x : badge_x + badge_width,
        ]
        gold_coverage = float(np.count_nonzero(local_mask)) / float(local_mask.size)
        band = max(3, round(min(badge_width, badge_height) * 0.1))
        side_coverages = (
            float(np.count_nonzero(local_mask[:band, :])) / float(band * badge_width),
            float(np.count_nonzero(local_mask[-band:, :])) / float(band * badge_width),
            float(np.count_nonzero(local_mask[:, :band])) / float(band * badge_height),
            float(np.count_nonzero(local_mask[:, -band:])) / float(band * badge_height),
        )
        minimum_side_coverage = min(side_coverages)

        # A portrait badge is a closed ring: its external contour encloses most
        # of the rounded square, gold occupies a ring rather than a solid block,
        # and all four sides have visible support.  These constraints avoid
        # confusing bright/gold echo artwork with equipped state.
        if (
            badge_rectangularity >= 0.70
            and 0.15 <= gold_coverage <= 0.70
            and minimum_side_coverage >= 0.25
        ):
            strong_badges.append((badge_rectangularity, minimum_side_coverage))
        else:
            has_partial_badge = True

    selected_bbox = (x, y, width, height)
    if len(strong_badges) == 1 and not has_partial_badge:
        badge_rectangularity, minimum_side_coverage = strong_badges[0]
        confidence = min(
            0.99,
            0.55 * selected_rectangularity
            + 0.25 * badge_rectangularity
            + 0.20 * minimum_side_coverage,
        )
        return EquippedDetection(True, round(confidence, 4), selected_bbox)

    if strong_badges or has_partial_badge:
        return EquippedDetection(None, None, selected_bbox)

    if recovered_outline:
        # A connected background can shift the recovered frame by a few pixels.
        # It establishes selection geometry, but must not turn a cropped/white
        # portrait frame into confident evidence that the echo is unequipped.
        return EquippedDetection(None, None, selected_bbox)

    # A unique, nearly rectangular selected-card outline plus no badge-sized
    # shape in the fixed corner is positive evidence of an unequipped card.
    confidence = min(0.95, 0.78 + 0.17 * selected_rectangularity)
    return EquippedDetection(False, round(confidence, 4), selected_bbox)


def _bounded_outline_candidates(mask: NDArray[np.uint8]) -> list[tuple[int, int, int, int, float]]:
    """Recover a unique closed frame when background gold connects to it.

    Four long straight sides and contrast just inside three sides are required.
    A bright ribbon, COST footer, portrait badge or a solid gold patch alone
    cannot establish selection. Missing and multiple outlines stay unknown.
    """
    candidates: list[tuple[int, int, int, int, float]] = []
    for row in range(5):
        for column in range(3):
            x, y = 5 + 179 * column, 8 + 220 * row
            if y + 180 >= mask.shape[0] or x + 143 >= mask.shape[1]:
                continue
            top = _supported_edge(mask, x + 42, x + 140, max(0, y - 6), y + 15, True)
            bottom = _supported_edge(
                mask, x + 20, x + 140, y + 178, min(mask.shape[0], y + 204), True
            )
            left = _supported_edge(mask, y + 48, y + 167, max(0, x - 10), x + 16, False)
            right = _supported_edge(
                mask, y + 48, y + 167, x + 139, min(mask.shape[1], x + 166), False
            )
            if any(edge is None for edge in (top, bottom, left, right)):
                continue
            assert top is not None and bottom is not None and left is not None and right is not None
            # Use the inside end of a thick/connected side, then include the
            # calibrated four-pixel stroke. The exterior may contain the model.
            top_y, bottom_y = top[1] - 3, bottom[0] + 3
            left_x, right_x = left[1] - 3, right[0] + 3
            width, height = right_x - left_x + 1, bottom_y - top_y + 1
            if not (130 <= width <= 175 and 170 <= height <= 220):
                continue
            inside = (
                mask[top_y + 8, x + 42 : x + 140],
                mask[bottom_y - 8, x + 20 : x + 140],
                mask[y + 48 : y + 167, left_x + 8],
                mask[y + 48 : y + 167, right_x - 8],
            )
            if sum(float(np.count_nonzero(line)) / line.size <= 0.60 for line in inside) < 3:
                continue
            candidates.append((left_x, top_y, width, height, 0.95))
    return candidates


def _supported_edge(
    mask: NDArray[np.uint8],
    start: int,
    end: int,
    lower: int,
    upper: int,
    horizontal: bool,
) -> tuple[int, int] | None:
    patch = mask[lower:upper, start:end] if horizontal else mask[start:end, lower:upper]
    support = np.count_nonzero(patch, axis=1 if horizontal else 0) / (end - start)
    positions = np.flatnonzero(support >= 0.80)
    if len(positions) < 2:
        return None
    return lower + int(positions[0]), lower + int(positions[-1])


__all__ = ["EquippedDetection", "detect_selected_echo_equipped"]
