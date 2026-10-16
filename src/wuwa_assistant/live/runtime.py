"""Passive capture loop that owns full frames only long enough to copy ROIs."""

from __future__ import annotations

from collections.abc import Callable
from math import isfinite
from threading import Event, RLock, Thread, current_thread
from time import monotonic

from wuwa_assistant.capture import (
    CONTENT_ROIS_BY_PAGE,
    CaptureBackend,
    CaptureConfig,
    CaptureExhaustedError,
    ExtractedRois,
    InvalidMonitorError,
    MonitorInfo,
    RoiExtractor,
    RoiPacket,
    RoiPacketKind,
)
from wuwa_assistant.display import RESOLUTIONS
from wuwa_assistant.vision.preprocess import preprocess_for_hash
from wuwa_assistant.vision.rois import PAGE_HEADER_ROI
from wuwa_assistant.vision.stability import perceptual_hash

from .coordinator import LiveRecognitionCoordinator, RoiAnalyzer
from .dto import (
    GenerationToken,
    LiveRecognitionResult,
    LiveRuntimeError,
    LiveStatus,
    LiveStatusUpdate,
)

type StatusCallback = Callable[[LiveStatusUpdate], None]
type ResultCallback = Callable[[LiveRecognitionResult], None]
type ErrorCallback = Callable[[LiveRuntimeError], None]


