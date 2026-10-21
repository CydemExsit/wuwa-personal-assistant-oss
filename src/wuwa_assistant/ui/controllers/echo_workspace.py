"""Qt presentation coordinator for live echo recognition and durable history.

The coordinator is the only place where capture callbacks, storage services and
widgets meet.  Capture/OCR remain owned by :class:`LiveCaptureController`; every
database operation runs on one dedicated executor so the Qt event loop never opens
a SQLAlchemy session.  Only immutable, array-free DTOs cross these boundaries.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from threading import RLock
from typing import Any, TypeVar, cast
from zoneinfo import ZoneInfo

from PySide6.QtCore import QObject, Qt, Signal, Slot

from wuwa_assistant.application import (
    EchoDisposition,
    EchoHistory,
    EchoHistoryService,
    EchoSessionSummary,
    IngestReceipt,
    ObservationSnapshot,
    ScoringEvaluation,
    ScoringWorkflowService,
)
from wuwa_assistant.application.observation_review import (
    confirmed_fields,
    observation_review_issues,
    scoring_stat_confidence,
)
from wuwa_assistant.application.replacement_training import (
    ReplacementComparison,
    ReplacementTarget,
    ReplacementTrainingService,
)
from wuwa_assistant.capture import CaptureStatus
from wuwa_assistant.display import DEFAULT_RESOLUTION, RESOLUTION_KEY, RESOLUTIONS
from wuwa_assistant.live.dto import JsonValue
from wuwa_assistant.ui.pages.echo_live import (
    CaptureState,
    ChoiceOption,
    CorrectionRequest,
    EchoLivePage,
    EchoObservationView,
    EchoStatView,
    RecommendationView,
    TimelineEntryView,
)
from wuwa_assistant.ui.pages.echo_live import (
    CaptureStatusView as PageCaptureStatusView,
)
from wuwa_assistant.ui.pages.history import (
    EchoHistoryPage,
    HistoryEventRow,
    HistoryObservationRow,
    HistorySessionDetail,
    HistorySessionRow,
)
from wuwa_assistant.ui.pages.scoring_profiles import ScoringProfilesPage
from wuwa_assistant.ui.pages.settings import MonitorOption, SettingsPage

from .background_executor import BackgroundExecutor
from .live_capture import (
    CaptureControllerError,
    CaptureStatusView,
    ControllerErrorCode,
    LiveCaptureController,
    LiveCaptureSettings,
    MonitorDescriptor,
    RecognitionPayload,
)
from .scoring_profiles import (
    evaluation_to_echo_recommendation,
    evaluation_to_live_substats,
    evaluation_to_profile_preview,
)

_T = TypeVar("_T")
_APPLICATION_LOGGER = logging.getLogger("wuwa_assistant")
_LOG_PAGE_TYPES = frozenset(
    {
        "character_echo_overview",
        "echo_selection",
        "echo_enhancement",
        "enhancement_material_selection",
        "unknown",
    }
)
_LOG_PACKET_KINDS = frozenset({"probe", "content"})


@dataclass(frozen=True, slots=True)
class EchoWorkspaceSettings:
    """Plain settings snapshot used without reading SQLite on the UI thread."""

    sample_hz: float = 5.0
    stable_duration_seconds: float = 0.60
    confidence_threshold: float = 0.85
    minimum_stable_samples: int = 3
    resolution: str = DEFAULT_RESOLUTION

    def for_monitor(self, monitor_index: int, *, manual_mode: bool = False) -> LiveCaptureSettings:
        return LiveCaptureSettings(
            monitor_index=monitor_index,
            sample_hz=self.sample_hz,
            stable_duration_seconds=self.stable_duration_seconds,
            confidence_threshold=self.confidence_threshold,
            minimum_stable_samples=self.minimum_stable_samples,
            manual_mode=manual_mode,
            resolution=self.resolution,
        )


class _Operation(StrEnum):
    RECOVER = "recover"
    START = "start"
    INGEST = "ingest"
    STOP = "stop"
    CORRECT = "correct"
    DISPOSITION = "disposition"
    HISTORY_LIST = "history_list"
    HISTORY_DETAIL = "history_detail"
    SCORE = "score"
    TRAINING_TARGET = "training_target"


@dataclass(frozen=True, slots=True)
class _JobResult:
    operation: _Operation
    value: object | None = None
    context: object | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _Ingested:
    receipt: IngestReceipt
    history: EchoHistory | None
    recognition: RecognitionPayload


@dataclass(frozen=True, slots=True)
class _ScoreJobContext:
    generation: int
    observation_id: str


@dataclass(frozen=True, slots=True)
class _ScoreRequest:
    history: EchoHistory
    persist: bool
    profile_id: str | None = None


@dataclass(frozen=True, slots=True)
class _StopJobContext:
    capture_id: str
    source: str


@dataclass(frozen=True, slots=True)
class _StartJobContext:
    generation: int
    settings: LiveCaptureSettings


@dataclass(frozen=True, slots=True)
class _SessionJobContext:
    session_id: str


class EchoWorkspacePresenter(QObject):
    """Bind the two echo pages to passive capture and durable local storage."""

    _job_completed = Signal(object)
    characters_changed = Signal()
    training_ready = Signal()

    def __init__(
        self,
        *,
        live_controller: LiveCaptureController,
        history_service: EchoHistoryService,
        echo_page: EchoLivePage,
        history_page: EchoHistoryPage,
        settings_page: SettingsPage | None = None,
        scoring_workflow: ScoringWorkflowService | None = None,
        replacement_service: ReplacementTrainingService | None = None,
        scoring_preview_page: ScoringProfilesPage | None = None,
        data_set_id: str | None = None,
        preferred_monitor_id: str | None = None,
        app_version: str,
        settings: EchoWorkspaceSettings | None = None,
        logger: logging.Logger | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._live = live_controller
        self._history = history_service
        self._echo_page = echo_page
        self._history_page = history_page
        self._settings_page = settings_page
        self._scoring = scoring_workflow
        self._replacement = replacement_service
        self._training_target: tuple[str, int] | None = None
        self._training_generation = 0
        self._scoring_preview_page = scoring_preview_page
        self._data_set_id = data_set_id
        self._preferred_monitor_id = preferred_monitor_id or ""
        self._app_version = app_version
        self._settings = settings or EchoWorkspaceSettings()
        self._logger = logger or _APPLICATION_LOGGER
        self._executor = BackgroundExecutor(max_workers=1, thread_name_prefix="wuwa-echo-db")
        self._future_lock = RLock()
        self._futures: set[Future[object]] = set()
        self._monitor_by_id: dict[str, MonitorDescriptor] = {}
        self._session_db_by_local: dict[str, str] = {}
        self._active_capture_id: str | None = None
        self._capture_generation = 0
        self._capture_start_pending = False
        self._capture_stopping = False
        self._active_character_id: str | None = None
        self._current_local_session_id: str | None = None
        self._latest_live_sequence: int | None = None
        self._applied_live_sequence: int | None = None
        self._current_db_session_id: str | None = None
        self._history_navigation_id: str | None = None
        self._current_history: EchoHistory | None = None
        self._current_substats: tuple[EchoStatView, ...] = ()
        self._current_observation: EchoObservationView | None = None
        self._has_completed_analysis = False
        self._score_generation = 0
        self._score_input_key: str | None = None
        self._scored_evaluation: ScoringEvaluation | None = None
        self._score_in_flight: _ScoreJobContext | None = None
        self._pending_score: _ScoreRequest | None = None
        self._history_rows: tuple[EchoSessionSummary, ...] = ()
        self._group_duplicates = True
        self._last_live_error: CaptureControllerError | None = None
        self._closing = False
        self.capture_guard: Callable[[MonitorDescriptor], bool] | None = None
        self._shutdown_complete = False

        self._job_completed.connect(
            self._handle_job_result,
            Qt.ConnectionType.QueuedConnection,
        )
        self._connect_pages()
        self._connect_live_controller()

    def initialize(self) -> None:
        """Recover interrupted rows, enumerate monitors and load local history."""

        if self._closing:
            return
        self._echo_page.apply_capture_status(
            PageCaptureStatusView(CaptureState.INACTIVE, "被動擷取尚未啟用。")
        )
        self._history_page.set_loading(True)
        self._live.refresh_monitors()
        self._submit(
            _Operation.RECOVER,
            lambda: self._history.recover_interrupted_capture_sessions(
                data_set_id=self._data_set_id
            ),
        )
        self.refresh_history()

    def update_settings(self, values: Mapping[str, object]) -> None:
        """Apply SettingsPage values without another database read on Qt's thread."""

        try:
            monitor_id = values.get("capture/monitor_id")
            if isinstance(monitor_id, str):
                self._preferred_monitor_id = monitor_id
            self._settings = EchoWorkspaceSettings(
                sample_hz=_setting_float(
                    values.get("capture/sample_rate_hz"), self._settings.sample_hz
                ),
                stable_duration_seconds=_setting_float(
                    values.get(
                        "capture/stability_wait_seconds",
                        self._settings.stable_duration_seconds,
                    ),
                    self._settings.stable_duration_seconds,
                ),
                confidence_threshold=_setting_float(
                    values.get("ocr/confidence_threshold"),
                    self._settings.confidence_threshold,
                ),
                minimum_stable_samples=self._settings.minimum_stable_samples,
                resolution=str(values.get(RESOLUTION_KEY, self._settings.resolution)),
            )
        except (TypeError, ValueError) as exc:
            self._show_error(f"設定值無效: {exc}")

    def prepare_shutdown(self) -> None:
        """Stop accepting UI work before any worker wait begins."""

        self._closing = True
        self._pending_score = None
        self._executor.prepare_shutdown()

    def shutdown(self, *, timeout: float = 8.0) -> bool:
        """Blocking compatibility entry point for non-window callers."""

        self.prepare_shutdown()
        return self.finish_shutdown(timeout=timeout)

    def finish_shutdown(self, *, timeout: float = 8.0) -> bool:
        """Preserve confirmed records and stop workers before DB disposal."""

        if self._shutdown_complete:
            return True
        live_stopped = self._live.shutdown(timeout=timeout)
        self._executor.shutdown()
        capture_id = self._active_capture_id
        if capture_id is not None:
            try:
                self._history.stop_capture(capture_id, status="stopped")
                self._logger.info(
                    "capture_session_stopped capture_id=%s source=shutdown success=true",
                    capture_id,
                )
            except Exception:
                self._logger.error(
                    "capture_session_stop_failed capture_id=%s source=shutdown",
                    capture_id,
                )
                live_stopped = False
            finally:
                self._active_capture_id = None
        # A start transaction may have completed after ``_closing`` suppressed its
        # Qt callback. Close that row as interrupted instead of leaving it active.
        try:
            self._history.recover_interrupted_capture_sessions(data_set_id=self._data_set_id)
        except Exception:
            live_stopped = False
        self._shutdown_complete = live_stopped
        return live_stopped

    def _connect_pages(self) -> None:
        self._echo_page.training_clear_requested.connect(self.clear_training_target)
        self._echo_page.start_requested.connect(self.start_capture)
        self._echo_page.stop_requested.connect(self.stop_capture)
        self._echo_page.recheck_requested.connect(self.recheck_capture)
        self._echo_page.capture_mode_changed.connect(self.change_capture_mode)
        self._echo_page.correction_requested.connect(self.correct_observation)
        self._echo_page.disposition_requested.connect(self.set_disposition)
        self._echo_page.character_link_requested.connect(self.link_character)
        self._echo_page.session_navigation_requested.connect(self.navigate_session)
        self._history_page.refresh_requested.connect(self.refresh_history)
        self._history_page.grouping_changed.connect(self.change_history_grouping)
        self._history_page.session_selected.connect(self.load_history_detail)

    def _connect_live_controller(self) -> None:
        connection_type = Qt.ConnectionType.QueuedConnection
        self._live.monitors_changed.connect(self._apply_monitors, connection_type)
        self._live.status_changed.connect(self._apply_live_status, connection_type)
        self._live.recognition_ready.connect(self._handle_recognition, connection_type)
        self._live.error_occurred.connect(self._apply_live_error, connection_type)

    @Slot(str, str, str)
    def start_capture(self, monitor_id: str, character_id: str, profile_id: str) -> None:
        from wuwa_assistant.scoring.character_guides import AUTO_PROFILE_ID

        if profile_id == AUTO_PROFILE_ID:
            profile_id = ""
        if (
            self._closing
            or self._active_capture_id is not None
            or self._capture_start_pending
            or self._capture_stopping
        ):
            return
        monitor = self._monitor_by_id.get(monitor_id)
        if monitor is None:
            self._show_error("選擇的顯示器已不存在, 請重新偵測。")
            self._live.refresh_monitors()
            return
        if not monitor.is_compatible:
            self._show_error("支援 1920x1080 / 2560x1440, 請選擇相容的顯示器。")
            return
        if (monitor.width, monitor.height) != RESOLUTIONS.get(self._settings.resolution):
            self._show_error("所選解析度與遊戲螢幕不符, 請到設定調整後重新啟動。")
            return
        try:
            live_settings = self._settings.for_monitor(
                monitor.index, manual_mode=self._echo_page.manual_mode
            )
        except ValueError as exc:
            self._show_error(f"擷取設定無效: {exc}")
            return

        if self.capture_guard is not None and not self.capture_guard(monitor):
            return

        self._last_live_error = None
        self._capture_generation += 1
        self._capture_start_pending = True
        self._logger.info(
            "capture_session_create_requested monitor_index=%d assigned_character=%s "
            "scoring_profile=%s",
            monitor.index,
            bool(character_id),
            bool(profile_id),
        )
        self._active_character_id = character_id or None
        self._echo_page.apply_capture_status(
            PageCaptureStatusView(CaptureState.TRANSITIONING, "正在建立本機 Capture Session…")
        )

        def create_capture() -> object:
            return self._history.start_capture(
                backend_kind="mss-monitor",
                monitor_index=monitor.index,
                sampling_hz=live_settings.sample_hz,
                stable_wait_ms=round(live_settings.stable_duration_seconds * 1000),
                app_version=self._app_version,
                data_set_id=self._data_set_id,
                assigned_character_id=character_id or None,
                scoring_profile_id=profile_id or None,
                roi_config_version=f"{live_settings.resolution}-zh-TW-display-v1",
                metadata={
                    "full_frame_persistence": False,
                    "manual_start": True,
                    "recognition_mode": "manual" if live_settings.manual_mode else "auto",
                },
            )

        self._submit(
            _Operation.START,
            create_capture,
            context=_StartJobContext(self._capture_generation, live_settings),
        )

    @Slot(str)
    def change_capture_mode(self, mode: str) -> None:
        if self._closing or mode not in {"auto", "manual"}:
            return
        # Changing a preference is never permission to begin another capture.
        # Fence even a pending DB-start callback so it cannot arm the old mode.
        if self._active_capture_id is not None or self._capture_start_pending:
            self.stop_capture()

    @Slot()
    def recheck_capture(self) -> None:
        if not self._closing and self._active_capture_id is not None:
            self._live.request_recheck()

    @Slot()
    def stop_capture(self) -> None:
        if self._closing or self._capture_stopping:
            return
        self._capture_generation += 1
        self._capture_start_pending = False
        capture_id = self._active_capture_id
        self._active_capture_id = None
        self._capture_stopping = capture_id is not None
        if self._capture_stopping:
            self._echo_page.apply_capture_status(
                PageCaptureStatusView(CaptureState.STOPPING, "正在停止目前辨識…")
            )
        stopped = self._live.stop()
        if not self._capture_stopping or not stopped:
            self._capture_stopping = False
            self._echo_page.apply_capture_status(PageCaptureStatusView(CaptureState.STOPPED))
        self._logger.info(
            "capture_session_stop_requested active=%s capture_id=%s",
            capture_id is not None,
            capture_id or "none",
        )
        if capture_id is not None:
            self._submit(
                _Operation.STOP,
                lambda: self._history.stop_capture(capture_id, status="stopped"),
                context=_StopJobContext(capture_id, "user"),
            )

    @Slot(object)
    def correct_observation(self, request: object) -> None:
        if not isinstance(request, CorrectionRequest):
            return
        try:
            value = _parse_correction_value(request.field_path, request.corrected_value)
        except ValueError as exc:
            self._show_error(f"修正值無效: {exc}")
            return

        session_id = self._session_db_by_local.get(request.session_id)
        if session_id is None:
            self._show_error("找不到這顆聲骸的本機紀錄, 請重新辨識。")
            return

        def correct() -> object:
            self._history.correct_observation(
                request.observation_id,
                request.field_path,
                value,
                reason="使用者於聲骸強化頁手動修正 OCR",
                mark_reviewed=True,
            )
            return self._history.get_history(session_id)

        self._submit(
            _Operation.CORRECT,
            correct,
            context=_SessionJobContext(session_id),
        )

    @Slot(str, str)
    def set_disposition(self, local_session_id: str, disposition: str) -> None:
        session_id = self._session_db_by_local.get(local_session_id)
        if session_id is None:
            self._show_error("尚未完成 Session 保存, 請稍後再試。")
            return
        try:
            normalized = EchoDisposition(disposition)
        except ValueError:
            self._show_error("未知的聲骸最終結果。")
            return
        character_id = self._active_character_id if normalized is EchoDisposition.EQUIPPED else None

        def save_disposition() -> object:
            self._history.set_disposition(
                session_id,
                normalized,
                character_id=character_id,
            )
            return self._history.get_history(session_id)

        self._submit(
            _Operation.DISPOSITION,
            save_disposition,
            context=_SessionJobContext(session_id),
        )

    @Slot(str, str, int)
    def link_character(
        self,
        local_session_id: str,
        character_id: str,
        slot_index: int,
    ) -> None:
        """Link a recognized echo to an explicitly selected local character."""

        session_id = self._session_db_by_local.get(local_session_id)
        if session_id is None:
            self._show_error("尚未完成 Session 保存, 請稍後再試。")
            return
        if not character_id:
            self._show_error("請先選擇要關聯的角色。")
            return
        if not 1 <= slot_index <= 5:
            self._show_error("請選擇角色的聲骸位置 1-5。")
            return

        def save_link() -> object:
            self._history.set_disposition(
                session_id,
                EchoDisposition.EQUIPPED,
                character_id=character_id,
                slot_index=slot_index,
                replace=True,
            )
            return self._history.get_history(session_id)

        self._submit(
            _Operation.DISPOSITION,
            save_link,
            context=_SessionJobContext(session_id),
        )

    @Slot(bool)
    def change_history_grouping(self, enabled: bool) -> None:
        self._group_duplicates = enabled
        self.refresh_history()

    @Slot()
    def refresh_history(self) -> None:
        if self._closing:
            return
        self._history_page.set_loading(True)
        grouped = self._group_duplicates
        self._submit(
            _Operation.HISTORY_LIST,
            lambda: self._load_history_rows(grouped),
            context=grouped,
        )

    def _load_history_rows(self, grouped: bool) -> tuple[EchoSessionSummary, ...]:
        # One snapshot preserves every scan and folds rescans only once, even
        # when the table spans many service pages. Work stays off the Qt thread.
        return self._history.list_history_snapshot(
            data_set_id=self._data_set_id, group_duplicates=grouped
        )

    @Slot(str)
    def load_history_detail(self, echo_session_id: str) -> None:
        if self._closing or not echo_session_id:
            return
        self._history_navigation_id = echo_session_id
        self._history_page.set_loading(True, "正在讀取 Session 詳細紀錄…")
        self._submit(
            _Operation.HISTORY_DETAIL,
            lambda: self._history.get_history(echo_session_id),
        )

    @Slot(int)
    def navigate_session(self, direction: int) -> None:
        if direction not in {-1, 1} or not self._history_rows:
            return
        ids = [item.id for item in self._history_rows]
        navigation_id = self._history_navigation_id or self._current_db_session_id
        current_id = next(
            (row.id for row in self._history_rows if navigation_id in row.grouped_session_ids),
            navigation_id,
        )
        try:
            current = ids.index(current_id or "")
        except ValueError:
            current = 0
        target = max(0, min(len(ids) - 1, current + direction))
        self.load_history_detail(ids[target])

    @Slot(object)
    def _apply_monitors(self, value: object) -> None:
        if not isinstance(value, tuple) or not all(
            isinstance(item, MonitorDescriptor) for item in value
        ):
            return
        self._monitor_by_id = {item.identifier: item for item in value}
        options = tuple(ChoiceOption(item.identifier, item.label) for item in value)
        self._echo_page.set_monitors(options)
        self._echo_page.select_monitor(self._preferred_monitor_id)
        if self._settings_page is not None:
            self._settings_page.set_monitors(
                tuple(MonitorOption(item.identifier, item.label) for item in value)
            )

    @Slot(object)
    def _apply_live_status(self, value: object) -> None:
        if not isinstance(value, CaptureStatusView):
            return
        if self._capture_stopping:
            if value.status is CaptureStatus.STOPPED:
                self._capture_stopping = False
            elif value.status is not CaptureStatus.STOPPING:
                return
        if (
            self._active_capture_id is None
            and not self._capture_start_pending
            and value.status
            not in {
                CaptureStatus.NOT_ENABLED,
                CaptureStatus.STOPPING,
                CaptureStatus.STOPPED,
                CaptureStatus.ERROR,
            }
        ):
            return
        if self._last_live_error is not None:
            if value.status is CaptureStatus.STOPPED:
                error = self._last_live_error
                self._echo_page.apply_capture_status(
                    PageCaptureStatusView(
                        CaptureState.ERROR,
                        f"{error.title}: {error.message}",
                    )
                )
            elif value.status is CaptureStatus.STOPPING:
                self._echo_page.apply_capture_status(
                    PageCaptureStatusView(
                        CaptureState.STOPPING,
                        "辨識發生錯誤, 正在安全停止被動擷取。",
                    )
                )
            return
        state = _PAGE_STATE_BY_CAPTURE_STATUS[value.status]
        detail = (
            value.reason
            if value.status
            in (
                CaptureStatus.TRANSITIONING,
                CaptureStatus.STABLE,
                CaptureStatus.WAITING_FOR_REQUEST,
            )
            and value.reason
            else _CAPTURE_STATUS_DETAILS[value.status]
        )
        self._echo_page.apply_capture_status(PageCaptureStatusView(state, detail))

    @Slot(object)
    def _apply_live_error(self, value: object) -> None:
        if not isinstance(value, CaptureControllerError):
            return
        first_error = self._last_live_error is None
        self._last_live_error = value
        stop_failed = value.code is ControllerErrorCode.STOP_FAILED
        if stop_failed:
            # A failed join will not necessarily emit another STOPPED event.
            # Release the presenter gate; the controller still prevents Start
            # while native OCR is alive. Do not recursively retry failed stops.
            self._capture_stopping = False
        if first_error and not stop_failed:
            # A runtime error does not inherently stop the passive capture
            # workers. Request their asynchronous stop before enabling Restart;
            # otherwise the next Start is rejected as AlreadyRunning.
            self._live.stop()
        self._echo_page.apply_capture_status(
            PageCaptureStatusView(CaptureState.ERROR, f"{value.title}: {value.message}")
        )
        capture_id = self._active_capture_id
        self._active_capture_id = None
        self._logger.error(
            "capture_session_runtime_error capture_id=%s error_type=%s code=%s",
            capture_id or "none",
            value.error_type,
            value.code.value,
        )
        if capture_id is not None:
            self._submit(
                _Operation.STOP,
                lambda: self._history.stop_capture(
                    capture_id,
                    status="error",
                    # Persist only bounded metadata.  Exception messages may carry
                    # local model paths or OCR text and do not belong in diagnostics.
                    error_summary=f"{value.code.value}: {value.error_type}",
                ),
                context=_StopJobContext(capture_id, "runtime_error"),
            )

    @Slot(object)
    def _handle_recognition(self, value: object) -> None:
        if not isinstance(value, RecognitionPayload):
            return
        if self._active_capture_id is None:
            return
        if not self._live.accepts_recognition(value):
            return
        self._echo_page.apply_page_detection(
            value.page_type,
            value.confidence,
            value.relevant,
        )
        # Probe results exist to explain what the passive capture can currently
        # see.  Only a parsed content packet is a durable echo observation.  This
        # keeps overview/selection detections visible without manufacturing empty
        # sessions or doing needless database work.
        if value.packet_kind != "content" or not value.relevant:
            self._logger.info(
                "recognition_persistence_skipped generation=%d sequence=%d "
                "packet_kind=%s page_type=%s reason=not_parsed_content",
                value.generation,
                value.source_sequence,
                _allowlisted_log_token(value.packet_kind, _LOG_PACKET_KINDS),
                _allowlisted_log_token(value.page_type, _LOG_PAGE_TYPES),
            )
            return
        if value.unchanged:
            # Pixel-level stability can legitimately schedule OCR again after
            # harmless animation changes.  The analyzer has already compared
            # structured fields, so do not create another observation or score
            # for the same semantic state.
            session = value.observation.get("current_session")
            history = self._current_history
            if (
                isinstance(session, dict)
                and session.get("local_session_id") == self._current_local_session_id
                and history is not None
                and history.capture.id == self._active_capture_id
                and history.observations
                and self._latest_live_sequence is not None
                and self._applied_live_sequence == self._latest_live_sequence
            ):
                # A fresh confirmation needs no new DB write or score. A pending
                # newer ingest must not restore an older displayed observation.
                self._echo_page.confirm_current_observation(history.observations[-1].id)
            self._logger.info(
                "recognition_persistence_skipped generation=%d sequence=%d "
                "packet_kind=%s page_type=%s reason=semantic_unchanged",
                value.generation,
                value.source_sequence,
                _allowlisted_log_token(value.packet_kind, _LOG_PACKET_KINDS),
                _allowlisted_log_token(value.page_type, _LOG_PAGE_TYPES),
            )
            return
        if _is_structurally_empty_enhancement(value):
            # During the first frames after opening the enhancement page OCR can
            # occasionally see only one decorative glyph from the echo name.
            # A name fragment without a level, either main-stat row, or any
            # substat is not an actionable observation.  Keeping it out of the
            # database avoids a phantom session and score while preserving every
            # partial result that contains actual enhancement state.
            self._logger.info(
                "recognition_persistence_skipped generation=%d sequence=%d "
                "packet_kind=%s page_type=%s reason=structurally_empty",
                value.generation,
                value.source_sequence,
                _allowlisted_log_token(value.packet_kind, _LOG_PACKET_KINDS),
                _allowlisted_log_token(value.page_type, _LOG_PAGE_TYPES),
            )
            return
        self._persist_recognition(value)

    def _persist_recognition(self, value: RecognitionPayload) -> None:
        capture_id = self._active_capture_id
        if capture_id is None:
            self._logger.info(
                "recognition_persistence_skipped generation=%d sequence=%d "
                "reason=no_active_capture",
                value.generation,
                value.source_sequence,
            )
            return

        self._latest_live_sequence = value.source_sequence
        self._echo_page.mark_analysis_stale()
        self._logger.info(
            "recognition_persistence_requested capture_id=%s generation=%d sequence=%d "
            "packet_kind=%s page_type=%s relevant=%s confidence=%s",
            capture_id,
            value.generation,
            value.source_sequence,
            _allowlisted_log_token(value.packet_kind, _LOG_PACKET_KINDS),
            _allowlisted_log_token(value.page_type, _LOG_PAGE_TYPES),
            value.relevant,
            _optional_log_float(value.confidence),
        )

        observation = dict(value.observation)
        # This is the coordinator's ROI-only state hash.  Persist it for audit
        # and diagnostics, but never treat it as a game-provided echo ID.
        observation["semantic_fingerprint"] = value.state_fingerprint

        def ingest() -> object:
            receipt = self._history.ingest_live_result(
                capture_id,
                cast(Mapping[str, object], observation),
                source_sequence=value.source_sequence,
                confidence_threshold=self._settings.confidence_threshold,
            )
            history = (
                self._history.get_history(receipt.echo_session_id)
                if receipt.echo_session_id is not None
                else None
            )
            return _Ingested(receipt, history, value)

        self._submit(_Operation.INGEST, ingest, context=self._capture_generation)

    def _submit(
        self,
        operation: _Operation,
        function: Callable[[], _T],
        *,
        context: object | None = None,
    ) -> None:
        if self._closing:
            return

        def execute() -> object:
            try:
                return _JobResult(operation, function(), context=context)
            except Exception as exc:
                return _JobResult(
                    operation,
                    context=context,
                    error=f"{type(exc).__name__}: {exc}",
                )

        future = self._executor.submit(
            execute,
            read_only=operation
            in {_Operation.HISTORY_LIST, _Operation.HISTORY_DETAIL, _Operation.TRAINING_TARGET},
        )
        with self._future_lock:
            self._futures.add(future)
        future.add_done_callback(self._job_done)

    def _job_done(self, future: Future[object]) -> None:
        with self._future_lock:
            self._futures.discard(future)
        if self._closing:
            return
        try:
            result = future.result()
        except Exception as exc:  # pragma: no cover - execute catches application errors
            result = _JobResult(_Operation.HISTORY_LIST, error=f"{type(exc).__name__}: {exc}")
        self._job_completed.emit(result)

    @Slot(object)
    def _handle_job_result(self, value: object) -> None:
        if self._closing or not isinstance(value, _JobResult):
            return
        if (
            value.operation is _Operation.TRAINING_TARGET
            and value.context != self._training_generation
        ):
            return
        if value.operation is _Operation.SCORE:
            self._finish_score_job(value)
            return
        if (
            value.operation is _Operation.HISTORY_LIST
            and value.context is not None
            and value.context != self._group_duplicates
        ):
            return
        if value.error is not None:
            if value.operation is _Operation.START and isinstance(value.context, _StartJobContext):
                if value.context.generation != self._capture_generation:
                    return
                self._capture_start_pending = False
            self._handle_storage_error(value)
            return
        if value.operation is _Operation.START:
            self._capture_started(value.value, value.context)
        elif value.operation is _Operation.INGEST and isinstance(value.value, _Ingested):
            if value.context is not None and value.context != self._capture_generation:
                self.refresh_history()
                return
            self._apply_ingested(value.value)
        elif value.operation is _Operation.STOP:
            context = value.context
            self._logger.info(
                "capture_session_stopped capture_id=%s source=%s success=true",
                context.capture_id if isinstance(context, _StopJobContext) else "none",
                context.source if isinstance(context, _StopJobContext) else "unknown",
            )
        elif value.operation is _Operation.CORRECT:
            if isinstance(value.value, EchoHistory):
                self._apply_history(
                    value.value,
                    update_live=self._job_targets_current_session(value),
                    evaluate_score=True,
                    persist_score=True,
                )
            self.refresh_history()
            self.characters_changed.emit()
        elif value.operation is _Operation.DISPOSITION:
            if isinstance(value.value, EchoHistory):
                self._apply_history(
                    value.value,
                    update_live=self._job_targets_current_session(value),
                    evaluate_score=False,
                    persist_score=False,
                )
            self.refresh_history()
            self.characters_changed.emit()
        elif value.operation is _Operation.HISTORY_LIST:
            if value.context is None or value.context == self._group_duplicates:
                self._apply_history_list(value.value)
        elif value.operation is _Operation.HISTORY_DETAIL and isinstance(value.value, EchoHistory):
            self._apply_history(
                value.value,
                update_live=False,
                evaluate_score=False,
                persist_score=False,
            )
        elif value.operation is _Operation.TRAINING_TARGET and isinstance(
            value.value, ReplacementTarget
        ):
            target = value.value
            self._training_target = (target.character_id, target.slot_index)
            self._echo_page.set_training_target(target.description)
            self.training_ready.emit()
            if self._current_history is not None:
                self._schedule_score(self._current_history, persist=False)

    def _finish_score_job(self, result: _JobResult) -> None:
        context = result.context
        if not isinstance(context, _ScoreJobContext) or context != self._score_in_flight:
            return
        self._score_in_flight = None
        if context.generation == self._score_generation:
            if result.error is not None:
                self._scored_evaluation = None
                self._handle_storage_error(result)
            elif isinstance(result.value, ScoringEvaluation):
                self._apply_scoring(result.value, context)
            elif isinstance(result.value, ReplacementComparison):
                comparison = result.value
                if comparison.evaluation is not None:
                    recommendation = evaluation_to_echo_recommendation(
                        comparison.evaluation,
                        target_score=comparison.target.piece.score,
                        score_delta=comparison.delta,
                    )
                    if not comparison.target_comparable:
                        recommendation = replace(
                            recommendation,
                            target_score=None,
                            target_probability=None,
                            score_delta=None,
                        )
                else:
                    self._scored_evaluation = None
                    recommendation = RecommendationView()
                self._echo_page.set_training_target(
                    comparison.target.description, is_same_echo=comparison.is_same_echo
                )
                reason = comparison.reason
                if comparison.delta is not None:
                    reason += f" 目前分差 {comparison.delta:+.2f} 分。"
                recommendation = replace(
                    recommendation,
                    conclusion=comparison.conclusion,
                    save_advice=comparison.conclusion,
                    upgrade_advice=(
                        comparison.conclusion
                        if comparison.conclusion
                        in {
                            "資料待補",
                            "戰鬥資料待補",
                            "配裝資料待補",
                            "COST 不同",
                            "先檢查配裝",
                        }
                        else recommendation.upgrade_advice or comparison.conclusion
                    ),
                    reason=reason,
                    is_same_echo=comparison.is_same_echo,
                    combat_summary=""
                    if comparison.combat is None
                    else comparison.combat.after.summary,
                )
                if comparison.evaluation is not None:
                    self._apply_scoring(comparison.evaluation, context, recommendation)
                else:
                    self._publish_analysis(recommendation)
        # OCR may outpace scoring. Keep one latest refresh rather than restarting
        # equivalent work or accumulating an unbounded queue of evaluations.
        pending, self._pending_score = self._pending_score, None
        if pending is not None:
            self._schedule_score(
                pending.history, persist=pending.persist, profile_id=pending.profile_id
            )

    @Slot(str, int)
    def set_training_target(self, character_id: str, slot_index: int) -> None:
        service, data_set_id = self._replacement, self._data_set_id
        if service is None or data_set_id is None or self._closing:
            return
        self._training_generation += 1
        self._invalidate_score()
        self._training_target = None
        self._echo_page.set_training_target("正在讀取角色目前的配裝...")
        self._submit(
            _Operation.TRAINING_TARGET,
            lambda: service.target(data_set_id, character_id, slot_index),
            context=self._training_generation,
        )

    @Slot()
    def clear_training_target(self) -> None:
        self._training_generation += 1
        self._invalidate_score()
        self._training_target = None
        self._echo_page.set_training_target()
        if self._current_history is not None:
            self._schedule_score(self._current_history, persist=False)

    def _job_targets_current_session(self, result: _JobResult) -> bool:
        context = result.context
        return (
            isinstance(context, _SessionJobContext)
            and context.session_id == self._current_db_session_id
        )

    def _capture_started(self, value: object | None, context: object | None) -> None:
        from wuwa_assistant.application import CaptureSessionSnapshot

        if not isinstance(value, CaptureSessionSnapshot) or not isinstance(
            context, _StartJobContext
        ):
            self._show_error("無法建立 Capture Session。")
            return
        if context.generation != self._capture_generation:
            self._submit(
                _Operation.STOP,
                lambda: self._history.stop_capture(value.id, status="stopped"),
                context=_StopJobContext(value.id, "cancelled_start"),
            )
            return
        self._capture_start_pending = False
        settings = context.settings
        self._active_capture_id = value.id
        # Tracker-local IDs intentionally restart for each capture.  Scope this
        # lookup to the new run so an old ``echo-session-0001`` can never receive
        # a disposition meant for the current run.
        self._session_db_by_local.clear()
        self._current_local_session_id = None
        self._latest_live_sequence = None
        self._applied_live_sequence = None
        self._current_db_session_id = None
        self._current_history = None
        self._current_substats = ()
        self._current_observation = None
        self._has_completed_analysis = False
        self._invalidate_score()
        self._echo_page.begin_capture_run(_capture_run_heading(value.started_at))
        self._echo_page.apply_recommendation(RecommendationView())
        self._echo_page.select_result_character(self._active_character_id or "")
        self._logger.info(
            "capture_session_created capture_id=%s monitor_index=%d",
            value.id,
            settings.monitor_index,
        )
        if not self._live.start(settings):
            capture_id = value.id
            self._active_capture_id = None
            self._submit(
                _Operation.STOP,
                lambda: self._history.stop_capture(
                    capture_id,
                    status="error",
                    error_summary="live controller rejected start",
                ),
                context=_StopJobContext(capture_id, "start_rejected"),
            )

    def _apply_ingested(self, ingested: _Ingested) -> None:
        receipt = ingested.receipt
        if (
            receipt.created_character_id is not None
            or receipt.equipment_changed
            or receipt.panel_inputs_changed
            or receipt.equipment_check_changed
            or receipt.event_ids
        ):
            self.characters_changed.emit()
        self._logger.info(
            "recognition_persisted capture_id=%s sequence=%d duplicate=%s "
            "session_created=%s observation_created=%s event_count=%d",
            receipt.capture_session_id,
            ingested.recognition.source_sequence,
            receipt.duplicate,
            receipt.echo_session_id is not None,
            receipt.observation_id is not None,
            len(receipt.event_ids),
        )
        if ingested.history is None:
            return
        self._applied_live_sequence = ingested.recognition.source_sequence
        self._apply_history(
            ingested.history,
            update_live=True,
            evaluate_score=True,
            persist_score=ingested.receipt.observation_id is not None,
        )
        self.refresh_history()

    def _apply_history_list(self, value: object | None) -> None:
        if not isinstance(value, tuple) or not all(
            isinstance(item, EchoSessionSummary) for item in value
        ):
            self._history_page.show_error("資料格式不正確。")
            return
        self._history_rows = value
        run_labels = _capture_run_labels(value)
        self._history_page.set_sessions(
            tuple(_history_row(item, run_labels[item.capture_session_id]) for item in value)
        )

    def _apply_history(
        self,
        history: EchoHistory,
        *,
        update_live: bool,
        evaluate_score: bool,
        persist_score: bool,
    ) -> None:
        summary = history.session
        self._history_page.set_detail(_history_detail(history))
        if not update_live or not history.observations:
            return
        self._current_history = history
        self._session_db_by_local[summary.local_session_id] = summary.id
        self._current_local_session_id = summary.local_session_id
        self._current_db_session_id = summary.id
        self._history_navigation_id = None
        latest = history.observations[-1]
        observation_view = _observation_view(
            summary.local_session_id,
            latest.id,
            latest.effective_payload,
            latest.requires_review,
            equipped_fallback=_last_known_equipped(history.observations),
            owner_fallback=_last_known_equipped_owner(history.observations),
            set_fallback=_last_known_set(history.observations),
            assigned_character_id=summary.character_id,
            assigned_slot_index=summary.slot_index,
            minimum_confidence=self._scoring.minimum_confidence if self._scoring else 0.85,
        )
        self._current_substats = observation_view.sub_stats
        self._current_observation = observation_view
        scoring_changed = self._select_scoring_inputs(history)
        if self._scored_evaluation is not None:
            observation_view = replace(
                observation_view,
                sub_stats=evaluation_to_live_substats(
                    self._scored_evaluation, observation_view.sub_stats
                ),
            )
        if self._scored_evaluation is not None or not self._has_completed_analysis:
            self._echo_page.apply_observation(observation_view)
            self._apply_current_history_details()
            if self._scored_evaluation is None and self._scoring is not None:
                self._echo_page.set_analysis_pending(True, retain_previous=False)
        if not evaluate_score and not scoring_changed:
            return
        self._schedule_score(history, persist=persist_score)

    def _invalidate_score(self) -> None:
        self._score_generation += 1
        self._score_input_key = None
        self._scored_evaluation = None
        self._pending_score = None
        if self._current_history is not None:
            self._echo_page.set_analysis_pending(True, retain_previous=self._has_completed_analysis)

    def _apply_current_history_details(self) -> None:
        history = self._current_history
        if history is None:
            return
        self._echo_page.apply_disposition(history.session.final_status)
        revisit_notice = _revisit_notice(history)
        if revisit_notice is not None:
            self._echo_page.apply_revisit_notice(revisit_notice)
        self._echo_page.set_timeline(_timeline_entries(history))

    def _publish_analysis(
        self, recommendation: RecommendationView, evaluation: ScoringEvaluation | None = None
    ) -> None:
        observation = self._current_observation
        if observation is not None:
            if evaluation is not None:
                observation = replace(
                    observation,
                    sub_stats=evaluation_to_live_substats(evaluation, observation.sub_stats),
                )
            self._echo_page.apply_analysis_result(
                observation,
                recommendation,
                current=self._applied_live_sequence == self._latest_live_sequence,
            )
            self._apply_current_history_details()
            self._has_completed_analysis = True
        else:
            self._echo_page.apply_recommendation(recommendation)
            self._echo_page.set_analysis_pending(False)

    def _select_scoring_inputs(self, history: EchoHistory, profile_id: str | None = None) -> bool:
        """Separate scoring changes from a new OCR ID, timestamp or confidence."""

        scoring = self._scoring
        if scoring is None or not history.observations:
            return False
        minimum_confidence = scoring.minimum_confidence
        if self._training_target is not None and self._replacement is not None:
            minimum_confidence = self._replacement.minimum_confidence
        key = _scoring_input_key(
            history,
            profile_id=profile_id,
            target=self._training_target,
            minimum_confidence=minimum_confidence,
        )
        if key == self._score_input_key:
            return False
        self._invalidate_score()
        self._score_input_key = key
        return True

    @Slot()
    def refresh_scoring_context(self) -> None:
        """Invalidate scores after explicit character/profile/equipment edits."""

        if self._closing:
            return
        self._invalidate_score()
        if self._current_history is not None:
            self._schedule_score(self._current_history, persist=False)

    def _schedule_score(
        self,
        history: EchoHistory,
        *,
        persist: bool,
        profile_id: str | None = None,
    ) -> None:
        scoring = self._scoring
        if scoring is None or not history.observations:
            self._publish_analysis(
                RecommendationView(
                    conclusion="等待評分設定檔",
                    reason="目前尚未啟用評分服務。",
                )
            )
            return
        latest = history.observations[-1]
        self._select_scoring_inputs(history, profile_id)
        if self._score_in_flight is not None:
            pending = self._pending_score
            if pending is not None and pending.history.observations[-1].id == latest.id:
                persist = persist or pending.persist
            self._pending_score = _ScoreRequest(history, persist, profile_id)
            return
        score_context = _ScoreJobContext(self._score_generation, latest.id)
        self._score_in_flight = score_context
        target, replacement = self._training_target, self._replacement
        if target is not None and replacement is not None:
            candidate_payload = dict(latest.effective_payload)
            if not candidate_payload.get("set_name"):
                candidate_payload["set_name"] = _last_known_set(history.observations)
            self._submit(
                _Operation.SCORE,
                lambda: replacement.evaluate(
                    candidate_payload,
                    data_set_id=history.capture.data_set_id,
                    character_id=target[0],
                    slot_index=target[1],
                    requires_review=latest.requires_review,
                    candidate_echo_id=history.session.echo_id,
                ),
                context=score_context,
            )
            return

        def evaluate_current() -> ScoringEvaluation | ReplacementComparison:
            evaluation = scoring.evaluate(
                latest.effective_payload,
                data_set_id=history.capture.data_set_id,
                profile_id=profile_id or history.capture.scoring_profile_id,
                echo_session_id=history.session.id,
                observation_id=latest.id,
                requires_review=latest.requires_review,
                persist=persist,
                character_mode=not (profile_id or history.capture.scoring_profile_id),
            )
            if replacement is not None and not (profile_id or history.capture.scoring_profile_id):
                equipped = replacement.equipped_target(
                    history.capture.data_set_id, history.session.echo_id
                )
                if equipped is not None:
                    value = replacement.evaluate(
                        latest.effective_payload,
                        data_set_id=history.capture.data_set_id,
                        character_id=equipped[0],
                        slot_index=equipped[1],
                        requires_review=latest.requires_review,
                        candidate_echo_id=history.session.echo_id,
                    )
                    if value.evaluation is not None:
                        value = replace(
                            value,
                            evaluation=replace(
                                value.evaluation,
                                persisted_decision_id=evaluation.persisted_decision_id,
                            ),
                        )
                    return value
            return evaluation

        self._submit(_Operation.SCORE, evaluate_current, context=score_context)

    def _apply_scoring(
        self,
        evaluation: ScoringEvaluation,
        context: object | None,
        recommendation: RecommendationView | None = None,
    ) -> None:
        history = self._current_history
        if (
            not isinstance(context, _ScoreJobContext)
            or context.generation != self._score_generation
            or history is None
            or not history.observations
        ):
            return
        self._scored_evaluation = evaluation
        self._publish_analysis(
            recommendation or evaluation_to_echo_recommendation(evaluation), evaluation
        )
        if self._scoring_preview_page is not None:
            self._scoring_preview_page.apply_score_preview(
                evaluation_to_profile_preview(evaluation)
            )
        if evaluation.persisted_decision_id is not None:
            self.refresh_history()

    @Slot(str)
    def rescore_current(self, profile_id: str) -> None:
        history = self._current_history
        if history is None or not profile_id:
            return
        capture_profile = history.capture.scoring_profile_id
        if capture_profile is not None and capture_profile != profile_id:
            return
        self._invalidate_score()
        self._schedule_score(
            history,
            persist=history.capture.id == self._active_capture_id,
            # Editing an unrelated profile must not switch an automatic role
            # evaluation into an explicit preview of that edited profile.
            profile_id=capture_profile,
        )

    def _handle_storage_error(self, result: _JobResult) -> None:
        message = result.error or "未知資料庫錯誤"
        self._logger.error("capture_storage_error operation=%s", result.operation.value)
        if result.operation is _Operation.START:
            self._echo_page.apply_capture_status(
                PageCaptureStatusView(CaptureState.ERROR, f"無法建立 Session: {message}")
            )
        elif result.operation in {_Operation.HISTORY_LIST, _Operation.HISTORY_DETAIL}:
            self._history_page.show_error(message)
        elif result.operation is _Operation.SCORE:
            missing_profile = message.startswith("尚無已查核的角色評分設定")
            missing_owner = message.startswith("尚未確認聲骸所屬角色")
            self._publish_analysis(
                RecommendationView(
                    conclusion=(
                        "此角色尚未設定評分需求"
                        if missing_profile
                        else "請指定角色或評分設定檔"
                        if missing_owner
                        else "資料不足, 需要人工確認"
                    ),
                    reason=f"評分失敗: {message}",
                    scoring_basis=(
                        "請於角色頁指定評分設定檔" if missing_profile else "等待評分設定"
                    ),
                    scoring_profile_name="尚未設定"
                    if missing_profile or missing_owner
                    else "未指定評分設定檔",
                )
            )
            if self._scoring_preview_page is not None:
                self._scoring_preview_page.show_error(message)
        elif result.operation is _Operation.TRAINING_TARGET:
            self._echo_page.set_training_target(f"無法讀取替換目標: {message}")
        else:
            self._show_error(message)

    def _show_error(self, message: str) -> None:
        self._echo_page.apply_capture_status(PageCaptureStatusView(CaptureState.ERROR, message))


