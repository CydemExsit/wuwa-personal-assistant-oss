from __future__ import annotations

import cv2
import numpy as np

from wuwa_assistant.models.domain import EchoObservation, PageType
from wuwa_assistant.state.tracker import EchoStateTracker
from wuwa_assistant.vision.rois import SELECTION_GRID_ROI
from wuwa_assistant.vision.selection_identity import (
    extract_selection_identity,
    visual_identity_matches,
)


def grid(pattern: int) -> np.ndarray:
    image = np.zeros((SELECTION_GRID_ROI.height, SELECTION_GRID_ROI.width, 3), dtype=np.uint8)
    for row in range(5):
        for column in range(3):
            x = 5 + 179 * column
            y = 8 + 220 * row
            color = (
                (pattern + row * 31 + column * 17) % 255,
                (pattern + row * 13 + column * 43) % 255,
                (pattern + row * 47 + column * 7) % 255,
            )
            cv2.rectangle(image, (x + 45, y + 38), (x + 141, y + 137), color, -1)
            cv2.line(image, (x + 48, y + 42), (x + 138, y + 132), (255, 255, 255), 3)
    return image


def test_selection_identity_keeps_only_hashes_and_matches_same_cell_context() -> None:
    first = extract_selection_identity(grid(20), (5, 8, 152, 190))
    second_image = grid(20)
    # Portrait/outline region is intentionally outside the hashed card center.
    second_image[10:52, 8:50] = (0, 220, 255)
    second = extract_selection_identity(second_image, (6, 9, 152, 190))

    assert first is not None and second is not None
    assert first.selected_cell == second.selected_cell == 1
    assert len(first.card_art_phash) == 16
    assert len(first.context_phashes) == 15
    assert visual_identity_matches(first, second) is True
    assert not hasattr(first, "image")
    assert not hasattr(first, "pixels")


def test_selection_identity_requires_same_cell_card_and_full_context() -> None:
    first = extract_selection_identity(grid(20), (5, 8, 152, 190))
    other_cell = extract_selection_identity(grid(20), (184, 8, 152, 190))
    changed_context = grid(20)
    # Change a non-selected card's geometry, while retaining the selected card.
    changed_context[46:140, 229:320] = np.random.default_rng(42).integers(
        0,
        256,
        size=(94, 91, 3),
        dtype=np.uint8,
    )
    other_context = extract_selection_identity(changed_context, (5, 8, 152, 190))

    assert first is not None and other_cell is not None and other_context is not None
    assert visual_identity_matches(first, other_cell) is False
    assert visual_identity_matches(first, other_context) is False


def test_selection_identity_fails_closed_for_missing_or_off_grid_selection() -> None:
    image = grid(20)

    assert extract_selection_identity(image, None) is None
    assert extract_selection_identity(image, (90, 90, 152, 190)) is None
    assert extract_selection_identity(np.zeros((10, 10), dtype=np.uint8), (5, 8, 152, 190)) is None


def test_selection_identity_supports_every_visible_fifth_row_cell() -> None:
    image = grid(20)

    for cell, x in enumerate((5, 184, 363), start=13):
        evidence = extract_selection_identity(image, (x, 888, 152, 190))
        assert evidence is not None
        assert evidence.selected_cell == cell


def test_empty_last_row_animation_preserves_identity_but_new_card_invalidates_it() -> None:
    first_image = grid(20)
    second_image = grid(20)
    for image, offset in ((first_image, 0), (second_image, 35)):
        y, x = np.indices(image[880:].shape[:2])
        background = (90 + offset + x // 12 + y // 18).astype(np.uint8)
        image[880:] = background[:, :, None]
        cv2.circle(image, (100 + offset, 950), 2, (230, 230, 230), -1)
    first = extract_selection_identity(first_image, (5, 8, 152, 190))
    second = extract_selection_identity(second_image, (5, 8, 152, 190))
    assert first is not None and second is not None
    assert visual_identity_matches(first, second)
    tracker = EchoStateTracker()
    tracker.process(
        EchoObservation(
            PageType.ECHO_SELECTION, name="測試聲骸", level=20, selection_identity=first
        )
    )
    assert (
        tracker.process(
            EchoObservation(
                PageType.ECHO_SELECTION, name="測試聲骸", level=20, selection_identity=second
            )
        )
        == []
    )
    assert len(tracker.sessions) == 1
    # A populated cell cannot be ignored merely because it was empty before.
    second_image[880:] = grid(20)[880:]
    populated = extract_selection_identity(second_image, (5, 8, 152, 190))
    assert not visual_identity_matches(first, populated)
    assert extract_selection_identity(first_image, (5, 888, 152, 190)) is None


def test_smooth_artwork_with_visible_card_frame_is_not_treated_as_empty() -> None:
    image = grid(20)
    image[880:] = 100
    empty = extract_selection_identity(image, (5, 8, 152, 190))
    cv2.rectangle(image, (7, 890), (155, 1075), (220, 220, 220), 2)
    framed = extract_selection_identity(image, (5, 8, 152, 190))
    assert empty is not None and framed is not None
    assert framed.context_phashes[12] != empty.context_phashes[12]
    assert not visual_identity_matches(empty, framed)
