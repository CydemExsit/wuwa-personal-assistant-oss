"""Allowlisted ROI packets that never retain a full monitor frame."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Final

import numpy as np

from wuwa_assistant.parsers.echo import (
    ENHANCEMENT_PARSE_PANEL,
    MATERIAL_PARSE_PANEL,
    SELECTION_PARSE_PANEL,
)
from wuwa_assistant.vision.classifier import ENHANCEMENT_STATE_ROI
from wuwa_assistant.vision.rois import (
    ENHANCEMENT_EXPERIENCE_BAR_ROI,
    ENHANCEMENT_EXPERIENCE_ROI,
    ENHANCEMENT_LEVEL_ROI,
    ENHANCEMENT_MAIN_STAT_ROIS,
    ENHANCEMENT_NAME_ROI,
    ENHANCEMENT_SUBSTAT_ROIS,
    MATERIAL_CARD_DISCARD_BADGE_ROI,
    MATERIAL_CTA_ROI,
    MATERIAL_LEVEL_PREVIEW_ROI,
    MATERIAL_MAIN_STAT_PREVIEW_ROI,
    MATERIAL_SELECTED_CARD_ROI,
    MATERIAL_SOURCE_SLOT_ROI,
    OVERVIEW_COST_ANCHOR_ROI,
    OVERVIEW_EQUIPPED_ECHOES_ROI,
    PAGE_HEADER_ROI,
    SELECTION_CARD_DISCARD_BADGE_ROI,
    SELECTION_CARD_STATUS_ROIS,
    SELECTION_COST_ROI,
    SELECTION_DISCARD_TOAST_TEXT_ROI,
    SELECTION_DISCARD_TOGGLE_ROI,
    SELECTION_EQUIPPED_OWNER_ROI,
    SELECTION_EQUIPPED_SLOTS_ROI,
    SELECTION_GRID_ROI,
    SELECTION_LEVEL_ROI,
    SELECTION_MAIN_STAT_ROIS,
    SELECTION_NAME_ROI,
    SELECTION_PRIMARY_ACTION_ROI,
    SELECTION_SET_EFFECTS_ROI,
    SELECTION_SUBSTAT_PANEL_ROI,
    Roi,
)

from .backend import BgrImage, CapturedFrame

# Public aliases make their role at the capture boundary explicit while preserving
# the exact ROI names consumed by the existing parser APIs.
ENHANCEMENT_PARSE_PANEL_ROI: Final = ENHANCEMENT_PARSE_PANEL
SELECTION_PARSE_PANEL_ROI: Final = SELECTION_PARSE_PANEL
MATERIAL_PARSE_PANEL_ROI: Final = MATERIAL_PARSE_PANEL

PROBE_ROIS: Final[tuple[Roi, ...]] = (
    PAGE_HEADER_ROI,
    ENHANCEMENT_STATE_ROI,
    MATERIAL_CTA_ROI,
)

CONTENT_ROIS_BY_PAGE: Final[Mapping[str, tuple[Roi, ...]]] = MappingProxyType(
    {
        "character_echo_overview": (
            *PROBE_ROIS,
            OVERVIEW_COST_ANCHOR_ROI,
            OVERVIEW_EQUIPPED_ECHOES_ROI,
        ),
        "echo_selection": (
            *PROBE_ROIS,
            SELECTION_PARSE_PANEL_ROI,
            SELECTION_GRID_ROI,
            *SELECTION_CARD_STATUS_ROIS,
            SELECTION_NAME_ROI,
            SELECTION_LEVEL_ROI,
            SELECTION_COST_ROI,
            *SELECTION_MAIN_STAT_ROIS,
            SELECTION_SUBSTAT_PANEL_ROI,
            SELECTION_DISCARD_TOGGLE_ROI,
            SELECTION_DISCARD_TOAST_TEXT_ROI,
            SELECTION_CARD_DISCARD_BADGE_ROI,
            SELECTION_EQUIPPED_OWNER_ROI,
            SELECTION_PRIMARY_ACTION_ROI,
            SELECTION_EQUIPPED_SLOTS_ROI,
            SELECTION_SET_EFFECTS_ROI,
        ),
        "echo_enhancement": (
            *PROBE_ROIS,
            ENHANCEMENT_PARSE_PANEL_ROI,
            ENHANCEMENT_NAME_ROI,
            ENHANCEMENT_LEVEL_ROI,
            ENHANCEMENT_EXPERIENCE_ROI,
            ENHANCEMENT_EXPERIENCE_BAR_ROI,
            *ENHANCEMENT_MAIN_STAT_ROIS,
            *ENHANCEMENT_SUBSTAT_ROIS,
        ),
        "enhancement_material_selection": (
            *PROBE_ROIS,
            MATERIAL_PARSE_PANEL_ROI,
            MATERIAL_LEVEL_PREVIEW_ROI,
            MATERIAL_MAIN_STAT_PREVIEW_ROI,
            MATERIAL_SOURCE_SLOT_ROI,
            MATERIAL_SELECTED_CARD_ROI,
            MATERIAL_CARD_DISCARD_BADGE_ROI,
        ),
    }
)

_ALL_ROIS: Final[tuple[Roi, ...]] = (
    *PROBE_ROIS,
    *(roi for rois in CONTENT_ROIS_BY_PAGE.values() for roi in rois),
)
ALLOWED_ROIS: Final[Mapping[str, Roi]] = MappingProxyType({roi.name: roi for roi in _ALL_ROIS})


class RoiPacketKind(StrEnum):
    PROBE = "probe"
    CONTENT = "content"


@dataclass(frozen=True, slots=True)
class RoiPacket:
    """Metadata plus independent, read-only BGR crops from one captured sample."""

    kind: RoiPacketKind
    captured_at: str
    monotonic_timestamp_ns: int
    monitor_index: int
    sequence: int
    images: Mapping[str, BgrImage] = field(repr=False)
    page_type: str | None = None
    source_file: str | None = None

    def __post_init__(self) -> None:
        if not self.images:
            raise ValueError("an ROI packet must contain at least one image")
        if self.kind is RoiPacketKind.PROBE and self.page_type is not None:
            raise ValueError("a probe packet may not claim a classified page type")
        if self.kind is RoiPacketKind.CONTENT and self.page_type not in CONTENT_ROIS_BY_PAGE:
            raise ValueError("a content packet needs a supported page type")

        allowed_for_kind = (
            {roi.name for roi in PROBE_ROIS}
            if self.kind is RoiPacketKind.PROBE
            else {roi.name for roi in CONTENT_ROIS_BY_PAGE[self.page_type or ""]}
        )
        supplied_names = set(self.images)
        if supplied_names != allowed_for_kind:
            missing = sorted(allowed_for_kind - supplied_names)
            unexpected = sorted(supplied_names - allowed_for_kind)
            raise ValueError(
                f"ROI packet does not match its allowlist; missing={missing}, "
                f"unexpected={unexpected}"
            )

        owned: dict[str, BgrImage] = {}
        for name, image in self.images.items():
            roi = ALLOWED_ROIS[name]
            if not isinstance(image, np.ndarray):
                raise TypeError(f"ROI {name!r} must be a NumPy array")
            expected_shape = (roi.height, roi.width, 3)
            if image.dtype != np.uint8 or image.shape != expected_shape:
                raise ValueError(
                    f"ROI {name!r} must be uint8 BGR {expected_shape!r}; "
                    f"got dtype={image.dtype}, shape={image.shape!r}"
                )
            copied = np.ascontiguousarray(image).copy()
            copied.setflags(write=False)
            owned[name] = copied
        object.__setattr__(self, "images", MappingProxyType(owned))

    def image(self, name: str) -> BgrImage:
        return self.images[name]

    @property
    def shapes(self) -> Mapping[str, tuple[int, ...]]:
        return MappingProxyType({name: image.shape for name, image in self.images.items()})

    @property
    def total_bytes(self) -> int:
        return sum(image.nbytes for image in self.images.values())


@dataclass(frozen=True, slots=True)
class ExtractedRois:
    probe: RoiPacket
    content: RoiPacket | None


class RoiExtractor:
    """Copy only allowlisted ROIs and release the source frame before returning."""

    def extract(
        self,
        frame: CapturedFrame,
        *,
        page_type: str | None = None,
    ) -> ExtractedRois:
        normalized_page = str(page_type) if page_type is not None else None
        try:
            probe = self._packet(frame, RoiPacketKind.PROBE, PROBE_ROIS, page_type=None)
            content_rois = CONTENT_ROIS_BY_PAGE.get(normalized_page or "")
            content = (
                self._packet(
                    frame,
                    RoiPacketKind.CONTENT,
                    content_rois,
                    page_type=normalized_page,
                )
                if content_rois is not None
                else None
            )
            return ExtractedRois(probe=probe, content=content)
        finally:
            # No pHash, OCR, callback, Qt signal, or queue can observe the full frame.
            frame.release()

    @staticmethod
    def _packet(
        frame: CapturedFrame,
        kind: RoiPacketKind,
        rois: Sequence[Roi],
        *,
        page_type: str | None,
    ) -> RoiPacket:
        # Views exist only while constructing the packet. RoiPacket copies each view
        # into owned memory before ``extract`` releases the full-frame lease.
        images = {
            roi.name: roi.crop(frame.pixels, copy=False)
            if frame.pixels.shape[:2] == (1440, 2560)
            else frame.copy_roi(roi)
            for roi in rois
        }
        return RoiPacket(
            kind=kind,
            captured_at=frame.captured_at,
            monotonic_timestamp_ns=frame.monotonic_timestamp_ns,
            monitor_index=frame.monitor_index,
            sequence=frame.sequence,
            images=images,
            page_type=page_type,
            source_file=frame.source_file,
        )


__all__ = [
    "ALLOWED_ROIS",
    "CONTENT_ROIS_BY_PAGE",
    "ENHANCEMENT_PARSE_PANEL_ROI",
    "MATERIAL_PARSE_PANEL_ROI",
    "PROBE_ROIS",
    "SELECTION_PARSE_PANEL_ROI",
    "ExtractedRois",
    "RoiExtractor",
    "RoiPacket",
    "RoiPacketKind",
]