_PAGE_STATE_BY_CAPTURE_STATUS: Mapping[CaptureStatus, CaptureState] = {
    CaptureStatus.NOT_ENABLED: CaptureState.INACTIVE,
    CaptureStatus.STARTING: CaptureState.TRANSITIONING,
    CaptureStatus.WAITING_FOR_ECHO_PAGE: CaptureState.WAITING_FOR_PAGE,
    CaptureStatus.WAITING_FOR_REQUEST: CaptureState.WAITING_FOR_REQUEST,
    CaptureStatus.TRANSITIONING: CaptureState.TRANSITIONING,
    CaptureStatus.RECOGNIZING: CaptureState.RECOGNIZING,
    CaptureStatus.STABLE: CaptureState.STABLE,
    CaptureStatus.LOW_CONFIDENCE: CaptureState.LOW_CONFIDENCE,
    CaptureStatus.STOPPING: CaptureState.TRANSITIONING,
    CaptureStatus.STOPPED: CaptureState.STOPPED,
    CaptureStatus.ERROR: CaptureState.ERROR,
}

_CAPTURE_STATUS_DETAILS: Mapping[CaptureStatus, str] = {
    CaptureStatus.NOT_ENABLED: "被動擷取尚未啟用。",
    CaptureStatus.STARTING: "正在啟動本機離線辨識…",
    CaptureStatus.WAITING_FOR_ECHO_PAGE: ("正在讀取所選顯示器; 尚未看到可分析的聲骸頁面。"),
    CaptureStatus.WAITING_FOR_REQUEST: "目前未擷取畫面; 換好聲骸後, 按「辨識目前畫面」再讀取。",
    CaptureStatus.TRANSITIONING: "偵測到畫面變化, 等待文字區域穩定…",
    CaptureStatus.RECOGNIZING: "畫面已穩定, 正在本機離線辨識…",
    CaptureStatus.STABLE: "聲骸資料已更新。",
    CaptureStatus.LOW_CONFIDENCE: "部分欄位信心不足, 請人工確認。",
    CaptureStatus.STOPPING: "正在停止辨識並保存結構化紀錄…",
    CaptureStatus.STOPPED: "辨識已停止; 已確認的紀錄仍保存在本機。",
    CaptureStatus.ERROR: "辨識發生錯誤, 請查看提示或診斷紀錄。",
}

