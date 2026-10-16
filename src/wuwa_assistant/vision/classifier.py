"""Small-anchor page classifier for the fixed Traditional Chinese layout."""

from __future__ import annotations

from collections.abc import Mapping
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from wuwa_assistant.models import OcrToken, PageClassification, PageType
from wuwa_assistant.ocr.base import OcrEngine

from .debug import DebugWriter
from .preprocess import crop_roi, validate_image, validate_reference_frame
from .rois import MATERIAL_CTA_ROI, PAGE_HEADER_ROI, Roi

ENHANCEMENT_STATE_ROI = Roi("enhancement_state", 240, 240, 920, 330)


def _anchor_match(tokens: list[OcrToken], expected: str) -> tuple[float, str | None]:
    best_score = 0.0
    best_text: str | None = None
    for token in tokens:
        normalized = "".join(token.text.split())
        if expected in normalized:
            similarity = 1.0
        else:
            similarity = SequenceMatcher(a=normalized, b=expected).ratio()
        score = min(token.confidence, similarity)
        if score > best_score:
            best_score = score
            best_text = token.text
    return best_score, best_text


class PageClassifier:
    def __init__(self, ocr: OcrEngine, debug_writer: DebugWriter | None = None) -> None:
        self.ocr = ocr
        self.debug_writer = debug_writer or DebugWriter(None)

    def classify(
        self,
        frame: NDArray[np.uint8],
        *,
        source_file: str | Path = "unknown",
    ) -> PageClassification:
        try:
            validate_reference_frame(frame)
        except (TypeError, ValueError) as exc:
            return PageClassification(
                page_type=PageType.UNKNOWN,
                confidence=0.0,
                evidence=[str(exc)],
            )

        rois = {
            PAGE_HEADER_ROI.name: crop_roi(frame, PAGE_HEADER_ROI),
            ENHANCEMENT_STATE_ROI.name: crop_roi(frame, ENHANCEMENT_STATE_ROI),
            MATERIAL_CTA_ROI.name: crop_roi(frame, MATERIAL_CTA_ROI),
        }
        return self.classify_rois(rois, source_file=source_file)

    def classify_rois(
        self,
        rois: Mapping[str, NDArray[np.uint8]],
        *,
        source_file: str | Path = "live-capture",
    ) -> PageClassification:
        """Classify owned, bounded ROI copies without retaining a monitor frame."""

        try:
            header = _required_roi(rois, PAGE_HEADER_ROI)
        except (KeyError, TypeError, ValueError) as exc:
            return PageClassification(
                page_type=PageType.UNKNOWN,
                confidence=0.0,
                evidence=[f"invalid page probe: {exc}"],
            )
        header_tokens = self.ocr.recognize(header, detect=True)
        debug_paths = self.debug_writer.save_roi(source_file, "page_header", header)
        raw_ocr: dict[str, list[OcrToken]] = {"page_header": header_tokens}

        enhancement_score, enhancement_text = _anchor_match(header_tokens, "聲骸強化")
        if enhancement_score >= 0.72:
            try:
                state = _required_roi(rois, ENHANCEMENT_STATE_ROI)
                cta = _required_roi(rois, MATERIAL_CTA_ROI)
            except (KeyError, TypeError, ValueError) as exc:
                return PageClassification(
                    page_type=PageType.UNKNOWN,
                    confidence=0.0,
                    raw_ocr=raw_ocr,
                    evidence=[f"incomplete enhancement probe: {exc}"],
                    debug_roi_paths=debug_paths,
                )
            state_tokens = self.ocr.recognize(state, detect=True)
            cta_tokens = self.ocr.recognize(cta, detect=True)
            raw_ocr["enhancement_state"] = state_tokens
            raw_ocr["enhancement_cta"] = cta_tokens
            debug_paths.extend(self.debug_writer.save_roi(source_file, "enhancement_state", state))
            debug_paths.extend(self.debug_writer.save_roi(source_file, "enhancement_cta", cta))

            state_text = " ".join(token.text for token in state_tokens)
            cta_score, cta_text = _anchor_match(cta_tokens, "強化並調諧")
            has_projected_level = (
                ">" in state_text or "→" in state_text or "強化並調諧" in (cta_text or "")
            )
            if has_projected_level:
                confidence = min(
                    enhancement_score,
                    max(cta_score, max((token.confidence for token in state_tokens), default=0.0)),
                )
                return PageClassification(
                    page_type=PageType.ENHANCEMENT_MATERIAL_SELECTION,
                    confidence=confidence,
                    raw_ocr=raw_ocr,
                    evidence=[
                        f"header={enhancement_text!r}",
                        f"projected_level={state_text!r}",
                        f"cta={cta_text!r}",
                    ],
                    debug_roi_paths=debug_paths,
                )
            return PageClassification(
                page_type=PageType.ECHO_ENHANCEMENT,
                confidence=enhancement_score,
                raw_ocr=raw_ocr,
                evidence=[f"header={enhancement_text!r}", "no material-preview evidence"],
                debug_roi_paths=debug_paths,
            )

        selection_score, selection_text = _anchor_match(header_tokens, "COST")
        if selection_score >= 0.80:
            return PageClassification(
                page_type=PageType.ECHO_SELECTION,
                confidence=selection_score,
                raw_ocr=raw_ocr,
                evidence=[f"header={selection_text!r}"],
                debug_roi_paths=debug_paths,
            )

        overview_score, overview_text = _anchor_match(header_tokens, "聲骸")
        if overview_score >= 0.72:
            return PageClassification(
                page_type=PageType.CHARACTER_ECHO_OVERVIEW,
                confidence=overview_score,
                raw_ocr=raw_ocr,
                evidence=[f"header={overview_text!r}"],
                debug_roi_paths=debug_paths,
            )

        return PageClassification(
            page_type=PageType.UNKNOWN,
            confidence=max(enhancement_score, selection_score, overview_score),
            raw_ocr=raw_ocr,
            evidence=["no page anchor exceeded its acceptance threshold"],
            debug_roi_paths=debug_paths,
        )


def _required_roi(
    images: Mapping[str, NDArray[np.uint8]],
    roi: Roi,
) -> NDArray[np.uint8]:
    try:
        image = images[roi.name]
    except KeyError as exc:
        raise KeyError(f"missing ROI {roi.name!r}") from exc
    validate_image(image)
    if image.shape[:2] != (roi.height, roi.width):
        height, width = image.shape[:2]
        raise ValueError(f"ROI {roi.name!r} must be {roi.width}x{roi.height}, got {width}x{height}")
    return image
