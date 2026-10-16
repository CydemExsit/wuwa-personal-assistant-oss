from __future__ import annotations

import numpy as np

from wuwa_assistant.models import OcrToken, PageType
from wuwa_assistant.ocr import FakeOcrEngine
from wuwa_assistant.vision.classifier import ENHANCEMENT_STATE_ROI, PageClassifier
from wuwa_assistant.vision.rois import MATERIAL_CTA_ROI, PAGE_HEADER_ROI


def token(text: str, confidence: float = 0.99) -> OcrToken:
    return OcrToken(text=text, confidence=confidence, bbox=(0, 0, 100, 40))


def frame() -> np.ndarray:
    return np.zeros((1440, 2560, 3), dtype=np.uint8)


def test_classifies_all_anchor_families() -> None:
    overview = PageClassifier(FakeOcrEngine([[token("聲骸")]])).classify(frame())
    selection = PageClassifier(FakeOcrEngine([[token("COST")]])).classify(frame())
    enhancement = PageClassifier(
        FakeOcrEngine([[token("聲骸強化")], [token("+15")], [token("強化")]])
    ).classify(frame())
    material = PageClassifier(
        FakeOcrEngine([[token("聲骸強化")], [token("+0 > +15")], [token("強化並調諧")]])
    ).classify(frame())

    assert overview.page_type is PageType.CHARACTER_ECHO_OVERVIEW
    assert selection.page_type is PageType.ECHO_SELECTION
    assert enhancement.page_type is PageType.ECHO_ENHANCEMENT
    assert material.page_type is PageType.ENHANCEMENT_MATERIAL_SELECTION


def test_wrong_resolution_fails_closed_without_ocr() -> None:
    ocr = FakeOcrEngine([[token("聲骸強化")]])
    result = PageClassifier(ocr).classify(np.zeros((720, 1280, 3), dtype=np.uint8))

    assert result.page_type is PageType.UNKNOWN
    assert result.confidence == 0.0
    assert ocr.call_count == 0


def test_live_classifier_accepts_only_bounded_probe_rois() -> None:
    engine = FakeOcrEngine([[token("聲骸強化")], [token("+15")], [token("強化")]])
    rois = {
        PAGE_HEADER_ROI.name: np.zeros(
            (PAGE_HEADER_ROI.height, PAGE_HEADER_ROI.width, 3), dtype=np.uint8
        ),
        ENHANCEMENT_STATE_ROI.name: np.zeros(
            (ENHANCEMENT_STATE_ROI.height, ENHANCEMENT_STATE_ROI.width, 3), dtype=np.uint8
        ),
        MATERIAL_CTA_ROI.name: np.zeros(
            (MATERIAL_CTA_ROI.height, MATERIAL_CTA_ROI.width, 3), dtype=np.uint8
        ),
    }

    result = PageClassifier(engine).classify_rois(rois)

    assert result.page_type is PageType.ECHO_ENHANCEMENT
    assert engine.call_count == 3
    assert all(image.shape[0] < 1440 and image.shape[1] < 2560 for image in rois.values())


def test_live_classifier_missing_probe_fails_closed_without_guessing() -> None:
    result = PageClassifier(FakeOcrEngine()).classify_rois({})

    assert result.page_type is PageType.UNKNOWN
    assert result.confidence == 0.0
    assert "missing ROI" in result.evidence[0]