_EVENT_LABELS: Mapping[str, str] = {
    "page_changed": "頁面切換",
    "echo_selected": "選取聲骸",
    "echo_session_started": "Session 開始",
    "echo_upgraded": "聲骸強化",
    "echo_marked_discarded": "標記棄置",
    "echo_used_as_material": "投入強化素材",
    "echo_session_ended": "Session 結束",
    "new_echo_session_started": "新 Session 開始",
}

_DISPOSITION_LABELS: Mapping[str, str] = {
    "undecided": "尚未決定",
    "kept": "保留",
    "discarded": "淘汰",
    "equipped": "已裝備",
}


def _scoring_input_key(
    history: EchoHistory,
    *,
    profile_id: str | None,
    target: tuple[str, int] | None,
    minimum_confidence: float,
) -> str:
    latest = history.observations[-1]
    payload = latest.effective_payload
    fields = {
        field: payload.get(field)
        for field in (
            "name",
            "cost",
            "level",
            "locked_substat_slots",
            "is_equipped",
            "equipped_character_name",
            "equipped_slot_index",
        )
    }
    fields["set_name"] = payload.get("set_name") or _last_known_set(history.observations)
    fields["manual_confirmed_fields"] = sorted(confirmed_fields(payload))
    for collection in ("main_stats", "sub_stats"):
        raw_stats = payload.get(collection)
        stats: list[object] = []
        for index, raw in enumerate(raw_stats if isinstance(raw_stats, list) else []):
            if not isinstance(raw, Mapping):
                stats.append(None)
                continue
            stat = {
                field: raw.get(field)
                for field in ("name", "value", "unit", "stat_kind", "slot_index")
            }
            confidence = scoring_stat_confidence(
                raw, f"/{collection}/{index}", confirmed_fields(payload)
            )
            # Exact OCR probabilities are still displayed and persisted. The
            # score only depends on whether every required confidence passes.
            stat["confidence_accepted"] = (
                confidence is not None and confidence >= minimum_confidence
            )
            stats.append(stat)
        fields[collection] = stats
    return json.dumps(
        (
            history.capture.data_set_id,
            history.capture.id,
            history.session.id,
            history.session.echo_id,
            history.capture.assigned_character_id,
            history.session.character_id,
            history.session.slot_index,
            profile_id or history.capture.scoring_profile_id,
            target,
            latest.requires_review,
            fields,
        ),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _observation_view(
    local_session_id: str,
    observation_id: str,
    payload: Mapping[str, Any],
    requires_review: bool,
    *,
    equipped_fallback: bool | None = None,
    owner_fallback: str | None = None,
    set_fallback: str | None = None,
    assigned_character_id: str | None = None,
    assigned_slot_index: int | None = None,
    minimum_confidence: float = 0.85,
) -> EchoObservationView:
    confidence = _overall_confidence(payload.get("confidence"))
    issues = observation_review_issues(
        payload, minimum_confidence=minimum_confidence, requires_review=requires_review
    )
    current_equipped = _optional_bool(payload.get("is_equipped"))
    return EchoObservationView(
        session_id=local_session_id,
        observation_id=observation_id,
        name=_optional_str(payload.get("name")),
        set_name=_optional_str(payload.get("set_name")) or set_fallback,
        cost=_optional_int(payload.get("cost")),
        level=_optional_int(payload.get("level")),
        main_stats=_stat_views(payload.get("main_stats")),
        sub_stats=_stat_views(payload.get("sub_stats")),
        locked_substat_slots=_optional_int(payload.get("locked_substat_slots")),
        confidence=confidence,
        needs_confirmation=requires_review or bool(issues),
        review_issues=issues,
        is_equipped=current_equipped if current_equipped is not None else equipped_fallback,
        equipped_slot_index=_optional_int(payload.get("equipped_slot_index")),
        equipped_character_name=(
            _optional_str(payload.get("equipped_character_name"))
            if current_equipped is True
            else owner_fallback
            if current_equipped is None
            else None
        ),
        assigned_character_id=assigned_character_id,
        assigned_slot_index=assigned_slot_index,
    )


def _last_known_equipped(observations: tuple[ObservationSnapshot, ...]) -> bool | None:
    """Keep the latest explicit selection-page badge evidence for this session."""

    for observation in reversed(observations):
        equipped = _optional_bool(observation.effective_payload.get("is_equipped"))
        if equipped is not None:
            return equipped
    return None


def _last_known_set(observations: tuple[ObservationSnapshot, ...]) -> str | None:
    for observation in reversed(observations):
        name = _optional_str(observation.effective_payload.get("set_name"))
        if name is not None:
            return name
    return None


def _last_known_equipped_owner(observations: tuple[ObservationSnapshot, ...]) -> str | None:
    for observation in reversed(observations):
        payload = observation.effective_payload
        equipped = _optional_bool(payload.get("is_equipped"))
        if equipped is not None:
            return _optional_str(payload.get("equipped_character_name")) if equipped else None
    return None


def _stat_views(value: object) -> tuple[EchoStatView, ...]:
    if not isinstance(value, list):
        return ()
    views: list[EchoStatView] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        raw_value = item.get("value")
        numeric = (
            float(raw_value)
            if isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool)
            else None
        )
        if numeric is not None and numeric.is_integer():
            numeric = int(numeric)
        views.append(
            EchoStatView(
                name=_optional_str(item.get("name")),
                value=numeric,
                unit=_optional_str(item.get("unit")),
                confidence=_optional_float(item.get("confidence")),
            )
        )
    return tuple(views)


