"""Conservative visual state for the selection-page discard toggle."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True, slots=True)
class DiscardDetection:
    """Tri-state discard result from one fixed, bounded toggle crop."""

    is_discarded: bool | None
    confidence: float | None


def detect_selection_discard_toggle(image: NDArray[np.uint8]) -> DiscardDetection:
    """Return red=discarded, clear neutral icon=not discarded, else unknown.

    Thresholds were calibrated against the user's private 2560 x 1440
    negative/positive samples.  No OCR, identity inference, or pixel persistence
    is involved.
    """

    if (
        not isinstance(image, np.ndarray)
        or image.dtype != np.uint8
        or image.ndim != 3
        or image.shape[2] != 3
        or image.size == 0
    ):
        return DiscardDetection(None, None)

    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    hue = hsv[:, :, 0]
    saturation = hsv[:, :, 1]
    value = hsv[:, :, 2]
    red = ((hue <= 10) | (hue >= 170)) & (saturation >= 80) & (value >= 100)
    red_ratio = float(np.count_nonzero(red)) / float(red.size)
    bright_ratio = float(np.count_nonzero(value >= 170)) / float(value.size)

    if red_ratio >= 0.02:
        return DiscardDetection(True, round(min(0.99, 0.90 + red_ratio), 4))
    if red_ratio <= 0.005 and bright_ratio >= 0.03:
        return DiscardDetection(False, round(min(0.95, 0.88 + bright_ratio * 0.5), 4))
    return DiscardDetection(None, None)


__all__ = ["DiscardDetection", "detect_selection_discard_toggle"]