class LiveCaptureRuntime:
    """Sample one selected monitor and submit ROI packets to one OCR worker.

    The capture thread is the only owner of the backend and every full-frame
    lease.  The coordinator receives immutable ROI packets.  User callbacks receive
    only array-free DTOs.
    """

    def __init__(
        self,
        backend: CaptureBackend,
        config: CaptureConfig,
        analyzer: RoiAnalyzer,
        *,
        stable_duration_seconds: float = 0.60,
        minimum_stable_samples: int = 3,
        confidence_threshold: float = 0.85,
        selection_only: bool = False,
        manual_mode: bool = False,
        manual_timeout_seconds: float = 30.0,
        on_status: StatusCallback | None = None,
        on_result: ResultCallback | None = None,
        on_error: ErrorCallback | None = None,
    ) -> None:
        if not isinstance(manual_mode, bool):
            raise ValueError("manual_mode must be a boolean")
        if (
            isinstance(manual_timeout_seconds, bool)
            or not isfinite(manual_timeout_seconds)
            or manual_timeout_seconds <= 0
        ):
            raise ValueError("manual_timeout_seconds must be finite and positive")
        self._backend = backend
        self._config = config
        self._analyzer = analyzer
        self._on_status = on_status
        self._on_result = on_result
        self._on_error = on_error
        self._extractor = RoiExtractor()
        self._stop_event = Event()
        self._manual_requested = Event()
        self._manual_mode = manual_mode
        self._manual_timeout_seconds = manual_timeout_seconds
        self._manual_deadline: float | None = None
        self._lock = RLock()
        self._capture_thread: Thread | None = None
        self._content_page_type: str | None = None
        self._last_sequence: int | None = None
        self._token: GenerationToken | None = None
        self._layout_header_hash: int | None = None
        self._submitted_headers: dict[int, int] = {}
        self._selection_only = selection_only
        self._routing_revision = 0
        self._result_floor = -1
        self._force_probe = False
        self._content_transition_since: float | None = None
        self.coordinator = LiveRecognitionCoordinator(
            analyzer,
            stable_duration_seconds=stable_duration_seconds,
            minimum_stable_samples=minimum_stable_samples,
            confidence_threshold=confidence_threshold,
            selection_recovery=selection_only,
            on_status=self._publish_status,
            on_result=self._handle_result,
            on_error=self._publish_error,
        )

    @property
    def is_running(self) -> bool:
        with self._lock:
            capture_alive = self._capture_thread is not None and self._capture_thread.is_alive()
        return capture_alive or self.coordinator.worker_alive

    @property
    def content_page_type(self) -> str | None:
        with self._lock:
            return self._content_page_type

    def start(self) -> GenerationToken:
        with self._lock:
            if self._capture_thread is not None and self._capture_thread.is_alive():
                raise RuntimeError("live capture is already running")
            self._invoke_lifecycle("reset")
            self._stop_event.clear()
            self._content_page_type = None
            self._last_sequence = None
            self._layout_header_hash = None
            self._submitted_headers.clear()
            self._routing_revision += 1
            self._result_floor = -1
            self._force_probe = False
            self._content_transition_since = None
            self._manual_requested.set()
            self._manual_deadline = monotonic() + self._manual_timeout_seconds
            token = self.coordinator.start()
            self._token = token
            thread = Thread(
                target=self._capture_loop,
                args=(token,),
                name=f"wuwa-monitor-capture-{token.value}",
                daemon=False,
            )
            self._capture_thread = thread
            thread.start()
            return token

    def request_recheck(self) -> int | None:
        """Reclassify fresh list crops without stopping capture or its OCR worker."""
        with self._lock:
            cutoff = self.coordinator.request_recheck()
            if cutoff is None:
                return None
            self._routing_revision += 1
            self._result_floor = cutoff
            self._content_page_type = None
            self._layout_header_hash = None
            self._submitted_headers.clear()
            self._force_probe = True
            self._content_transition_since = None
            self._manual_deadline = monotonic() + self._manual_timeout_seconds
            self._manual_requested.set()
            return cutoff

    def stop(self, *, timeout: float = 8.0) -> bool:
        if timeout < 0:
            raise ValueError("timeout cannot be negative")
        deadline = monotonic() + timeout
        self._stop_event.set()
        self.coordinator.stop(wait=False)
        with self._lock:
            thread = self._capture_thread
        if thread is not None and thread is not current_thread():
            thread.join(max(0.0, deadline - monotonic()))
        coordinator_stopped = self.coordinator.stop(
            wait=True, timeout=max(0.0, deadline - monotonic())
        )
        capture_stopped = thread is None or not thread.is_alive()
        self._invoke_lifecycle("stop")
        return capture_stopped and coordinator_stopped

    def _capture_loop(self, token: GenerationToken) -> None:
        exhausted = False
        try:
            monitor = self._selected_monitor()
            if (monitor.width, monitor.height) != RESOLUTIONS[self._config.resolution]:
                raise InvalidMonitorError(
                    f"顯示器 {monitor.index} 與所選解析度 {self._config.resolution} 不符; "
                    f"got {monitor.width}x{monitor.height}"
                )

            next_deadline = monotonic()
            while not self._stop_event.is_set():
                with self._lock:
                    if (
                        self._manual_mode
                        and self._manual_requested.is_set()
                        and self._manual_deadline is not None
                        and monotonic() >= self._manual_deadline
                    ):
                        self._finish_manual_locked("manual request timed out")
                    should_capture = not self._manual_mode or self._manual_requested.is_set()
                    revision = self._routing_revision
                    page_type = self._content_page_type
                if not should_capture:
                    # Keep the session and OCR model, but acquire no frame and
                    # schedule no OCR until the next explicit button request.
                    self._stop_event.wait(0.05)
                    next_deadline = monotonic()
                    continue
                try:
                    frame = self._backend.capture_monitor(self._config.monitor_index)
                except CaptureExhaustedError:
                    exhausted = True
                    break

                expected_width, expected_height = RESOLUTIONS[self._config.resolution]
                if frame.pixels.shape[:2] != (expected_height, expected_width):
                    frame.release()
                    raise InvalidMonitorError("擷取解析度已改變, 請停止後重新選擇解析度。")
                extracted = self._extractor.extract(frame, page_type=page_type)
                with self._lock:
                    # A manual request during capture cannot submit old-layout
                    # crops as a fresh post-reset observation.
                    if revision == self._routing_revision and not self._stop_event.is_set():
                        packet = self._route_packet(extracted)
                        self._last_sequence = packet.sequence
                        self.coordinator.submit(token, packet)

                next_deadline += self._config.interval_seconds
                wait_seconds = max(0.0, next_deadline - monotonic())
                if self._stop_event.wait(wait_seconds):
                    break

            if exhausted:
                self.coordinator.wait_until_idle(timeout=8.0)
        except Exception as exc:
            self._publish_error(
                LiveRuntimeError(
                    generation=token.value,
                    source_sequence=self._last_sequence,
                    emitted_at=monotonic(),
                    error_type=type(exc).__name__,
                    message=str(exc),
                )
            )
        finally:
            self._stop_event.set()
            self.coordinator.stop(wait=True, timeout=8.0)
            self._invoke_lifecycle("stop")
            try:
                self._backend.close()
            except Exception as exc:
                self._publish_error(
                    LiveRuntimeError(
                        generation=token.value,
                        source_sequence=self._last_sequence,
                        emitted_at=monotonic(),
                        error_type=type(exc).__name__,
                        message=str(exc),
                    )
                )

    def _selected_monitor(self) -> MonitorInfo:
        monitors = self._backend.list_monitors()
        for monitor in monitors:
            if monitor.index == self._config.monitor_index:
                return monitor
        raise InvalidMonitorError(f"monitor {self._config.monitor_index} is not available")

    def _route_packet(self, extracted: ExtractedRois) -> RoiPacket:
        """Reclassify changed headers independently of the previous content layout.

        After leaving enhancement, its old text crops can contain animated artwork
        forever. A probe still goes through the normal stability gate and single OCR
        worker, but never waits for those obsolete content crops to settle.
        Only hashes are retained; full frames have already been released.
        """
        header = perceptual_hash(preprocess_for_hash(extracted.probe.image(PAGE_HEADER_ROI.name)))
        with self._lock:
            known = self._layout_header_hash
            changed = known is not None and (known ^ header).bit_count() > 4
            timestamp = extracted.probe.monotonic_timestamp_ns / 1_000_000_000
            selection_content = (
                self._selection_only
                and extracted.content is not None
                and extracted.content.page_type == "echo_selection"
            )
            if selection_content:
                # Content OCR reclassifies the header before parsing. Header
                # animation must not route a confirmed list back into a probe-
                # only loop, nor may a two-second watchdog reset recovery.
                self._content_transition_since = None
            elif (
                extracted.content is not None
                and self.coordinator.status is LiveStatus.TRANSITIONING
            ):
                if self._content_transition_since is None:
                    self._content_transition_since = timestamp
                elif timestamp - self._content_transition_since >= 2.0:
                    # Header hashes can collide when leaving/returning to the
                    # list. Never wait indefinitely for obsolete animated crops.
                    self._force_probe = True
            else:
                self._content_transition_since = None
            packet = (
                extracted.content
                if selection_content
                else extracted.probe
                if self._force_probe or changed or extracted.content is None
                else extracted.content
            )
            assert packet is not None
            self._submitted_headers[packet.sequence] = header
            # Slow OCR can never make pixel-free recovery bookkeeping unbounded.
            while len(self._submitted_headers) > 256:
                del self._submitted_headers[next(iter(self._submitted_headers))]
            return packet

    def _handle_result(self, result: LiveRecognitionResult) -> None:
        analysis = result.analysis
        next_page = (
            analysis.page_type
            if (
                analysis.request_content
                and analysis.page_type in CONTENT_ROIS_BY_PAGE
                and (not self._selection_only or analysis.page_type == "echo_selection")
            )
            else None
        )
        with self._lock:
            if result.source_sequence <= self._result_floor or self._stop_event.is_set():
                return
            self._content_page_type = next_page
            self._layout_header_hash = self._submitted_headers.get(result.source_sequence)
            self._force_probe = False
            self._content_transition_since = None
            revision = self._routing_revision
        if self._on_result is not None:
            self._on_result(result)
        with self._lock:
            if (
                self._manual_mode
                and self._manual_requested.is_set()
                and revision == self._routing_revision
                and not self._stop_event.is_set()
            ):
                if result.packet_kind is RoiPacketKind.CONTENT and analysis.relevant:
                    self._finish_manual_locked("manual request completed")
                elif next_page is None:
                    self._finish_manual_locked("manual request needs echo selection page")

    def _finish_manual_locked(self, reason: str) -> None:
        """End one request without losing tracker lineage or its accepted result.

        The sequence fence discards pending/in-flight work. A native OCR call
        already running may drain, but cannot publish or commit after timeout.
        Both capture submission and request completion use the runtime lock;
        coordinator callbacks release their own lock before entering this one.
        """
        self._manual_requested.clear()
        self._manual_deadline = None
        self._routing_revision += 1
        cutoff = self.coordinator.request_recheck()
        if cutoff is not None:
            self._result_floor = max(self._result_floor, cutoff)
        self._content_page_type = None
        self._layout_header_hash = None
        self._submitted_headers.clear()
        self._force_probe = True
        self._content_transition_since = None
        self._publish_status(
            LiveStatusUpdate(
                LiveStatus.WAITING_FOR_REQUEST,
                self._token.value if self._token is not None else None,
                monotonic(),
                reason,
                self._last_sequence,
            )
        )

    def _publish_status(self, update: LiveStatusUpdate) -> None:
        if (
            self._manual_mode
            and not self._manual_requested.is_set()
            and update.status
            not in {LiveStatus.WAITING_FOR_REQUEST, LiveStatus.STOPPED, LiveStatus.ERROR}
        ):
            return
        if self._on_status is not None:
            self._on_status(update)

    def _publish_error(self, error: LiveRuntimeError) -> None:
        if self._on_error is not None:
            self._on_error(error)

    def _invoke_lifecycle(self, method_name: str) -> None:
        method = getattr(self._analyzer, method_name, None)
        if callable(method):
            method()


__all__ = ["LiveCaptureRuntime"]