def _overall_confidence(value: object) -> float | None:
    if not isinstance(value, Mapping):
        return None
    confidences = [
        float(item)
        for item in value.values()
        if isinstance(item, (int, float)) and not isinstance(item, bool)
    ]
    return min(confidences) if confidences else None


def _timeline_entries(history: EchoHistory) -> tuple[TimelineEntryView, ...]:
    return tuple(
        TimelineEntryView(
            timestamp=event.occurred_at,
            event_label=_EVENT_LABELS.get(event.event_type, event.event_type),
            level=_optional_int(event.payload.get("current_level")),
            summary=_render_event_notes(event.notes) or _event_summary(event.payload),
            confidence=event.confidence,
        )
        for event in history.events
    )


def _revisit_notice(history: EchoHistory) -> str | None:
    if not history.observations:
        return None
    latest = history.observations[-1]
    status = _optional_str(latest.effective_payload.get("revisit_status"))
    if status == "possible":
        return status
    for event in reversed(history.events):
        if event.occurred_at < latest.observed_at:
            break
        if "revisited=true" in event.notes:
            return "reused"
    return None


def _render_event_notes(notes: tuple[str, ...]) -> str:
    if "revisited=true" in notes:
        return "回到本次先前辨識的同一顆聲骸"
    if "possible_revisit=true" in notes:
        return "畫面與先前紀錄相似, 但證據不足; 已分成新紀錄"
    return "; ".join(notes)


