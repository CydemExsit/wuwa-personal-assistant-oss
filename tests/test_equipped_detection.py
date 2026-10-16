from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from wuwa_assistant.vision.equipped import detect_selected_echo_equipped
from wuwa_assistant.vision.rois import SELECTION_GRID_ROI
from wuwa_assistant.vision.selection_identity import extract_selection_identity


def selection_grid(*, badge: str | None) -> np.ndarray:
    grid = np.zeros((SELECTION_GRID_ROI.height, SELECTION_GRID_ROI.width, 3), dtype=np.uint8)
    x, y, width, height = 190, 120, 152, 191
    # OpenCV centers a thick stroke on the coordinates.  Inset it by two pixels
    # so the detected outer bounds remain exactly ``x, y, width, height``.
    cv2.rectangle(
        grid,
        (x + 2, y + 2),
        (x + width - 3, y + height - 3),
        (100, 200, 220),
        4,
    )
    if badge == "portrait":
        # Cover the selected-card corner before drawing the separate closed
        # portrait frame, matching the game's selected-equipped composition.
        grid[y + 3 : y + 47, x + 3 : x + 47] = (30, 36, 44)
        cv2.rectangle(grid, (x + 5, y + 5), (x + 41, y + 42), (70, 190, 245), 4)
        grid[y + 10 : y + 38, x + 10 : x + 37] = (205, 205, 205)
        for offset in range(12, 37, 6):
            cv2.line(
                grid,
                (x + offset, y + 10),
                (x + offset, y + 37),
                (45, 45, 45),
                2,
            )
    elif badge == "ambiguous":
        grid[y + 3 : y + 47, x + 3 : x + 47] = (30, 36, 44)
        cv2.rectangle(grid, (x + 6, y + 6), (x + 38, y + 26), (70, 190, 245), 4)
    return grid


def test_equipped_detector_reports_closed_portrait_badge_as_equipped() -> None:
    result = detect_selected_echo_equipped(selection_grid(badge="portrait"))

    assert result.is_equipped is True
    assert result.confidence is not None
    assert result.confidence >= 0.85
    assert result.selected_card_bbox is not None


def test_equipped_detector_reports_clear_badge_absence_as_unequipped() -> None:
    result = detect_selected_echo_equipped(selection_grid(badge=None))

    assert result.is_equipped is False
    assert result.confidence is not None
    assert result.confidence >= 0.90
    assert result.selected_card_bbox is not None


def test_equipped_detector_keeps_ambiguous_badge_and_missing_selection_unknown() -> None:
    ambiguous = detect_selected_echo_equipped(selection_grid(badge="ambiguous"))
    missing = detect_selected_echo_equipped(np.zeros_like(selection_grid(badge=None)))

    assert ambiguous.is_equipped is None
    assert ambiguous.confidence is None
    assert ambiguous.selected_card_bbox is not None
    assert missing.is_equipped is None
    assert missing.confidence is None
    assert missing.selected_card_bbox is None


@pytest.mark.parametrize("cell", range(1, 16))
def test_model_ribbon_connected_to_selected_outline_preserves_cell_identity(cell: int) -> None:
    image = np.zeros((SELECTION_GRID_ROI.height, SELECTION_GRID_ROI.width, 3), np.uint8)
    row, column = divmod(cell - 1, 3)
    x, y = 5 + 179 * column, 8 + 220 * row
    cv2.rectangle(image, (x, y), (x + 151, y + 189), (40, 190, 235), 4)
    cv2.circle(image, (x + 90, y + 86), 24, (160, 70, 210), -1)
    before = detect_selected_echo_equipped(image)
    first = extract_selection_identity(image, before.selected_card_bbox)
    assert first is not None and first.selected_cell == cell

    # A background ribbon touches the right frame and continues outside the
    # card. It changes the global external contour, not the frame/art itself.
    cv2.rectangle(image, (x + 151, y + 90), (x + 174, y + 260), (40, 190, 235), -1)
    cv2.rectangle(image, (x + 151, y - 50), (x + 166, y + 95), (40, 190, 235), -1)
    after = detect_selected_echo_equipped(image)
    second = extract_selection_identity(image, after.selected_card_bbox)
    assert second is not None and second.selected_cell == cell
    assert second.card_art_phash == first.card_art_phash
    assert after.is_equipped is None  # recovery does not invent badge absence


@pytest.mark.parametrize("shape", ["solid", "three-sides", "two-frames", "one-connected"])
def test_bounded_outline_recovery_requires_unique_closed_contrasting_frame(shape: str) -> None:
    image = np.zeros((SELECTION_GRID_ROI.height, SELECTION_GRID_ROI.width, 3), np.uint8)
    color = (40, 190, 235)
    if shape == "solid":
        image[:450, :535] = color
    else:
        for x in (5, 184) if shape in {"two-frames", "one-connected"} else (5,):
            cv2.rectangle(image, (x, 8), (x + 151, 197), color, 4)
            if shape != "one-connected" or x == 5:
                cv2.rectangle(image, (x + 151, 98), (x + 174, 400), color, -1)
        if shape == "three-sides":
            image[0:20, 0:155] = 0
    assert detect_selected_echo_equipped(image).selected_card_bbox is None


def test_private_selection_screenshots_calibrate_equipped_badge() -> None:
    private_samples = Path(__file__).resolve().parents[1] / "samples_private"
    paths = {
        True: private_samples / "equipped-selection-emis-slot1.png",
        False: private_samples / "2.png",
    }
    negative_repeat = private_samples / "7.png"
    if not all(path.is_file() for path in (*paths.values(), negative_repeat)):
        pytest.skip("private equipped calibration screenshots are not available")

    for expected, path in paths.items():
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        assert image is not None
        result = detect_selected_echo_equipped(SELECTION_GRID_ROI.crop(image))
        assert result.is_equipped is expected
        assert result.confidence is not None
        assert result.confidence >= 0.85
        assert result.selected_card_bbox is not None

    repeated_image = cv2.imread(str(negative_repeat), cv2.IMREAD_COLOR)
    assert repeated_image is not None
    repeated_result = detect_selected_echo_equipped(SELECTION_GRID_ROI.crop(repeated_image))
    assert repeated_result.is_equipped is False
    assert repeated_result.confidence is not None
