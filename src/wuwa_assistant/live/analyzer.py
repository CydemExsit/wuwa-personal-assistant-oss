"""ROI-only adapter connecting live packets to the proven OCR/parser/tracker core."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, cast

from wuwa_assistant.capture import RoiPacket, RoiPacketKind
from wuwa_assistant.models import PageType, to_jsonable
from wuwa_assistant.ocr.base import OcrEngine
from wuwa_assistant.parsers import EchoPageParser
from wuwa_assistant.state import EchoStateTracker
from wuwa_assistant.vision.classifier import PageClassifier
from wuwa_assistant.vision.rois import PAGE_HEADER_ROI

from .dto import AnalysisResult

_CONTENT_PAGES = {
    PageType.ECHO_SELECTION,
    PageType.ECHO_ENHANCEMENT,
    PageType.ENHANCEMENT_MATERIAL_SELECTION,
}


@dataclass(slots=True)
class _PreparedTrackerCommit:
    result: AnalysisResult
    tracker: EchoStateTracker
    revision: int


class LiveEchoRoiAnalyzer:
    """Run local OCR only against owned crops from :class:`RoiPacket`.

    No debug writer is configured here, so the normal live path cannot persist an
    image.  The tracker uses deferred material confirmation: selecting a material is
    not treated as consumption until the subsequent enhanced target is observed.
    """

    def __init__(self, ocr: OcrEngine, *, selection_only: bool = False) -> None:
        self._ocr = ocr
        self.classifier = PageClassifier(ocr)
        self.parser = EchoPageParser(ocr)
        self.tracker = EchoStateTracker(defer_material_consumption=True)
        self._tracker_lock = RLock()
        self._tracker_revision = 0
        self._prepared: _PreparedTrackerCommit | None = None
        self._selection_only = selection_only
        self._force_observation = False

    def reset(self) -> None:
        """Start a new capture-session lineage without reinitializing the OCR model."""

        self._clear_ocr_cache()
        with self._tracker_lock:
            self.tracker.cancel_pending_consumption()
            self.tracker = EchoStateTracker(defer_material_consumption=True)
            self._tracker_revision += 1
            self._prepared = None
            self._force_observation = False

    def request_recheck(self) -> None:
        """Refresh the next accepted observation without resetting echo lineage."""
        self._clear_ocr_cache()
        with self._tracker_lock:
            self._tracker_revision += 1
            # Clear only after commit, so a canceled in-flight OCR cannot consume
            # the request. This also refreshes results already queued for Qt.
            self._force_observation = True

    def stop(self) -> None:
        """Cancel reversible material selection when capture stops."""

        self._clear_ocr_cache()
        with self._tracker_lock:
            self.tracker.cancel_pending_consumption()
            self._tracker_revision += 1
            self._prepared = None

    def _clear_ocr_cache(self) -> None:
        clear = getattr(self._ocr, "clear_cache", None)
        if callable(clear):
            clear()

    def finalize_analysis(self, result: AnalysisResult, *, accept: bool) -> bool:
        """Commit or discard the tracker transaction prepared by ``analyze``.

        OCR and parsing may take long enough for a newer stable packet to supersede
        the active item.  The coordinator therefore decides freshness first and
        invokes this method while holding its generation lock.  A stale result can
        never advance a local session number or material lineage.
        """

        if not result.requires_commit:
            return True
        with self._tracker_lock:
            prepared = self._prepared
            if prepared is None or prepared.result is not result:
                return False
            self._prepared = None
            if not accept or prepared.revision != self._tracker_revision:
                return not accept
            self.tracker = prepared.tracker
            self._force_observation = False
            return True

    def analyze(self, packet: RoiPacket) -> AnalysisResult:
        # Live selection-only mode reads just the fixed header outside the list.
        # Enhancement/material OCR remains available to offline imports/replays.
        probe_images = (
            {PAGE_HEADER_ROI.name: packet.images[PAGE_HEADER_ROI.name]}
            if self._selection_only
            else packet.images
        )
        classification = self.classifier.classify_rois(probe_images)
        page_type = classification.page_type
        relevant = (
            page_type is PageType.ECHO_SELECTION
            if self._selection_only
            else page_type in _CONTENT_PAGES
        )

        if packet.kind is RoiPacketKind.PROBE:
            return AnalysisResult(
                page_type=page_type.value,
                relevant=relevant,
                confidence=classification.confidence,
                observation={
                    "page_type": page_type.value,
                    "confidence": {"page_type": classification.confidence},
                    "events": [],
                },
                request_content=relevant,
            )

        # A content packet includes the three small probe ROIs.  Reclassifying
        # them before parsing prevents stale page layouts from becoming facts.
        if packet.page_type != page_type.value or not relevant:
            return AnalysisResult(
                page_type=page_type.value,
                relevant=relevant,
                confidence=classification.confidence,
                observation={
                    "page_type": page_type.value,
                    "confidence": {"page_type": classification.confidence},
                    "events": [],
                },
                request_content=relevant,
            )

        source_file = Path(packet.source_file).name if packet.source_file else None
        observation = self.parser.parse_rois(
            packet.images,
            classification,
            source_file=source_file,
            source_timestamp=packet.captured_at,
        )
        if (
            page_type is PageType.ECHO_SELECTION
            and observation.name is None
            and observation.level is None
            and observation.cost is None
            and not any(
                stat.name is not None and stat.value is not None
                for stat in (*observation.main_stats, *observation.sub_stats)
            )
        ):
            # A screenshot editor/notification can leave the COST page anchor
            # visible while covering all real echo content.  Rejected/empty
            # observations must not mutate the tracker or inherit its session.
            return AnalysisResult(
                page_type=page_type.value,
                relevant=False,
                confidence=None,
                observation={"page_type": page_type.value, "confidence": {}, "events": []},
                request_content=True,
            )
        with self._tracker_lock:
            if self._prepared is not None:
                raise RuntimeError("the previous tracker transaction was not finalized")
            unchanged = not self._force_observation and self.tracker.is_semantically_unchanged(
                observation
            )
            candidate = self.tracker if unchanged else deepcopy(self.tracker)
            emitted = [] if unchanged else candidate.process_observation(observation)
            payload = cast(dict[str, Any], to_jsonable(observation))
            payload["events"] = to_jsonable(emitted)
            payload["current_session"] = self._current_session_summary(candidate)
            result = AnalysisResult(
                page_type=page_type.value,
                relevant=True,
                confidence=observation.overall_confidence,
                observation=payload,
                request_content=True,
                unchanged=unchanged,
                requires_commit=not unchanged,
            )
            if not unchanged:
                self._prepared = _PreparedTrackerCommit(
                    result=result,
                    tracker=candidate,
                    revision=self._tracker_revision,
                )
            return result

    @staticmethod
    def _current_session_summary(tracker: EchoStateTracker) -> dict[str, object] | None:
        session = tracker.current_session
        if session is None:
            return None
        latest = session.latest_observation
        return {
            "local_session_id": session.local_session_id,
            "current_state": session.current_state.value,
            "is_discarded": session.is_discarded,
            "was_consumed": session.was_consumed,
            "consumed_into_session_id": session.consumed_into_session_id,
            "latest_level": latest.level if latest is not None else None,
        }


__all__ = ["LiveEchoRoiAnalyzer"]