def _capture_run_heading(started_at: str) -> str:
    try:
        parsed = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        readable = parsed.astimezone(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        readable = started_at.replace("T", " ")[:16]
    return f"本次辨識 · {readable} 開始"


def _capture_run_labels(
    rows: tuple[EchoSessionSummary, ...],
) -> dict[str, str]:
    """Give all echoes from the same capture a shared, human-facing batch label."""

    ordered_capture_ids: list[str] = []
    for summary in reversed(rows):
        if summary.capture_session_id not in ordered_capture_ids:
            ordered_capture_ids.append(summary.capture_session_id)
    return {
        capture_id: f"第 {index} 次辨識"
        for index, capture_id in enumerate(ordered_capture_ids, start=1)
    }


def _history_row(summary: EchoSessionSummary, capture_run: str = "未分組") -> HistorySessionRow:
    return HistorySessionRow(
        session_id=summary.id,
        started_at=summary.started_at,
        ended_at=summary.ended_at,
        echo_name=summary.name,
        final_level=summary.level,
        disposition=_DISPOSITION_LABELS.get(summary.final_status, summary.final_status),
        final_score=summary.score,
        character_name=summary.character_id,
        capture_run=capture_run,
        scan_count=summary.rescan_count + 1,
    )


def _history_detail(history: EchoHistory) -> HistorySessionDetail:
    corrected_ids = {item.observation_id for item in history.corrections}
    return HistorySessionDetail(
        session_id=history.session.id,
        echo_name=history.session.name,
        set_name=_last_known_set(history.observations),
        final_level=history.session.level,
        final_disposition=_DISPOSITION_LABELS.get(
            history.session.final_status, history.session.final_status
        ),
        assigned_character=history.session.character_id,
        observations=tuple(
            HistoryObservationRow(
                captured_at=item.observed_at,
                level=_optional_int(item.effective_payload.get("level")),
                summary=_observation_summary(item.effective_payload),
                confidence=_overall_confidence(item.effective_payload.get("confidence")),
                corrected=item.id in corrected_ids,
            )
            for item in history.observations
        ),
        events=tuple(
            HistoryEventRow(
                occurred_at=item.occurred_at,
                event_label=_EVENT_LABELS.get(item.event_type, item.event_type),
                notes="; ".join(item.notes),
            )
            for item in history.events
        ),
    )


def _observation_summary(payload: Mapping[str, Any]) -> str:
    parts: list[str] = []
    level = _optional_int(payload.get("level"))
    if level is not None:
        parts.append(f"+{level}")
    name = _optional_str(payload.get("name"))
    if name:
        parts.append(name)
    substats = _stat_views(payload.get("sub_stats"))
    if substats:
        parts.append("、".join(stat.name or "未知詞條" for stat in substats))
    return " · ".join(parts) or "需要人工確認"


def _event_summary(payload: Mapping[str, Any]) -> str:
    previous = _optional_int(payload.get("previous_level"))
    current = _optional_int(payload.get("current_level"))
    if previous is not None or current is not None:
        previous_text = previous if previous is not None else "—"
        current_text = current if current is not None else "—"
        return f"{previous_text} → {current_text}"
    return "已保存結構化事件"


def _parse_correction_value(field_path: str, value: str) -> JsonValue:
    normalized = value.strip()
    if not normalized:
        raise ValueError("修正值不可空白")
    if field_path in {"/level", "/cost", "/locked_substat_slots"}:
        try:
            return int(normalized)
        except ValueError as exc:
            raise ValueError("此欄位需要整數") from exc
    if field_path.endswith("/value"):
        try:
            parsed = float(normalized.rstrip("%"))
        except ValueError as exc:
            raise ValueError("詞條數值需要數字") from exc
        return int(parsed) if parsed.is_integer() else parsed
    return normalized


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if 0.0 <= parsed <= 1.0 else None


def _is_structurally_empty_enhancement(value: RecognitionPayload) -> bool:
    """Return whether an enhancement result has no actionable state fields."""

    if value.page_type != "echo_enhancement":
        return False
    observation = value.observation
    if observation.get("level") is not None:
        return False
    return not (
        _stat_payload_has_content(observation.get("main_stats"))
        or _stat_payload_has_content(observation.get("sub_stats"))
    )


def _stat_payload_has_content(value: object) -> bool:
    if not isinstance(value, (list, tuple)):
        return False
    for item in value:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        if isinstance(name, str) and name.strip():
            return True
        stat_value = item.get("value")
        if stat_value is not None and not isinstance(stat_value, bool):
            return True
    return False


def _optional_log_float(value: float | None) -> str:
    return "none" if value is None else f"{value:.5f}"


def _allowlisted_log_token(value: str, allowed: frozenset[str]) -> str:
    return value if value in allowed else "unrecognized"


def _setting_float(value: object, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return default
    return float(value)


__all__ = ["EchoWorkspacePresenter", "EchoWorkspaceSettings"]
