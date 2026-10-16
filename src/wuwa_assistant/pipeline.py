"""Offline image and ordered-static-sequence analysis pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import cv2
import numpy as np
from numpy.typing import NDArray

from wuwa_assistant.models import EchoEvent, EchoObservation, EchoSession, PageType
from wuwa_assistant.ocr.base import OcrEngine
from wuwa_assistant.parsers import EchoPageParser
from wuwa_assistant.state import EchoStateTracker
from wuwa_assistant.vision.classifier import PageClassifier
from wuwa_assistant.vision.debug import DebugWriter
from wuwa_assistant.vision.rois import PAGE_HEADER_ROI, STABILITY_ROIS_BY_PAGE
from wuwa_assistant.vision.stability import semantic_roi_fingerprint


def load_local_image(path: Path) -> NDArray[np.uint8]:
    """Read a user-supplied local image, including paths containing non-ASCII text."""

    if not path.is_file():
        raise FileNotFoundError(path)
    encoded = np.fromfile(path, dtype=np.uint8)
    image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"Unsupported or corrupt image: {path}")
    return cast(NDArray[np.uint8], image)


class ImageAnalyzer:
    def __init__(self, ocr: OcrEngine, *, debug_root: Path | None = None) -> None:
        self.debug_writer = DebugWriter(debug_root)
        self.classifier = PageClassifier(ocr, self.debug_writer)
        self.parser = EchoPageParser(ocr, self.debug_writer)

    def analyze_path(self, path: Path) -> EchoObservation:
        resolved = path.resolve()
        timestamp = datetime.fromtimestamp(resolved.stat().st_mtime, tz=UTC).isoformat()
        frame = load_local_image(resolved)
        return self.analyze_frame(
            frame,
            source_file=resolved,
            source_timestamp=timestamp,
        )

    def analyze_frame(
        self,
        frame: NDArray[np.uint8],
        *,
        source_file: str | Path,
        source_timestamp: str | None,
    ) -> EchoObservation:
        classification = self.classifier.classify(frame, source_file=source_file)
        observation = self.parser.parse(
            frame,
            classification,
            source_file=source_file,
            source_timestamp=source_timestamp,
        )

        rois = STABILITY_ROIS_BY_PAGE.get(observation.page_type.value, (PAGE_HEADER_ROI,))
        if observation.page_type is not PageType.UNKNOWN:
            fingerprint = semantic_roi_fingerprint(frame, rois)
            observation.semantic_fingerprint = fingerprint.to_hex()
        return observation


@dataclass(slots=True)
class SequenceAnalysis:
    observations: list[EchoObservation]
    events: list[EchoEvent]
    sessions: list[EchoSession]


class StaticSequenceAnalyzer:
    """Treat ordered user-provided files as already-stable snapshots.

    Each snapshot still receives a semantic ROI fingerprint. The tracker suppresses an
    identical semantic state, while the future live backend uses ``StabilityGate`` before
    invoking this image pipeline.
    """

    def __init__(self, image_analyzer: ImageAnalyzer) -> None:
        self.image_analyzer = image_analyzer

    def analyze_paths(self, paths: list[Path]) -> SequenceAnalysis:
        tracker = EchoStateTracker()
        observations: list[EchoObservation] = []
        for path in paths:
            observation = self.image_analyzer.analyze_path(path)
            observations.append(observation)
            tracker.process_observation(observation)
        return SequenceAnalysis(
            observations=observations,
            events=list(tracker.events),
            sessions=list(tracker.sessions.values()),
        )
