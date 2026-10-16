from __future__ import annotations

import cv2
import numpy as np
import pytest

from wuwa_assistant.vision.equipped_slot import (
    detect_selected_equipped_slot,
    highlighted_equipped_slot,
)
from wuwa_assistant.vision.rois import SELECTION_EQUIPPED_SLOTS_ROI, SELECTION_GRID_ROI

_CENTERS_Y = (100, 315, 459, 603, 747)
_RINGS_Y = (99, 321, 465, 609, 753)
_BBOX = (5, 8, 152, 190)


def _art(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    image = np.zeros((190, 152, 3), np.uint8)
    for _ in range(80):
        point = tuple(int(value) for value in rng.integers((8, 8), (140, 160)))
        color = tuple(int(value) for value in rng.integers(20, 240, size=3))
        cv2.circle(image, point, int(rng.integers(2, 8)), color, -1)
    return image


def _put_art(strip: np.ndarray, slot: int, art: np.ndarray) -> None:
    radius = 78 if slot == 1 else 58
    scale = 0.98 if slot == 1 else 0.74
    transform = np.float32([[scale, 0, 8 if slot == 1 else 3], [0, scale, 3 if slot == 1 else 7]])
    patch = cv2.warpAffine(art, transform, (radius * 2, radius * 2))
    mask = np.zeros(patch.shape[:2], np.uint8)
    cv2.circle(mask, (radius, radius), radius - 8, 255, -1)
    center_y = _CENTERS_Y[slot - 1]
    target = strip[center_y - radius : center_y + radius, 110 - radius : 110 + radius]
    target[mask > 0] = patch[mask > 0]


def _highlight(strip: np.ndarray, slot: int) -> None:
    radius = 75 if slot == 1 else 55
    cv2.circle(strip, (112, _RINGS_Y[slot - 1]), radius, (120, 220, 240), 4)


def _fixture(slot: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    strip = np.zeros(
        (SELECTION_EQUIPPED_SLOTS_ROI.height, SELECTION_EQUIPPED_SLOTS_ROI.width, 3), np.uint8
    )
    grid = np.zeros((SELECTION_GRID_ROI.height, SELECTION_GRID_ROI.width, 3), np.uint8)
    selected = _art(42)
    grid[8:198, 5:157] = selected
    for index in range(1, 6):
        _put_art(strip, index, selected if index == slot else _art(index + 100))
    _highlight(strip, slot)
    return strip, grid, selected


@pytest.mark.parametrize("slot", range(1, 6))
def test_unique_active_artwork_match_recognizes_each_permanent_position(slot: int) -> None:
    strip, grid, _ = _fixture(slot)
    result = detect_selected_equipped_slot(strip, grid, _BBOX)

    assert highlighted_equipped_slot(strip) == slot
    assert result.slot_index == slot
    assert result.confidence is not None and result.confidence >= 0.90
    assert result.evidence == "active_ring_and_unique_artwork_match"
    assert not hasattr(result, "pixels")


def test_highlight_does_not_assign_an_unrelated_preview_card() -> None:
    strip, grid, _ = _fixture(3)
    grid[8:198, 5:157] = _art(999)

    assert highlighted_equipped_slot(strip) == 3
    assert detect_selected_equipped_slot(strip, grid, _BBOX).slot_index is None


def test_matching_card_in_a_different_nonhighlighted_slot_is_not_an_assignment() -> None:
    strip, grid, selected = _fixture(3)
    _put_art(strip, 3, _art(990))
    _put_art(strip, 4, selected)

    assert highlighted_equipped_slot(strip) == 3
    assert detect_selected_equipped_slot(strip, grid, _BBOX).slot_index is None


def test_duplicate_artwork_in_multiple_positions_remains_ambiguous() -> None:
    strip, grid, selected = _fixture(3)
    _put_art(strip, 5, selected)

    assert detect_selected_equipped_slot(strip, grid, _BBOX).slot_index is None


def test_two_navigation_highlights_fail_closed_even_with_matching_artwork() -> None:
    strip, grid, _ = _fixture(3)
    _highlight(strip, 4)

    assert highlighted_equipped_slot(strip) is None
    assert detect_selected_equipped_slot(strip, grid, _BBOX).slot_index is None


def test_artwork_match_without_visible_navigation_highlight_is_insufficient() -> None:
    strip, grid, selected = _fixture(3)
    strip.fill(0)
    _put_art(strip, 3, selected)

    assert detect_selected_equipped_slot(strip, grid, _BBOX).slot_index is None


def test_no_features_missing_selection_and_wrong_crop_sizes_fail_closed() -> None:
    strip, grid, _ = _fixture(1)
    assert detect_selected_equipped_slot(strip, grid, None).slot_index is None
    assert detect_selected_equipped_slot(strip[:20], grid, _BBOX).slot_index is None
    assert detect_selected_equipped_slot(strip, grid[:20], _BBOX).slot_index is None
    assert detect_selected_equipped_slot(strip, grid, (-1, 8, 152, 190)).slot_index is None
    grid.fill(0)
    assert detect_selected_equipped_slot(strip, grid, _BBOX).slot_index is None
