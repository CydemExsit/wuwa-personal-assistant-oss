"""Thread-safe live recognition coordinator with latest-only OCR backpressure."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from threading import Condition, RLock, Thread, current_thread
from time import monotonic
from typing import Protocol

from wuwa_assistant.capture.roi_packet import RoiPacket, RoiPacketKind
from wuwa_assistant.vision.discard import detect_selection_discard_toggle
from wuwa_assistant.vision.equipped import detect_selected_echo_equipped
from wuwa_assistant.vision.equipped_slot import highlighted_equipped_slot
from wuwa_assistant.vision.preprocess import (
    gold_text_mask,
    low_saturation_text_mask,
    preprocess_for_hash,
)
from wuwa_assistant.vision.rois import (
    PAGE_HEADER_ROI,
    SELECTION_DISCARD_TOGGLE_ROI,
    SELECTION_EQUIPPED_SLOTS_ROI,
    SELECTION_GRID_ROI,
    STABILITY_ROIS_BY_PAGE,
)
from wuwa_assistant.vision.selection_identity import extract_selection_identity
from wuwa_assistant.vision.stability import (
    SemanticFingerprint,
    fingerprints_match,
    perceptual_hash,
)

from .dto import (
    AnalysisResult,
    CoordinatorStats,
    GenerationToken,
    LiveRecognitionResult,
    LiveRuntimeError,
    LiveStatus,
    LiveStatusUpdate,
    PacketDisposition,
)

logger = logging.getLogger(__name__)


class RoiAnalyzer(Protocol):
    """Offline analyzer contract used by the single OCR worker."""

    def analyze(self, packet: RoiPacket) -> AnalysisResult:
        """Analyze only the owned ROI crops contained in ``packet``."""


class TransactionalRoiAnalyzer(Protocol):
    """Optional analyzer hook for state that must commit only when fresh."""

    def finalize_analysis(self, result: AnalysisResult, *, accept: bool) -> bool:
        """Commit an accepted result or discard a stale/canceled result."""


type StatusCallback = Callable[[LiveStatusUpdate], None]
type ResultCallback = Callable[[LiveRecognitionResult], None]
type ErrorCallback = Callable[[LiveRuntimeError], None]
type FingerprintMatcher = Callable[[SemanticFingerprint, SemanticFingerprint], bool]
type FingerprintFactory = Callable[[RoiPacket], SemanticFingerprint]


@dataclass(slots=True)
class _WorkItem:
    generation: int
    packet: RoiPacket
    fingerprint: SemanticFingerprint
    recovery_epoch: int | None = None


@dataclass(slots=True)
class _MutableStats:
    submitted_packets: int = 0
    stale_generation_dropped: int = 0
    stopped_dropped: int = 0
    out_of_order_dropped: int = 0
    unstable_samples: int = 0
    duplicate_states_dropped: int = 0
    scheduled: int = 0
    pending_replaced: int = 0
    analysis_started: int = 0
    analysis_completed: int = 0
    analysis_errors: int = 0
    canceled_results_dropped: int = 0
    superseded_results_dropped: int = 0
    results_emitted: int = 0
    callback_errors: int = 0

    def snapshot(self) -> CoordinatorStats:
        return CoordinatorStats(
            submitted_packets=self.submitted_packets,
            stale_generation_dropped=self.stale_generation_dropped,
            stopped_dropped=self.stopped_dropped,
            out_of_order_dropped=self.out_of_order_dropped,
            unstable_samples=self.unstable_samples,
            duplicate_states_dropped=self.duplicate_states_dropped,
            scheduled=self.scheduled,
            pending_replaced=self.pending_replaced,
            analysis_started=self.analysis_started,
            analysis_completed=self.analysis_completed,
            analysis_errors=self.analysis_errors,
            canceled_results_dropped=self.canceled_results_dropped,
            superseded_results_dropped=self.superseded_results_dropped,
            results_emitted=self.results_emitted,
            callback_errors=self.callback_errors,
        )


class LiveRecognitionCoordinator:
    """Coordinate stability, one OCR worker, cancellation, and safe callbacks.

    Callers submit small :class:`RoiPacket` objects from a passive capture loop.  A
    state must remain unchanged for the configured duration and minimum sample count
    before it is queued.  OCR has capacity for one active and one pending item; a newer
    stable state replaces the pending item.  Results superseded by a newer scheduled
    state are discarded before callbacks.

    Status callbacks can run on the submitter or OCR worker thread.  Result and analyzer
    error callbacks run on the OCR worker thread.  Callbacks must therefore be quick and
    thread-safe; Qt integration should bridge them through queued signals.
    """

    def __init__(
        self,
        analyzer: RoiAnalyzer,
        *,
        stable_duration_seconds: float = 0.60,
        minimum_stable_samples: int = 3,
        confidence_threshold: float = 0.85,
        fingerprint_matches: FingerprintMatcher | None = None,
        fingerprint_factory: FingerprintFactory | None = None,
        max_hamming_distance: int = 4,
        selection_recovery: bool = False,
        on_status: StatusCallback | None = None,
        on_result: ResultCallback | None = None,
        on_error: ErrorCallback | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if not 0.50 <= stable_duration_seconds <= 0.75:
            raise ValueError("stable_duration_seconds must be within 0.50..0.75")
        if minimum_stable_samples < 2:
            raise ValueError("minimum_stable_samples must be at least 2")
        if not 0.0 <= confidence_threshold <= 1.0:
            raise ValueError("confidence_threshold must be within [0, 1]")
        if max_hamming_distance < 0:
            raise ValueError("max_hamming_distance cannot be negative")

        self._analyzer = analyzer
        self._stable_duration = stable_duration_seconds
        self._minimum_samples = minimum_stable_samples
        self._confidence_threshold = confidence_threshold
        self._fingerprint_matches = fingerprint_matches or (
            lambda left, right: fingerprints_match(
                left,
                right,
                max_hamming_distance=max_hamming_distance,
            )
        )
        self._fingerprint_factory = fingerprint_factory or _packet_fingerprint
        self._selection_recovery = selection_recovery
        self._on_status = on_status
        self._on_result = on_result
        self._on_error = on_error
        self._clock = clock

        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._running = False
        self._status = LiveStatus.INACTIVE
        self._generation_counter = 0
        self._current_generation: int | None = None
        self._worker: Thread | None = None
        self._pending: _WorkItem | None = None
        self._active: _WorkItem | None = None

        self._candidate_fingerprint: SemanticFingerprint | None = None
        self._candidate_since: float | None = None
        self._candidate_samples = 0
        self._last_source_sequence: int | None = None
        self._last_captured_at: float | None = None
        self._last_scheduled_fingerprint: SemanticFingerprint | None = None
        self._latest_scheduled_sequence: int | None = None
        self._recheck_cutoff_sequence = -1
        self._guard: SemanticFingerprint | None = None
        self._guard_since = 0.0
        self._guard_samples = 0
        self._guard_epoch = 0
        self._recovery_next = 0.0
        self._confirmation_key: str | None = None
        self._manual_recheck = False
        self._probe_since: float | None = None
        self._stats = _MutableStats()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def status(self) -> LiveStatus:
        with self._lock:
            return self._status

    @property
    def generation(self) -> GenerationToken | None:
        with self._lock:
            if self._current_generation is None:
                return None
            return GenerationToken(self._current_generation)

    @property
    def worker_alive(self) -> bool:
        with self._lock:
            return self._worker is not None and self._worker.is_alive()

    @property
    def stats(self) -> CoordinatorStats:
        with self._lock:
            return self._stats.snapshot()

    def start(self) -> GenerationToken:
        """Start a fresh recognition generation and its single OCR worker."""

        with self._condition:
            if self._running:
                raise RuntimeError("live recognition is already running")
            if self._worker is not None and self._worker.is_alive():
                raise RuntimeError("the previous OCR worker has not stopped yet")

            self._generation_counter += 1
            token = GenerationToken(self._generation_counter)
            self._current_generation = token.value
            self._running = True
            self._reset_generation_state_locked()
            worker = Thread(
                target=self._worker_loop,
                args=(token.value,),
                name=f"wuwa-live-ocr-{token.value}",
                daemon=False,
            )
            self._worker = worker
            update = self._transition_locked(
                LiveStatus.WAITING_FOR_PAGE,
                reason="live recognition started; waiting for a stable ROI state",
                force=True,
            )

        worker.start()
        self._publish_status(update)
        return token

    def request_recheck(self) -> int | None:
        """Cancel old work and re-observe fresh crops on the existing OCR worker.

        Return a sequence fence for runtime/UI queued-result rejection. No thread,
        capture session or tracker lineage is restarted; stability still applies.
        """
        with self._condition:
            if not self._running:
                return None
            cutoff = self._last_source_sequence if self._last_source_sequence is not None else -1
            self._recheck_cutoff_sequence = cutoff
            self._pending = None
            self._candidate_fingerprint = None
            self._candidate_since = None
            self._candidate_samples = 0
            self._last_scheduled_fingerprint = None
            self._latest_scheduled_sequence = None
            self._reset_recovery_locked()
            self._manual_recheck = True
            recheck = getattr(self._analyzer, "request_recheck", None)
            if callable(recheck):
                recheck()
            update = self._transition_locked(
                LiveStatus.WAITING_FOR_PAGE,
                reason="manual recheck requested; waiting for fresh stable list crops",
                force=True,
            )
            self._condition.notify_all()
        self._publish_status(update)
        return cutoff

    def stop(self, *, wait: bool = True, timeout: float | None = 5.0) -> bool:
        """Invalidate the generation, clear pending work, and optionally join the worker."""

        if timeout is not None and timeout < 0:
            raise ValueError("timeout cannot be negative")

        with self._condition:
            self._running = False
            self._pending = None
            self._candidate_fingerprint = None
            self._candidate_since = None
            self._candidate_samples = 0
            worker = self._worker
            self._condition.notify_all()
            update = self._transition_locked(
                LiveStatus.STOPPED,
                reason="live recognition stopped",
                force=self._status is not LiveStatus.STOPPED,
            )

        self._publish_status(update)
        if wait and worker is not None and worker is not current_thread():
            worker.join(timeout)
        return worker is None or not worker.is_alive()

    def submit(self, token: GenerationToken, packet: RoiPacket) -> PacketDisposition:
        """Observe one ROI packet and queue it only after duration-based stability."""

        fingerprint = self._fingerprint_factory(packet)
        captured_seconds = packet.monotonic_timestamp_ns / 1_000_000_000
        update: LiveStatusUpdate | None = None
        with self._condition:
            self._stats.submitted_packets += 1
            if not self._running:
                self._stats.stopped_dropped += 1
                return PacketDisposition.DROPPED_STOPPED
            if token.value != self._current_generation:
                self._stats.stale_generation_dropped += 1
                return PacketDisposition.DROPPED_STALE_GENERATION
            if self._packet_is_out_of_order_locked(packet):
                self._stats.out_of_order_dropped += 1
                return PacketDisposition.DROPPED_OUT_OF_ORDER

            self._last_source_sequence = packet.sequence
            self._last_captured_at = captured_seconds
            recovery_ready = self._observe_recovery_locked(packet, fingerprint, captured_seconds)
            if not self._matches_candidate_locked(fingerprint):
                self._candidate_fingerprint = fingerprint
                self._candidate_since = captured_seconds
                self._candidate_samples = 1
                self._stats.unstable_samples += 1
                update = self._transition_locked(
                    LiveStatus.TRANSITIONING,
                    reason="ROI state changed; stability timer restarted",
                    source_sequence=packet.sequence,
                )
                disposition = PacketDisposition.OBSERVING
            else:
                self._candidate_samples += 1
                candidate_since = self._candidate_since
                assert candidate_since is not None
                duration = captured_seconds - candidate_since
                stable = (
                    self._candidate_samples >= self._minimum_samples
                    and duration >= self._stable_duration
                )
                if not stable:
                    self._stats.unstable_samples += 1
                    disposition = PacketDisposition.OBSERVING
                elif self._matches_last_scheduled_locked(fingerprint):
                    self._stats.duplicate_states_dropped += 1
                    disposition = PacketDisposition.DROPPED_DUPLICATE
                else:
                    item = _WorkItem(token.value, packet, fingerprint)
                    replaced = self._pending is not None
                    if replaced:
                        self._stats.pending_replaced += 1
                    self._pending = item
                    self._last_scheduled_fingerprint = fingerprint
                    self._latest_scheduled_sequence = packet.sequence
                    self._stats.scheduled += 1
                    update = self._transition_locked(
                        LiveStatus.STABLE,
                        reason="ROI state is stable and queued for OCR",
                        source_sequence=packet.sequence,
                    )
                    self._condition.notify_all()
                    disposition = (
                        PacketDisposition.REPLACED_PENDING if replaced else PacketDisposition.QUEUED
                    )

            if (
                recovery_ready
                and disposition is PacketDisposition.OBSERVING
                and self._active is None
                and self._pending is None
            ):
                # A transparent crop can keep changing forever. Read the text
                # once identity has settled, but do not commit it until a second
                # independently captured OCR result agrees (see finish_success).
                self._pending = _WorkItem(
                    token.value,
                    packet,
                    fingerprint,
                    self._guard_epoch if packet.kind is RoiPacketKind.CONTENT else None,
                )
                self._latest_scheduled_sequence = packet.sequence
                self._recovery_next = captured_seconds + (
                    1.0 if packet.kind is RoiPacketKind.CONTENT else 2.0
                )
                self._stats.scheduled += 1
                logger.info(
                    "selection recovery queued kind=%s sequence=%s", packet.kind, packet.sequence
                )
                update = self._transition_locked(
                    LiveStatus.STABLE,
                    reason="selection identity stable; checking text despite background animation",
                    source_sequence=packet.sequence,
                )
                self._condition.notify_all()
                disposition = PacketDisposition.QUEUED

        self._publish_status(update)
        return disposition

    def wait_until_idle(self, timeout: float = 5.0) -> bool:
        """Wait until no OCR item is active or pending; primarily useful for shutdown/tests."""

        if timeout < 0:
            raise ValueError("timeout cannot be negative")
        deadline = self._clock() + timeout
        with self._condition:
            while self._active is not None or self._pending is not None:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def _worker_loop(self, generation: int) -> None:
        try:
            while True:
                with self._condition:
                    while (
                        self._pending is None
                        and self._running
                        and self._current_generation == generation
                    ):
                        self._condition.wait()
                    if self._pending is None and (
                        not self._running or self._current_generation != generation
                    ):
                        return
                    item = self._pending
                    assert item is not None
                    self._pending = None
                    self._active = item
                    self._stats.analysis_started += 1
                    update = self._transition_locked(
                        LiveStatus.RECOGNIZING,
                        reason="offline OCR is analyzing stable ROI crops",
                        source_sequence=item.packet.sequence,
                    )
                self._publish_status(update)

                try:
                    analysis = self._analyzer.analyze(item.packet)
                except Exception as exc:
                    self._finish_error(item, exc)
                    continue
                self._finish_success(item, analysis)
        finally:
            with self._condition:
                self._active = None
                if self._worker is current_thread():
                    self._worker = None
                self._condition.notify_all()

    def _finish_success(self, item: _WorkItem, analysis: AnalysisResult) -> None:
        completed_at = self._clock()
        result: LiveRecognitionResult | None = None
        error: LiveRuntimeError | None = None
        with self._condition:
            self._stats.analysis_completed += 1
            self._active = None
            drop_reason = self._result_drop_reason_locked(item)
            try:
                if drop_reason == "canceled":
                    self._finalize_analysis_locked(analysis, accept=False)
                    self._stats.canceled_results_dropped += 1
                    update = None
                elif drop_reason == "superseded" and (
                    not analysis.requires_commit or item.recovery_epoch is not None
                ):
                    self._finalize_analysis_locked(analysis, accept=False)
                    self._stats.superseded_results_dropped += 1
                    update = None
                elif self._needs_recovery_confirmation_locked(item, analysis):
                    self._finalize_analysis_locked(analysis, accept=False)
                    self._last_scheduled_fingerprint = None
                    update = self._transition_locked(
                        LiveStatus.TRANSITIONING,
                        reason="background recovery waiting for matching text from a fresh frame",
                        source_sequence=item.packet.sequence,
                        force=True,
                    )
                elif not self._finalize_analysis_locked(analysis, accept=True):
                    # Analyzer lifecycle invalidated its prepared tracker snapshot.
                    # Treat it like cancellation rather than exposing uncommitted data.
                    self._stats.canceled_results_dropped += 1
                    update = None
                else:
                    self._confirmation_key = None
                    if item.packet.kind is RoiPacketKind.CONTENT:
                        self._manual_recheck = False
                    result = LiveRecognitionResult(
                        generation=item.generation,
                        source_sequence=item.packet.sequence,
                        captured_at=item.packet.captured_at,
                        completed_at=completed_at,
                        packet_kind=item.packet.kind,
                        state_fingerprint=_render_fingerprint(item.packet, item.fingerprint),
                        analysis=analysis,
                    )
                    self._stats.results_emitted += 1
                    if (
                        not analysis.relevant
                        or analysis.confidence is None
                        or analysis.confidence < self._confidence_threshold
                        or (
                            analysis.page_type == "echo_selection"
                            and analysis.observation.get("is_equipped") is True
                            and analysis.observation.get("equipped_slot_index") is None
                        )
                    ) and drop_reason is None:
                        # A transient unknown header/partly faded owner or slot
                        # must not be remembered forever as a completed state.
                        # Retrying unchanged evidence is bounded to >=2.6 seconds;
                        # analyzer semantic dedup still prevents repeated writes.
                        self._last_scheduled_fingerprint = None
                        self._candidate_since = (self._last_captured_at or 0.0) + 2.0
                        self._candidate_samples = 0
                    if analysis.request_content and (
                        item.packet.kind is RoiPacketKind.PROBE
                        or item.packet.page_type != analysis.page_type
                    ):
                        status = LiveStatus.TRANSITIONING
                        reason = "relevant page classified; waiting for content ROI packet"
                    elif not analysis.relevant:
                        status = LiveStatus.WAITING_FOR_PAGE
                        reason = "stable page is not an echo enhancement page"
                    elif (
                        analysis.confidence is None
                        or analysis.confidence < self._confidence_threshold
                    ):
                        status = LiveStatus.LOW_CONFIDENCE
                        reason = "OCR result requires manual confirmation"
                    else:
                        status = LiveStatus.STABLE
                        reason = "stable OCR result accepted"
                    update = self._transition_locked(
                        status,
                        reason=reason,
                        source_sequence=item.packet.sequence,
                    )
            except Exception as exc:
                result = None
                self._stats.analysis_errors += 1
                error = LiveRuntimeError(
                    generation=item.generation,
                    source_sequence=item.packet.sequence,
                    emitted_at=completed_at,
                    error_type=type(exc).__name__,
                    message=str(exc),
                )
                update = self._transition_locked(
                    LiveStatus.ERROR,
                    reason="offline OCR analyzer commit failed",
                    source_sequence=item.packet.sequence,
                )
            self._condition.notify_all()

        self._publish_status(update)
        if error is not None:
            self._publish_error(error)
        elif result is not None:
            self._publish_result(result)

    def _finalize_analysis_locked(self, analysis: AnalysisResult, *, accept: bool) -> bool:
        """Finalize optional analyzer state while scheduling freshness is locked."""

        finalize = getattr(self._analyzer, "finalize_analysis", None)
        if not callable(finalize):
            return True
        return bool(finalize(analysis, accept=accept))

    def _finish_error(self, item: _WorkItem, exc: Exception) -> None:
        emitted_at = self._clock()
        error: LiveRuntimeError | None = None
        with self._condition:
            self._stats.analysis_errors += 1
            self._active = None
            drop_reason = self._result_drop_reason_locked(item)
            if drop_reason == "canceled":
                self._stats.canceled_results_dropped += 1
                update = None
            elif drop_reason == "superseded":
                self._stats.superseded_results_dropped += 1
                update = None
            else:
                # A transient OCR failure must not permanently deduplicate this
                # unchanged screen. Re-observe it with a bounded two-second delay.
                self._last_scheduled_fingerprint = None
                self._candidate_since = (self._last_captured_at or 0.0) + 2.0
                self._candidate_samples = 0
                self._confirmation_key = None
                self._recovery_next = (self._last_captured_at or 0.0) + 2.0
                error = LiveRuntimeError(
                    generation=item.generation,
                    source_sequence=item.packet.sequence,
                    emitted_at=emitted_at,
                    error_type=type(exc).__name__,
                    message=str(exc),
                )
                update = self._transition_locked(
                    LiveStatus.ERROR,
                    reason="offline OCR analyzer failed",
                    source_sequence=item.packet.sequence,
                )
            self._condition.notify_all()

        self._publish_status(update)
        if error is not None:
            self._publish_error(error)

    def _reset_generation_state_locked(self) -> None:
        self._pending = None
        self._active = None
        self._candidate_fingerprint = None
        self._candidate_since = None
        self._candidate_samples = 0
        self._last_source_sequence = None
        self._last_captured_at = None
        self._last_scheduled_fingerprint = None
        self._latest_scheduled_sequence = None
        self._recheck_cutoff_sequence = -1
        self._reset_recovery_locked()

    def _reset_recovery_locked(self) -> None:
        self._guard = None
        self._guard_since = 0.0
        self._guard_samples = 0
        self._guard_epoch += 1
        self._recovery_next = 0.0
        self._confirmation_key = None
        self._manual_recheck = False
        self._probe_since = None

    def _observe_recovery_locked(
        self, packet: RoiPacket, fingerprint: SemanticFingerprint, timestamp: float
    ) -> bool:
        if not self._selection_recovery:
            return False
        if packet.kind is RoiPacketKind.PROBE:
            if self._probe_since is None:
                self._probe_since = timestamp
            if self._guard is not None:
                self._guard = None
                self._guard_epoch += 1
                self._confirmation_key = None
            # Classification-only probes contain no echo facts. A moving header
            # must not prevent startup, reset or return from another page.
            return timestamp - self._probe_since >= 2.0 and timestamp >= self._recovery_next
        self._probe_since = None
        guard = _selection_guard(fingerprint) if packet.kind is RoiPacketKind.CONTENT else None
        if guard is None:
            if self._guard is not None:
                self._guard = None
                self._guard_epoch += 1
                self._confirmation_key = None
            return False
        if self._guard is None or not self._fingerprint_matches(self._guard, guard):
            self._guard = guard
            self._guard_since = timestamp
            self._guard_samples = 1
            self._guard_epoch += 1
            self._confirmation_key = None
            return False
        self._guard_samples += 1
        delay = self._stable_duration if self._manual_recheck else 2.0
        return (
            self._guard_samples >= self._minimum_samples
            and timestamp - self._guard_since >= delay
            and timestamp >= self._recovery_next
        )

    def _needs_recovery_confirmation_locked(
        self, item: _WorkItem, analysis: AnalysisResult
    ) -> bool:
        if item.recovery_epoch is None or not analysis.relevant:
            return False
        # Page classification alone does not contain an echo and may route back
        # to probing immediately. Parsed content needs consecutive agreement.
        if analysis.page_type != "echo_selection" or not any(
            analysis.observation.get(key) is not None for key in ("name", "level", "cost")
        ):
            return False
        key = _observation_confirmation_key(analysis)
        matched = self._confirmation_key == key
        self._confirmation_key = key
        return not matched

    def _packet_is_out_of_order_locked(self, packet: RoiPacket) -> bool:
        sequence = self._last_source_sequence
        captured_at = self._last_captured_at
        packet_time = packet.monotonic_timestamp_ns / 1_000_000_000
        return (sequence is not None and packet.sequence <= sequence) or (
            captured_at is not None and packet_time < captured_at
        )

    def _matches_candidate_locked(self, fingerprint: SemanticFingerprint) -> bool:
        candidate = self._candidate_fingerprint
        return candidate is not None and self._fingerprint_matches(candidate, fingerprint)

    def _matches_last_scheduled_locked(self, fingerprint: SemanticFingerprint) -> bool:
        previous = self._last_scheduled_fingerprint
        return previous is not None and self._fingerprint_matches(previous, fingerprint)

    def _result_drop_reason_locked(self, item: _WorkItem) -> str | None:
        if (
            not self._running
            or item.generation != self._current_generation
            or item.packet.sequence <= self._recheck_cutoff_sequence
            or (item.recovery_epoch is not None and item.recovery_epoch != self._guard_epoch)
        ):
            return "canceled"
        latest = self._latest_scheduled_sequence
        if latest is not None and item.packet.sequence < latest:
            return "superseded"
        return None

    def _transition_locked(
        self,
        status: LiveStatus,
        *,
        reason: str,
        source_sequence: int | None = None,
        force: bool = False,
    ) -> LiveStatusUpdate | None:
        if self._status is status and not force:
            return None
        self._status = status
        return LiveStatusUpdate(
            status=status,
            generation=self._current_generation,
            emitted_at=self._clock(),
            reason=reason,
            source_sequence=source_sequence,
        )

    def _publish_status(self, update: LiveStatusUpdate | None) -> None:
        if update is not None and self._on_status is not None:
            self._invoke_callback(self._on_status, update)

    def _publish_result(self, result: LiveRecognitionResult) -> None:
        if self._on_result is not None:
            self._invoke_callback(self._on_result, result)

    def _publish_error(self, error: LiveRuntimeError) -> None:
        if self._on_error is not None:
            self._invoke_callback(self._on_error, error)

    def _invoke_callback[T](
        self,
        callback: Callable[[T], None],
        payload: T,
    ) -> None:
        try:
            callback(payload)
        except Exception:
            with self._lock:
                self._stats.callback_errors += 1


def _selection_guard(fingerprint: SemanticFingerprint) -> SemanticFingerprint | None:
    names = (
        "selection_selected_cell",
        "selection_selected_card_art",
        "selection_discard_state",
        "selection_active_equipped_slot",
    )
    pairs = tuple(
        (name, value)
        for name, value in zip(fingerprint.roi_names, fingerprint.hashes, strict=True)
        if name.startswith("content:echo_selection:") and name.rsplit(":", 1)[-1] in names
    )
    if len(pairs) != len(names):
        return None
    return SemanticFingerprint(tuple(name for name, _ in pairs), tuple(value for _, value in pairs))


def _observation_confirmation_key(analysis: AnalysisResult) -> str:
    # Compare semantic values, never OCR confidence, timestamps, bounding boxes
    # or provisional tracker events/session IDs. No pixels are retained here.
    scalar_fields = (
        "name",
        "set_name",
        "level",
        "projected_level",
        "cost",
        "locked_substat_slots",
        "is_discarded",
        "is_equipped",
        "equipped_character_name",
        "equipped_slot_index",
        "material_source_selected",
        "material_source_level",
    )
    values = {name: analysis.observation.get(name) for name in scalar_fields}
    for name in ("main_stats", "projected_main_stats", "sub_stats"):
        stats = analysis.observation.get(name)
        values[name] = (
            [
                {key: stat.get(key) for key in ("name", "value", "unit")}
                for stat in stats
                if isinstance(stat, dict)
            ]
            if isinstance(stats, list)
            else []
        )
    return json.dumps(values, ensure_ascii=False, sort_keys=True)


def _packet_fingerprint(packet: RoiPacket) -> SemanticFingerprint:
    """Hash only stable semantic crops, never animated/parser-only packet images.

    Probe packets need only the fixed page header to become stable before bounded page
    classification.  Content packets use the page-specific semantic ROI allowlist.  In
    particular, the large parser panels and material CTA remain available to OCR but do
    not keep restarting the stability timer as the game background animates.
    """

    prefix = f"{packet.kind.value}:{packet.page_type or 'unclassified'}"
    names = _stability_roi_names(packet)
    missing = tuple(name for name in names if name not in packet.images)
    if missing:
        raise ValueError(f"ROI packet is missing required stability ROI(s): {missing!r}")
    items = tuple((name, packet.images[name]) for name in names)
    roi_names = tuple(f"{prefix}:{name}" for name, _image in items)
    hashes = tuple(
        perceptual_hash(
            # Selection text is drawn over a transparent animated background.
            # CLAHE/Otsu amplified that background and repeatedly reset the
            # stability timer after a card switch.  Keep the visible gold/white
            # glyphs without promoting the dark animated world to text evidence.
            gold_text_mask(image) | low_saturation_text_mask(image)
            if name.startswith("selection_") and name != "selection_primary_action"
            else preprocess_for_hash(image)
        )
        for name, image in items
    )

    # Selection packets need to notice a different selected card even when two
    # echoes have identical visible text.  Hashing all 15 card corners made the
    # gate depend on every animated card at once.  Instead, derive two bounded,
    # pixel-free markers from the uniquely selected card: a high-distance cell
    # code and the selected artwork pHash.  Unknown evidence remains a stable
    # sentinel and never blocks normal text OCR.
    if packet.kind is RoiPacketKind.CONTENT and packet.page_type == "echo_selection":
        cell_marker, card_art_hash, discard_marker = _selection_stability_markers(packet)
        roi_names += (
            f"{prefix}:selection_selected_cell",
            f"{prefix}:selection_selected_card_art",
            f"{prefix}:selection_discard_state",
        )
        hashes += (cell_marker, card_art_hash, discard_marker)
        slot_image = packet.images.get(SELECTION_EQUIPPED_SLOTS_ROI.name)
        if slot_image is None:
            raise ValueError("selection content packet is missing its equipped-slots ROI")
        active_slot = highlighted_equipped_slot(slot_image)
        slot_marker = (
            0
            if active_slot is None
            else (1 << 63) | sum(1 << (active_slot - 1 + 5 * band) for band in range(4))
        )
        roi_names += (f"{prefix}:selection_active_equipped_slot",)
        hashes += (slot_marker,)

    return SemanticFingerprint(
        roi_names=roi_names,
        hashes=hashes,
    )


def _selection_stability_markers(packet: RoiPacket) -> tuple[int, int, int]:
    grid = packet.images.get(SELECTION_GRID_ROI.name)
    if grid is None:
        raise ValueError("selection content packet is missing its grid ROI")
    discard_image = packet.images.get(SELECTION_DISCARD_TOGGLE_ROI.name)
    if discard_image is None:
        raise ValueError("selection content packet is missing its discard-toggle ROI")
    discarded = detect_selection_discard_toggle(discard_image).is_discarded
    discard_marker = _binary_state_marker(discarded)
    detection = detect_selected_echo_equipped(grid)
    evidence = extract_selection_identity(grid, detection.selected_card_bbox)
    if evidence is None:
        return (0, 0, discard_marker)

    # Four repeated one-hot bands plus a common high bit ensure that any two
    # cells differ by eight bits, while unknown-to-known differs by five.  Both
    # exceed the normal per-field tolerance of four without treating this as a
    # game-provided identifier.
    cell_index = evidence.selected_cell - 1
    cell_marker = (1 << 63) | sum(1 << (cell_index + 15 * band) for band in range(4))
    return (cell_marker, int(evidence.card_art_phash, 16), discard_marker)


def _binary_state_marker(value: bool | None) -> int:
    if value is None:
        return 0
    state_index = int(value)
    return (1 << 63) | sum(1 << (state_index + 2 * band) for band in range(4))


def _stability_roi_names(packet: RoiPacket) -> tuple[str, ...]:
    if packet.kind is RoiPacketKind.PROBE:
        return (PAGE_HEADER_ROI.name,)
    if packet.kind is not RoiPacketKind.CONTENT:
        raise ValueError(f"unsupported ROI packet kind for stability: {packet.kind!r}")

    page_type = packet.page_type
    if page_type is None:
        raise ValueError("content ROI packet is missing its page type")
    page_rois = STABILITY_ROIS_BY_PAGE.get(page_type)
    if page_rois is None:
        raise ValueError(f"no stability ROI configuration for page type {page_type!r}")
    return tuple(roi.name for roi in page_rois)


def _render_fingerprint(packet: RoiPacket, fingerprint: SemanticFingerprint) -> str:
    return f"{packet.kind.value}:{packet.page_type or 'unclassified'}:{fingerprint.to_hex()}"
