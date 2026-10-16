"""Fail-closed, pixel-free identity evidence for the echo selection grid.

The game does not expose a trustworthy echo identifier.  This module therefore
produces only short-lived visual evidence that is useful inside one capture run:
the uniquely selected grid cell, a perceptual hash of the card artwork, and a
per-cell context vector.  No crop or pixel array leaves this function.
"""

from __future__ import annotations

from typing import Final

import cv2
import numpy as np
from numpy.typing import NDArray

from wuwa_assistant.models.domain import SelectionIdentityEvidence

from .stability import hamming_distance, perceptual_hash

_EVIDENCE_VERSION: Final = "selection-grid-v1"
_GRID_COLUMNS: Final = 3
_GRID_ROWS: Final = 5
_CELL_X: Final = 5
_CELL_Y: Final = 8
_CELL_X_STEP: Final = 179
_CELL_Y_STEP: Final = 220
_CELL_WIDTH: Final = 152
_CELL_HEIGHT: Final = 190

# The card center excludes the selected outline, character portrait badge, and
# footer text.  Those UI decorations may change while the physical echo does not.
_ART_LEFT: Final = 45
_ART_TOP: Final = 38
_ART_RIGHT: Final = 142
_ART_BOTTOM: Final = 138

_BBOX_POSITION_TOLERANCE: Final = 14
_CARD_HASH_DISTANCE: Final = 4
_CONTEXT_HASH_DISTANCE: Final = 4
_CONTEXT_TOTAL_DISTANCE: Final = 24
# pHash always clears its top/DC bit, so this marker cannot collide with artwork.
_EMPTY_CELL_HASH: Final = "ffffffffffffffff"


def extract_selection_identity(
    grid: NDArray[np.uint8],
    selected_card_bbox: tuple[int, int, int, int] | None,
) -> SelectionIdentityEvidence | None:
    """Return bounded hash evidence when the selected grid cell is unambiguous."""

    if (
        selected_card_bbox is None
        or not isinstance(grid, np.ndarray)
        or grid.ndim != 3
        or grid.shape[2] != 3
    ):
        return None

    x, y, width, height = selected_card_bbox
    cell = _nearest_cell(x, y)
    if cell is None or not (130 <= width <= 175 and 170 <= height <= 220):
        return None

    context_hashes: list[str] = []
    for row in range(_GRID_ROWS):
        for column in range(_GRID_COLUMNS):
            artwork = _art_crop(grid, row, column)
            if artwork is None:
                return None
            # Unoccupied cells show the animated scene behind the inventory.
            # Hashing that background invents a different echo on every scan.
            # Keep occupancy in the vector instead; populated/empty transitions
            # must still invalidate identity, including after sorting/scrolling.
            gray = cv2.cvtColor(artwork, cv2.COLOR_BGR2GRAY)
            edges = cv2.Canny(gray, 50, 100)
            x, y = _CELL_X + column * _CELL_X_STEP, _CELL_Y + row * _CELL_Y_STEP
            card = grid[y : y + _CELL_HEIGHT, x : x + _CELL_WIDTH]
            if card.shape != (_CELL_HEIGHT, _CELL_WIDTH, 3):
                return None
            card_edges = cv2.Canny(cv2.cvtColor(card, cv2.COLOR_BGR2GRAY), 50, 100)
            context_hashes.append(
                _EMPTY_CELL_HASH
                if np.count_nonzero(edges) / edges.size < 0.01
                and np.count_nonzero(card_edges) / card_edges.size < 0.01
                else _hash_hex(artwork)
            )

    selected_hash = context_hashes[cell]
    if selected_hash == _EMPTY_CELL_HASH:
        return None  # A selected cell classified as empty cannot establish identity.
    return SelectionIdentityEvidence(
        version=_EVIDENCE_VERSION,
        selected_cell=cell + 1,
        selected_card_bbox=selected_card_bbox,
        card_art_phash=selected_hash,
        context_phashes=tuple(context_hashes),
    )


def visual_identity_matches(
    left: SelectionIdentityEvidence | None,
    right: SelectionIdentityEvidence | None,
) -> bool:
    """Require the same cell, artwork, and complete visible-grid context."""

    if (
        left is None
        or right is None
        or left.version != _EVIDENCE_VERSION
        or right.version != _EVIDENCE_VERSION
        or left.selected_cell != right.selected_cell
        or len(left.context_phashes) != _GRID_COLUMNS * _GRID_ROWS
        or len(right.context_phashes) != _GRID_COLUMNS * _GRID_ROWS
    ):
        return False
    try:
        if left.card_art_phash == _EMPTY_CELL_HASH or right.card_art_phash == _EMPTY_CELL_HASH:
            return False
        if any(
            (first == _EMPTY_CELL_HASH) != (second == _EMPTY_CELL_HASH)
            for first, second in zip(left.context_phashes, right.context_phashes, strict=True)
        ):
            return False
        if _hash_distance(left.card_art_phash, right.card_art_phash) > _CARD_HASH_DISTANCE:
            return False
        distances = tuple(
            _hash_distance(first, second)
            for first, second in zip(
                left.context_phashes,
                right.context_phashes,
                strict=True,
            )
        )
    except ValueError:
        return False
    return max(distances) <= _CONTEXT_HASH_DISTANCE and sum(distances) <= _CONTEXT_TOTAL_DISTANCE


def _nearest_cell(x: int, y: int) -> int | None:
    candidates: list[tuple[int, int]] = []
    for row in range(_GRID_ROWS):
        for column in range(_GRID_COLUMNS):
            expected_x = _CELL_X + _CELL_X_STEP * column
            expected_y = _CELL_Y + _CELL_Y_STEP * row
            distance = abs(x - expected_x) + abs(y - expected_y)
            if (
                abs(x - expected_x) <= _BBOX_POSITION_TOLERANCE
                and abs(y - expected_y) <= _BBOX_POSITION_TOLERANCE
            ):
                candidates.append((distance, row * _GRID_COLUMNS + column))
    if len(candidates) != 1:
        return None
    return candidates[0][1]


def _art_crop(
    grid: NDArray[np.uint8],
    row: int,
    column: int,
) -> NDArray[np.uint8] | None:
    x = _CELL_X + _CELL_X_STEP * column
    y = _CELL_Y + _CELL_Y_STEP * row
    crop = grid[
        y + _ART_TOP : y + _ART_BOTTOM,
        x + _ART_LEFT : x + _ART_RIGHT,
    ]
    expected_shape = (_ART_BOTTOM - _ART_TOP, _ART_RIGHT - _ART_LEFT, 3)
    return crop if crop.shape == expected_shape else None


def _hash_hex(image: NDArray[np.uint8]) -> str:
    return f"{perceptual_hash(image):016x}"


def _hash_distance(left: str, right: str) -> int:
    if len(left) != 16 or len(right) != 16:
        raise ValueError("selection identity hashes must contain 64 bits")
    try:
        return hamming_distance(int(left, 16), int(right, 16))
    except ValueError as exc:
        raise ValueError("selection identity hashes must be hexadecimal") from exc


__all__ = ["extract_selection_identity", "visual_identity_matches"]
