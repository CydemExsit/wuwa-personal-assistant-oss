"""Fixed regions of interest for the 2560 x 1440 Traditional Chinese UI.

Coordinates use the OpenCV convention ``image[y1:y2, x1:x2]``.  The lower-right
corner is exclusive.  Phase one deliberately rejects other frame sizes instead of
silently stretching the regions and reading unrelated pixels.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

import numpy as np
from numpy.typing import NDArray

REFERENCE_WIDTH: Final = 2560
REFERENCE_HEIGHT: Final = 1440
REFERENCE_SIZE: Final = (REFERENCE_WIDTH, REFERENCE_HEIGHT)


@dataclass(frozen=True, slots=True)
class Roi:
    """An immutable named rectangle in reference-frame coordinates."""

    name: str
    x1: int
    y1: int
    x2: int
    y2: int

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ROI name may not be empty")
        if min(self.x1, self.y1) < 0:
            raise ValueError(f"ROI {self.name!r} starts outside the reference frame")
        if self.x2 <= self.x1 or self.y2 <= self.y1:
            raise ValueError(f"ROI {self.name!r} must have positive width and height")
        if self.x2 > REFERENCE_WIDTH or self.y2 > REFERENCE_HEIGHT:
            raise ValueError(f"ROI {self.name!r} exceeds the reference frame")

    @property
    def width(self) -> int:
        return self.x2 - self.x1

    @property
    def height(self) -> int:
        return self.y2 - self.y1

    @property
    def xyxy(self) -> tuple[int, int, int, int]:
        return (self.x1, self.y1, self.x2, self.y2)

    @property
    def slices(self) -> tuple[slice, slice]:
        """Return ``(row_slice, column_slice)`` for NumPy/OpenCV indexing."""

        return (slice(self.y1, self.y2), slice(self.x1, self.x2))

    def crop(self, image: NDArray[Any], *, copy: bool = False) -> NDArray[Any]:
        """Crop this rectangle from an image, returning a view unless ``copy`` is set."""

        if not isinstance(image, np.ndarray):
            raise TypeError("image must be a NumPy array")
        if image.ndim < 2:
            raise ValueError("image must have at least two dimensions")
        height, width = image.shape[:2]
        if self.x2 > width or self.y2 > height:
            raise ValueError(f"ROI {self.name!r} exceeds the supplied {width}x{height} image")
        cropped = image[self.slices]
        return cropped.copy() if copy else cropped


PAGE_HEADER_ROI: Final = Roi("page_header", 45, 35, 330, 135)
# Stable semantic aliases used by page classifiers and parsers.
PAGE_HEADER: Final = PAGE_HEADER_ROI

# Echo enhancement page.  The five substat rows have a measured 57 px cadence.
ENHANCEMENT_NAME_ROI: Final = Roi("enhancement_name", 268, 188, 630, 245)
ENHANCEMENT_LEVEL_ROI: Final = Roi("enhancement_level", 268, 258, 410, 306)
ENHANCEMENT_EXPERIENCE_ROI: Final = Roi("enhancement_experience", 680, 258, 895, 306)
ENHANCEMENT_EXPERIENCE_BAR_ROI: Final = Roi("enhancement_experience_bar", 268, 300, 895, 326)
ENHANCEMENT_MAIN_STAT_ROIS: Final = (
    Roi("enhancement_main_stat_1", 260, 336, 900, 393),
    Roi("enhancement_main_stat_2", 260, 393, 900, 450),
)
ENHANCEMENT_SUBSTAT_ROIS: Final = tuple(
    Roi(f"enhancement_substat_{index + 1}", 285, 450 + 57 * index, 895, 507 + 57 * index)
    for index in range(5)
)
ENHANCEMENT_INFO_PANEL_ROI: Final = Roi("enhancement_info_panel", 255, 180, 910, 740)
ENHANCEMENT_STATE: Final = Roi("enhancement_state", 255, 250, 910, 740)
ENHANCEMENT_PANEL: Final = ENHANCEMENT_INFO_PANEL_ROI

ENHANCEMENT_ROIS: Final[Mapping[str, Roi]] = MappingProxyType(
    {
        roi.name: roi
        for roi in (
            PAGE_HEADER_ROI,
            ENHANCEMENT_NAME_ROI,
            ENHANCEMENT_LEVEL_ROI,
            ENHANCEMENT_EXPERIENCE_ROI,
            ENHANCEMENT_EXPERIENCE_BAR_ROI,
            *ENHANCEMENT_MAIN_STAT_ROIS,
            *ENHANCEMENT_SUBSTAT_ROIS,
            ENHANCEMENT_INFO_PANEL_ROI,
        )
    }
)

# Echo-selection page.
SELECTION_COST_ANCHOR_ROI: Final = Roi("selection_cost_anchor", 55, 20, 225, 230)
SELECTION_FILTER_ROI: Final = Roi("selection_filter", 280, 45, 805, 215)
SELECTION_GRID_ROI: Final = Roi("selection_grid", 275, 210, 810, 1300)
# The fixed 3 x 5 visible card positions have a 179 px horizontal and 220 px
# vertical cadence.  These small corner crops are parser evidence for the
# selected outline and optional character portrait badge.  They are deliberately
# *not* all required by the live stability gate: several cards can animate at
# once, and requiring all 15 pHashes to match can prevent OCR indefinitely.
SELECTION_CARD_STATUS_ROIS: Final = tuple(
    Roi(
        f"selection_card_status_{row + 1}_{column + 1}",
        280 + 179 * column,
        214 + 220 * row,
        338 + 179 * column,
        272 + 220 * row,
    )
    for row in range(5)
    for column in range(3)
)
SELECTION_NAME_ROI: Final = Roi("selection_name", 1995, 185, 2240, 245)
SELECTION_LEVEL_ROI: Final = Roi("selection_level", 2335, 185, 2465, 245)
SELECTION_COST_ROI: Final = Roi("selection_cost", 1995, 240, 2210, 300)
SELECTION_MAIN_STAT_ROIS: Final = (
    Roi("selection_main_stat_1", 1995, 305, 2485, 360),
    Roi("selection_main_stat_2", 1995, 355, 2485, 412),
)
SELECTION_SUBSTAT_PANEL_ROI: Final = Roi("selection_substat_panel", 2000, 405, 2485, 650)
SELECTION_DISCARD_TOGGLE_ROI: Final = Roi("selection_discard_toggle", 2305, 235, 2375, 302)
SELECTION_DISCARD_TOAST_ROI: Final = Roi("selection_discard_toast", 950, 245, 1610, 335)
SELECTION_DISCARD_TOAST_TEXT_ROI: Final = Roi("selection_discard_toast_text", 1200, 260, 1370, 315)
SELECTION_CARD_DISCARD_BADGE_ROI: Final = Roi("selection_card_discard_badge", 758, 280, 798, 330)
# Direct equipped-state text shown only on the selection page. The owner crop
# carries the explicit character name and the ``...裝備中``/``...裝配中`` suffix.
# The primary action is ``卸下`` when equipped and ``裝配`` otherwise.
SELECTION_EQUIPPED_OWNER_ROI: Final = Roi("selection_equipped_owner", 1980, 1200, 2480, 1295)
SELECTION_PRIMARY_ACTION_ROI: Final = Roi("selection_primary_action", 1980, 1280, 2240, 1390)
# The five permanent equipment positions are separate from the 3x5 inventory grid.
# These crops are parser evidence, not broad image hashes in the stability gate.
SELECTION_EQUIPPED_SLOTS_ROI: Final = Roi("selection_equipped_slots", 35, 230, 235, 1070)
# Skill length moves the set-effect heading vertically, including above y=650
# for a level-zero echo and near y=900 for a fully upgraded one.
SELECTION_SET_EFFECTS_ROI: Final = Roi("selection_set_effects", 1900, 420, 2520, 1250)
SELECTION_DETAIL: Final = Roi("selection_detail", 1900, 150, 2520, 700)
DISCARD_BANNER: Final = SELECTION_DISCARD_TOAST_ROI

SELECTION_ROIS: Final[Mapping[str, Roi]] = MappingProxyType(
    {
        roi.name: roi
        for roi in (
            PAGE_HEADER_ROI,
            SELECTION_COST_ANCHOR_ROI,
            SELECTION_FILTER_ROI,
            SELECTION_GRID_ROI,
            *SELECTION_CARD_STATUS_ROIS,
            SELECTION_NAME_ROI,
            SELECTION_LEVEL_ROI,
            SELECTION_COST_ROI,
            *SELECTION_MAIN_STAT_ROIS,
            SELECTION_SUBSTAT_PANEL_ROI,
            SELECTION_DISCARD_TOGGLE_ROI,
            SELECTION_DISCARD_TOAST_ROI,
            SELECTION_DISCARD_TOAST_TEXT_ROI,
            SELECTION_CARD_DISCARD_BADGE_ROI,
            SELECTION_EQUIPPED_OWNER_ROI,
            SELECTION_PRIMARY_ACTION_ROI,
            SELECTION_EQUIPPED_SLOTS_ROI,
            SELECTION_SET_EFFECTS_ROI,
        )
    }
)

# Enhancement-material selection page.
MATERIAL_GRID_ROI: Final = Roi("material_grid", 1340, 55, 2490, 1145)
MATERIAL_SCROLLBAR_ROI: Final = Roi("material_scrollbar", 2485, 325, 2520, 540)
MATERIAL_LEVEL_PREVIEW_ROI: Final = Roi("material_level_preview", 260, 250, 930, 326)
MATERIAL_MAIN_STAT_PREVIEW_ROI: Final = Roi("material_main_stat_preview", 260, 330, 930, 455)
MATERIAL_COMPARISON_ARROWS_ROI: Final = Roi("material_comparison_arrows", 690, 330, 760, 455)
MATERIAL_SELECTED_CARD_ROI: Final = Roi("material_selected_card", 1350, 600, 1590, 890)
MATERIAL_CARD_DISCARD_BADGE_ROI: Final = Roi("material_card_discard_badge", 1510, 705, 1565, 770)
MATERIAL_SOURCE_SLOT_ROI: Final = Roi("material_source_slot", 420, 975, 570, 1160)
MATERIAL_CTA_ROI: Final = Roi("material_cta", 270, 1260, 900, 1370)
MATERIAL_PANEL: Final = Roi("material_panel", 1340, 55, 2520, 1270)
MATERIAL_SLOTS: Final = Roi("material_slots", 270, 970, 900, 1170)

MATERIAL_SELECTION_ROIS: Final[Mapping[str, Roi]] = MappingProxyType(
    {
        roi.name: roi
        for roi in (
            PAGE_HEADER_ROI,
            MATERIAL_GRID_ROI,
            MATERIAL_SCROLLBAR_ROI,
            MATERIAL_LEVEL_PREVIEW_ROI,
            MATERIAL_MAIN_STAT_PREVIEW_ROI,
            MATERIAL_COMPARISON_ARROWS_ROI,
            MATERIAL_SELECTED_CARD_ROI,
            MATERIAL_CARD_DISCARD_BADGE_ROI,
            MATERIAL_SOURCE_SLOT_ROI,
            MATERIAL_CTA_ROI,
        )
    }
)

# Overview anchors are intentionally small and avoid the animated character model.
OVERVIEW_COST_ANCHOR_ROI: Final = Roi("overview_cost_anchor", 1740, 110, 1930, 240)
OVERVIEW_EQUIPPED_ECHOES_ROI: Final = Roi("overview_equipped_echoes", 1810, 220, 2145, 1310)
OVERVIEW_ROIS: Final[Mapping[str, Roi]] = MappingProxyType(
    {
        roi.name: roi
        for roi in (PAGE_HEADER_ROI, OVERVIEW_COST_ANCHOR_ROI, OVERVIEW_EQUIPPED_ECHOES_ROI)
    }
)

# Caller selects a group after page classification.  String keys match PageType values
# without importing the models package and creating a vision/models dependency cycle.
STABILITY_ROIS_BY_PAGE: Final[Mapping[str, tuple[Roi, ...]]] = MappingProxyType(
    {
        "character_echo_overview": (
            PAGE_HEADER_ROI,
            OVERVIEW_COST_ANCHOR_ROI,
            OVERVIEW_EQUIPPED_ECHOES_ROI,
        ),
        "echo_selection": (
            SELECTION_EQUIPPED_OWNER_ROI,
            SELECTION_PRIMARY_ACTION_ROI,
            SELECTION_NAME_ROI,
            SELECTION_LEVEL_ROI,
            SELECTION_COST_ROI,
            *SELECTION_MAIN_STAT_ROIS,
            SELECTION_SUBSTAT_PANEL_ROI,
        ),
        "echo_enhancement": (
            PAGE_HEADER_ROI,
            ENHANCEMENT_LEVEL_ROI,
            *ENHANCEMENT_MAIN_STAT_ROIS,
            *ENHANCEMENT_SUBSTAT_ROIS,
        ),
        "enhancement_material_selection": (
            PAGE_HEADER_ROI,
            MATERIAL_LEVEL_PREVIEW_ROI,
            MATERIAL_MAIN_STAT_PREVIEW_ROI,
            MATERIAL_SOURCE_SLOT_ROI,
            MATERIAL_CARD_DISCARD_BADGE_ROI,
        ),
    }
)
