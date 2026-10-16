from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
from threading import Event, Lock

import cv2
import numpy as np

from wuwa_assistant.capture import CONTENT_ROIS_BY_PAGE, PROBE_ROIS, RoiPacket, RoiPacketKind
from wuwa_assistant.live import AnalysisResult


def packet(
    sequence: int,
    captured_at: float,
    fingerprint: str,
    *,
    kind: RoiPacketKind = RoiPacketKind.PROBE,
    page_type: str | None = None,
    state_pattern: str | None = None,
    roi_patterns: Mapping[str, str] | None = None,
) -> RoiPacket:
    rois = PROBE_ROIS if kind is RoiPacketKind.PROBE else CONTENT_ROIS_BY_PAGE[page_type or ""]
    pattern = state_pattern or fingerprint
    seed = sha256(pattern.encode("utf-8")).digest()
    overrides = dict(roi_patterns or {})
    unknown_overrides = set(overrides) - {roi.name for roi in rois}
    if unknown_overrides:
        raise ValueError(f"unknown ROI pattern overrides: {sorted(unknown_overrides)!r}")
    images: dict[str, np.ndarray] = {}
    for index, roi in enumerate(rois):
        roi_seed = (
            sha256(overrides[roi.name].encode("utf-8")).digest() if roi.name in overrides else seed
        )
        image = np.zeros((roi.height, roi.width, 3), dtype=np.uint8)
        x_limit = max(1, roi.width - 24)
        y_limit = max(1, roi.height - 24)
        x = 8 + roi_seed[index % len(roi_seed)] % x_limit
        y = 8 + roi_seed[(index + 7) % len(roi_seed)] % y_limit
        cv2.rectangle(
            image,
            (min(x, roi.width - 1), min(y, roi.height - 1)),
            (min(x + 15, roi.width - 1), min(y + 15, roi.height - 1)),
            (255, 255, 255),
            -1,
        )
        images[roi.name] = image
    return RoiPacket(
        kind=kind,
        captured_at=f"2026-08-01T00:00:{sequence:02d}+00:00",
        monotonic_timestamp_ns=round(captured_at * 1_000_000_000),
        monitor_index=1,
        sequence=sequence,
        images=images,
        page_type=page_type,
        source_file=fingerprint,
    )


def accepted_result(
    *,
    confidence: float | None = 0.95,
    relevant: bool = True,
    request_content: bool = False,
) -> AnalysisResult:
    return AnalysisResult(
        page_type="echo_enhancement" if relevant else "unknown",
        relevant=relevant,
        confidence=confidence,
        observation={"name": "不熄獵手", "level": 15},
        request_content=request_content,
    )


class BlockingAnalyzer:
    """Block only the first OCR call so tests can fill and replace the pending slot."""

    def __init__(self) -> None:
        self.first_started = Event()
        self.release_first = Event()
        self._calls: list[str] = []
        self._lock = Lock()

    @property
    def calls(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._calls)

    def analyze(self, roi_packet: RoiPacket) -> AnalysisResult:
        with self._lock:
            assert roi_packet.source_file is not None
            self._calls.append(roi_packet.source_file)
            call_number = len(self._calls)
        if call_number == 1:
            self.first_started.set()
            if not self.release_first.wait(3.0):
                raise TimeoutError("test did not release the first analyzer call")
        return accepted_result()
