from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from wuwa_assistant.vision.discard import detect_selection_discard_toggle
from wuwa_assistant.vision.rois import SELECTION_DISCARD_TOGGLE_ROI


def _toggle(color: tuple[int, int, int] | None) -> np.ndarray:
    image = np.zeros(
        (SELECTION_DISCARD_TOGGLE_ROI.height, SELECTION_DISCARD_TOGGLE_ROI.width, 3),
        dtype=np.uint8,
    )
    if color is not None:
        cv2.circle(image, (35, 33), 18, color, 4)
    return image


def test_discard_toggle_is_tri_state_and_uses_only_bounded_color_evidence() -> None:
    discarded = detect_selection_discard_toggle(_toggle((30, 30, 230)))
    available = detect_selection_discard_toggle(_toggle((230, 230, 230)))
    unknown = detect_selection_discard_toggle(_toggle(None))

    assert discarded.is_discarded is True
    assert discarded.confidence is not None
    assert available.is_discarded is False
    assert available.confidence is not None
    assert unknown.is_discarded is None
    assert unknown.confidence is None


def test_private_discard_samples_when_available() -> None:
    private_samples = Path(__file__).resolve().parents[1] / "samples_private"
    paths = {False: private_samples / "2.png", True: private_samples / "7.png"}
    if not all(path.is_file() for path in paths.values()):
        pytest.skip("private discard calibration samples are intentionally not committed")

    for expected, path in paths.items():
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        assert image is not None
        crop = SELECTION_DISCARD_TOGGLE_ROI.crop(image, copy=True)
        result = detect_selection_discard_toggle(crop)
        assert result.is_discarded is expected
        assert result.confidence is not None and result.confidence >= 0.88
