"""Traditional-Chinese live echo workspace with array-free view contracts.

The page is deliberately a passive view.  It emits user intent as Qt signals and
accepts immutable presentation DTOs through slots.  A controller may connect its
own Qt signals to these slots (which gives queued delivery across threads), but the
page never invokes capture, OCR, or database code itself.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from wuwa_assistant.application.observation_review import FieldReviewIssue
from wuwa_assistant.models.names import character_name_key
from wuwa_assistant.ui.panel_model import EchoPanelSnapshot
from wuwa_assistant.ui.score_colors import apply_score_color, score_span
from wuwa_assistant.ui.widgets.choice_selector import ChoiceSelector as QComboBox


@dataclass(frozen=True, slots=True)
class ChoiceOption:
    """Identifier/label pair for controller-provided choices."""

    identifier: str
    label: str

    def __post_init__(self) -> None:
        if not self.identifier.strip() or not self.label.strip():
            raise ValueError("choice identifier and label cannot be empty")


class CaptureState(StrEnum):
    INACTIVE = "inactive"
    STARTING = "starting"
    WAITING_FOR_PAGE = "waiting_for_page"
    WAITING_FOR_REQUEST = "waiting_for_request"
    TRANSITIONING = "transitioning"
    RECOGNIZING = "recognizing"
    STABLE = "stable"
    LOW_CONFIDENCE = "low_confidence"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class CaptureStatusView:
    state: CaptureState
    detail: str = ""
    confidence: float | None = None

    def __post_init__(self) -> None:
        _validate_confidence(self.confidence)


@dataclass(frozen=True, slots=True)
class EchoStatView:
    name: str | None
    value: int | float | None
    unit: str | None
    confidence: float | None
    roll_tier: str | None = None
    character_value: str | None = None
    score_contribution: float | None = None

    def __post_init__(self) -> None:
        _validate_confidence(self.confidence)

    @property
    def value_text(self) -> str:
        if self.value is None:
            return "—"
        return f"{_format_number(self.value)}{self.unit or ''}"


@dataclass(frozen=True, slots=True)
class EchoObservationView:
    session_id: str
    observation_id: str
    name: str | None
    set_name: str | None
    cost: int | None
    level: int | None
    main_stats: tuple[EchoStatView, ...] = ()
    sub_stats: tuple[EchoStatView, ...] = ()
    locked_substat_slots: int | None = None
    confidence: float | None = None
    needs_confirmation: bool = False
    is_equipped: bool | None = None
    equipped_slot_index: int | None = None
    equipped_character_name: str | None = None
    assigned_character_id: str | None = None
    assigned_slot_index: int | None = None
    review_issues: tuple[FieldReviewIssue, ...] = ()

    def __post_init__(self) -> None:
        if not self.session_id.strip() or not self.observation_id.strip():
            raise ValueError("session_id and observation_id cannot be empty")
        if self.cost is not None and self.cost not in {1, 3, 4}:
            raise ValueError("cost must be 1, 3, 4, or null")
        if self.level is not None and not 0 <= self.level <= 25:
            raise ValueError("level must be within 0..25 or null")
        if self.locked_substat_slots is not None and not 0 <= self.locked_substat_slots <= 5:
            raise ValueError("locked_substat_slots must be within 0..5 or null")
        if self.equipped_slot_index is not None and not 1 <= self.equipped_slot_index <= 5:
            raise ValueError("equipped_slot_index must be within 1..5 or null")
        _validate_confidence(self.confidence)


@dataclass(frozen=True, slots=True)
class RecommendationView:
    conclusion: str = "等待辨識"
    opened_slot_quality_score: float | None = None
    score: float | None = None
    expected_score: float | None = None
    estimated_score_range: tuple[float, float] | None = None
    pass_probability: float | None = None
    perfect_probability: float | None = None
    exceptional_probability: float | None = None
    reason: str = "尚未取得可評分的聲骸資料。"
    breakdown: tuple[str, ...] = ()
    scoring_basis: str = "通用參考"
    scoring_profile_name: str = "未指定評分設定檔"
    target_score: float | None = None
    target_probability: float | None = None
    score_delta: float | None = None
    is_same_echo: bool = False
    combat_summary: str = ""
    save_advice: str = "等待評分"
    upgrade_advice: str = ""

    def __post_init__(self) -> None:
        if not self.conclusion.strip():
            raise ValueError("conclusion cannot be empty")
        for field_name, value in (
            ("opened_slot_quality_score", self.opened_slot_quality_score),
            ("score", self.score),
            ("expected_score", self.expected_score),
            ("target_score", self.target_score),
        ):
            if value is not None and (not isfinite(value) or not 0 <= value <= 100):
                raise ValueError(f"{field_name} must be within 0..100 or null")
        if self.estimated_score_range is not None:
            minimum, maximum = self.estimated_score_range
            if not (0 <= minimum <= maximum <= 100):
                raise ValueError("estimated score range must be ordered within 0..100")
        for confidence in (
            self.pass_probability,
            self.perfect_probability,
            self.exceptional_probability,
            self.target_probability,
        ):
            _validate_confidence(confidence)
        if self.score_delta is not None and (
            not isfinite(self.score_delta) or not -100 <= self.score_delta <= 100
        ):
            raise ValueError("score_delta must be within -100..100 or null")
        if self.target_probability is not None and self.target_score is None:
            raise ValueError("target_probability requires a target score")
        if not self.scoring_basis.strip() or not self.scoring_profile_name.strip():
            raise ValueError("scoring basis and profile name cannot be empty")


@dataclass(frozen=True, slots=True)
class TimelineEntryView:
    timestamp: str
    event_label: str
    level: int | None
    summary: str
    confidence: float | None = None

    def __post_init__(self) -> None:
        if not self.event_label.strip():
            raise ValueError("event_label cannot be empty")
        _validate_confidence(self.confidence)


@dataclass(frozen=True, slots=True)
class CorrectionRequest:
    session_id: str
    observation_id: str
    field_path: str
    previous_value: str
    corrected_value: str

    def __post_init__(self) -> None:
        if not self.session_id.strip() or not self.observation_id.strip():
            raise ValueError("session_id and observation_id cannot be empty")
        if not self.field_path.startswith("/"):
            raise ValueError("field_path must be an RFC 6901 JSON pointer")
        if not self.corrected_value.strip():
            raise ValueError("corrected_value cannot be empty")


class ResultDisposition(StrEnum):
    UNDECIDED = "undecided"
    KEPT = "kept"
    DISCARDED = "discarded"
    EQUIPPED = "equipped"


_CAPTURE_LABELS = {
    CaptureState.INACTIVE: "未啟用",
    CaptureState.STARTING: "正在啟動",
    CaptureState.WAITING_FOR_PAGE: "等待聲骸頁面",
    CaptureState.WAITING_FOR_REQUEST: "等待手動辨識",
    CaptureState.TRANSITIONING: "畫面切換中",
    CaptureState.RECOGNIZING: "辨識中",
    CaptureState.STABLE: "已穩定",
    CaptureState.LOW_CONFIDENCE: "OCR 信心不足",
    CaptureState.STOPPING: "正在停止",
    CaptureState.STOPPED: "已停止",
    CaptureState.ERROR: "發生錯誤",
}

_DISPOSITION_LABELS = {
    ResultDisposition.UNDECIDED: "尚未決定",
    ResultDisposition.KEPT: "保留",
    ResultDisposition.DISCARDED: "淘汰",
    ResultDisposition.EQUIPPED: "裝備至角色",
}

_INVALID_INDEX = QModelIndex()
type ModelIndex = QModelIndex | QPersistentModelIndex


class TimelineTableModel(QAbstractTableModel):
    """Small read-only model for the current capture session timeline."""

    _HEADERS = ("時間", "事件", "等級", "內容", "信心")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._entries: list[TimelineEntryView] = []

    def rowCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._entries)

    def columnCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._HEADERS)

    def data(self, index: ModelIndex, role: int = int(Qt.ItemDataRole.DisplayRole)) -> object:
        if not index.isValid() or not 0 <= index.row() < len(self._entries):
            return None
        if role != int(Qt.ItemDataRole.DisplayRole):
            return None
        entry = self._entries[index.row()]
        values = (
            entry.timestamp or "—",
            entry.event_label,
            f"+{entry.level}" if entry.level is not None else "—",
            entry.summary,
            _format_confidence(entry.confidence),
        )
        return values[index.column()]

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = int(Qt.ItemDataRole.DisplayRole),
    ) -> object:
        if (
            role == int(Qt.ItemDataRole.DisplayRole)
            and orientation == Qt.Orientation.Horizontal
            and 0 <= section < len(self._HEADERS)
        ):
            return self._HEADERS[section]
        return None

    def set_entries(self, entries: Sequence[TimelineEntryView]) -> None:
        self.beginResetModel()
        self._entries = list(entries)
        self.endResetModel()

    def append_entry(self, entry: TimelineEntryView) -> None:
        row = len(self._entries)
        self.beginInsertRows(QModelIndex(), row, row)
        self._entries.append(entry)
        self.endInsertRows()


class EchoLivePage(QWidget):
    """Live echo recognition workspace; contains no worker or persistence code."""

    start_requested = Signal(str, str, str)
    stop_requested = Signal()
    recheck_requested = Signal()
    capture_mode_changed = Signal(str)
    correction_requested = Signal(object)
    disposition_requested = Signal(str, str)
    character_link_requested = Signal(str, str, int)
    session_navigation_requested = Signal(int)
    training_clear_requested = Signal()
    presentation_changed = Signal()

    def __init__(
        self,
        *,
        monitors: Sequence[ChoiceOption] = (),
        characters: Sequence[ChoiceOption] = (),
        scoring_profiles: Sequence[ChoiceOption] = (),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("EchoLivePage")
        self._observation: EchoObservationView | None = None
        self._field_values: dict[str, str] = {}
        self._capture_state = CaptureState.INACTIVE
        self._display_mode = "dual"
        self._panel_active = False
        self._panel_result_current = False
        self._last_recommendation = RecommendationView()
        self._capture_run_label = "尚未開始本次辨識"
        self._character_options = tuple(characters)
        self._last_observation_session_id: str | None = None
        self._suggested_character_id = ""
        self._shown_assignment: tuple[str | None, int | None] = (None, None)
        self._analysis_pending = False

        title = QLabel("聲骸強化")
        title.setObjectName("PageTitle")
        subtitle = QLabel("僅在手動啟用期間被動辨識指定顯示器; 本頁不會操作遊戲。")
        subtitle.setObjectName("PageDescription")

        controls = self._build_capture_controls(monitors, characters, scoring_profiles)
        self.idle_panel = self._build_idle_panel()
        content_splitter = QSplitter(Qt.Orientation.Horizontal)
        content_splitter.setObjectName("EchoWorkspaceSplitter")
        content_splitter.setChildrenCollapsible(False)
        content_splitter.addWidget(self._build_observation_panel())
        content_splitter.addWidget(self._build_recommendation_panel())
        content_splitter.setStretchFactor(0, 3)
        content_splitter.setStretchFactor(1, 2)

        result_actions = self._build_result_actions_panel()
        timeline_panel = self._build_timeline_panel()
        self.results_container = QWidget()
        results_layout = QVBoxLayout(self.results_container)
        results_layout.setContentsMargins(0, 0, 0, 0)
        results_layout.setSpacing(8)
        self.analysis_refresh_label = QLabel()
        self.analysis_refresh_label.setObjectName("EchoAnalysisRefreshStatus")
        self.analysis_refresh_label.setWordWrap(True)
        self.analysis_refresh_label.hide()
        results_layout.addWidget(self.analysis_refresh_label)
        results_layout.addWidget(content_splitter, 3)
        results_layout.addWidget(result_actions)
        self.timeline_toggle = QPushButton("辨識紀錄")
        self.timeline_toggle.setCheckable(True)
        self.timeline_toggle.toggled.connect(timeline_panel.setVisible)
        results_layout.addWidget(self.timeline_toggle)
        results_layout.addWidget(timeline_panel)
        timeline_panel.hide()
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 10, 0)
        content_layout.setSpacing(8)
        self.training_panel = QGroupBox("替換目標")
        training_layout = QVBoxLayout(self.training_panel)
        training_layout.setContentsMargins(12, 16, 12, 8)
        training_row = QHBoxLayout()
        self.training_summary_label = QLabel()
        self.training_summary_label.setWordWrap(True)
        training_row.addWidget(self.training_summary_label, 1)
        self.training_details_button = QPushButton("需求詳情")
        self.training_details_button.setCheckable(True)
        self.training_label = QLabel()
        self.training_label.setWordWrap(True)
        self.training_details_button.toggled.connect(self.training_label.setVisible)
        training_row.addWidget(self.training_details_button)
        self.training_clear_button = QPushButton("結束比較")
        self.training_clear_button.clicked.connect(self.training_clear_requested)
        training_row.addWidget(self.training_clear_button)
        training_layout.addLayout(training_row)
        training_layout.addWidget(self.training_label)
        self.training_label.hide()
        self.training_panel.hide()
        content_layout.addWidget(self.idle_panel, 1)
        content_layout.addWidget(self.results_container, 5)

        scroll = QScrollArea()
        scroll.setObjectName("EchoLiveScrollArea")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 12)
        layout.setSpacing(6)
        layout.addWidget(title)
        title.setToolTip(subtitle.text())
        subtitle.hide()
        layout.addWidget(controls)
        layout.addWidget(self.training_panel)
        layout.addWidget(scroll, 1)

        self.apply_capture_status(CaptureStatusView(CaptureState.INACTIVE))
        self.apply_recommendation(RecommendationView())
        self.clear_observation()

    def set_training_target(self, description: str = "", *, is_same_echo: bool = False) -> None:
        self.training_panel.setTitle("現有聲骸強化" if is_same_echo else "替換目標")
        self.training_label.setText(description)
        lines = description.splitlines()
        priority = next((line for line in lines if line.startswith("副詞條優先:")), "")
        self.training_summary_label.setText(
            "\n".join([*lines[:1], *([priority] if priority else [])])
        )
        self.training_summary_label.setToolTip(description)
        self.training_panel.setVisible(bool(description))

    def _build_capture_controls(
        self,
        monitors: Sequence[ChoiceOption],
        characters: Sequence[ChoiceOption],
        scoring_profiles: Sequence[ChoiceOption],
    ) -> QGroupBox:
        group = QGroupBox("聲骸辨識")
        group.setObjectName("EchoCaptureControls")

        self.capture_status_label = QLabel()
        self.capture_status_label.setObjectName("EchoCaptureStatus")
        self.capture_detail_label = QLabel()
        self.capture_detail_label.setObjectName("EchoCaptureStatusDetail")
        self.capture_detail_label.setWordWrap(True)
        self.confidence_label = QLabel("OCR 信心: —")
        self.confidence_label.setObjectName("EchoConfidenceStatus")
        self.page_detection_label = QLabel("目前偵測: 尚未開始")
        self.page_detection_label.setObjectName("EchoPageDetectionStatus")
        self.page_detection_label.setWordWrap(True)

        self.monitor_combo = QComboBox()
        self.monitor_combo.setObjectName("EchoMonitorCombo")
        self.character_combo = QComboBox()
        self.character_combo.setObjectName("EchoCharacterCombo")
        self.profile_combo = QComboBox()
        self.profile_combo.setObjectName("EchoScoringProfileCombo")
        self.capture_mode_combo = QComboBox()
        self.capture_mode_combo.setObjectName("EchoCaptureModeCombo")
        self.capture_mode_combo.addItem("自動連續", "auto")
        self.capture_mode_combo.addItem("手動單次", "manual")
        self.capture_mode_combo.setToolTip("手動單次只在按下按鈕後讀取; 切換模式會停止目前辨識。")

        self.start_button = QPushButton("開始辨識")
        self.start_button.setObjectName("EchoStartButton")
        self.start_button.setProperty("role", "primary")
        self.start_button.clicked.connect(self._request_start)
        self.stop_button = QPushButton("停止辨識")
        self.stop_button.setObjectName("EchoStopButton")
        self.stop_button.clicked.connect(self._request_stop)
        # set_monitors() refreshes visibility before the grid adopts controls.
        self.recheck_button = QPushButton("立即重新辨識", group)
        self.recheck_button.setObjectName("EchoRecheckButton")
        self.recheck_button.setToolTip("重新讀取目前聲骸清單; 等待畫面穩定, 不中斷本次紀錄。")
        self.recheck_button.clicked.connect(self.recheck_requested.emit)
        self.capture_mode_combo.currentIndexChanged.connect(self._request_capture_mode_change)
        self.set_monitors(monitors)
        self.set_characters(characters)
        self.set_scoring_profiles(scoring_profiles)
        self.character_combo.currentIndexChanged.connect(self._refresh_scoring_basis)
        self.profile_combo.currentIndexChanged.connect(self._refresh_scoring_basis)

        self.advanced_toggle = QPushButton("螢幕與評分設定")
        self.advanced_toggle.setObjectName("EchoAdvancedOptionsToggle")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.toggled.connect(self._toggle_advanced_options)
        self.advanced_widget = QWidget()
        self.advanced_widget.setObjectName("EchoAdvancedOptions")
        advanced_layout = QGridLayout(self.advanced_widget)
        advanced_layout.setContentsMargins(0, 4, 0, 0)
        advanced_layout.addWidget(QLabel("遊戲所在螢幕"), 0, 0)
        advanced_layout.addWidget(self.monitor_combo, 0, 1, 1, 3)
        advanced_layout.addWidget(QLabel("指定角色 (可選)"), 1, 0)
        advanced_layout.addWidget(self.character_combo, 1, 1)
        advanced_layout.addWidget(QLabel("評分設定檔 (可選)"), 1, 2)
        advanced_layout.addWidget(self.profile_combo, 1, 3)
        advanced_layout.addWidget(self.capture_detail_label, 2, 0, 1, 4)
        advanced_layout.addWidget(self.page_detection_label, 3, 0, 1, 4)
        self.advanced_widget.setVisible(False)

        grid = QGridLayout(group)
        grid.setContentsMargins(12, 16, 12, 8)
        grid.addWidget(self.capture_mode_combo, 0, 0)
        grid.addWidget(self.start_button, 0, 1)
        grid.addWidget(self.stop_button, 0, 2)
        grid.addWidget(self.recheck_button, 0, 3)
        grid.addWidget(self.capture_status_label, 0, 4)
        grid.addWidget(self.confidence_label, 0, 5)
        grid.addWidget(self.advanced_toggle, 0, 6)
        grid.addWidget(self.advanced_widget, 1, 0, 1, 7)
        grid.setColumnStretch(4, 1)
        return group

    @property
    def manual_mode(self) -> bool:
        return self.capture_mode_combo.currentData() == "manual"

    def select_capture_mode(self, mode: str) -> None:
        """Restore a preference without interpreting it as a new capture request."""

        index = self.capture_mode_combo.findData(mode)
        if index < 0:
            raise ValueError("capture mode must be auto or manual")
        previous = self.capture_mode_combo.blockSignals(True)
        self.capture_mode_combo.setCurrentIndex(index)
        self.capture_mode_combo.blockSignals(previous)
        self._refresh_capture_controls()
        if self._observation is None:
            self._show_capture_instructions()

    def _request_capture_mode_change(self) -> None:
        self._refresh_capture_controls()
        if self._observation is None:
            self._show_capture_instructions()
        self.capture_mode_changed.emit(str(self.capture_mode_combo.currentData()))

    def _show_capture_instructions(self) -> None:
        self.idle_title_label.setText("準備手動辨識" if self.manual_mode else "準備第一次聲骸辨識")
        action = (
            "3. 按下「辨識目前畫面」; 每次換聲骸或培養返回清單後, 再按一次"
            if self.manual_mode
            else "3. 按下「開始辨識」; 培養後返回清單, 即會重新讀取"
        )
        self.idle_steps_label.setText(
            "1. 在遊戲中打開聲骸清單, 選取要辨識的聲骸\n"
            "2. 上方選擇遊戲所在的左側/右側螢幕\n"
            f"{action}\n\n"
            "若一直沒有結果: 設定 → 一般設定 → 本機資料與備份 → "
            "預覽診斷包 (不含畫面)。"
        )

    def _refresh_capture_controls(self) -> None:
        active = self._capture_state not in {
            CaptureState.INACTIVE,
            CaptureState.STOPPED,
            CaptureState.ERROR,
        }
        manual_idle = self.manual_mode and self._capture_state is CaptureState.WAITING_FOR_REQUEST
        single = self._display_mode == "single"
        self.start_button.setText(
            "啟動小面板" if single else "辨識目前畫面" if self.manual_mode else "開始辨識"
        )
        self.stop_button.setText("結束小面板" if single else "停止辨識")
        self.start_button.setEnabled(
            bool(self.monitor_combo.currentData())
            and (not active or manual_idle)
            and not self._panel_active
        )
        self.stop_button.setEnabled(
            (active or self._panel_active) and self._capture_state is not CaptureState.STOPPING
        )
        self.recheck_button.setVisible(not self.manual_mode)
        self.recheck_button.setEnabled(
            not self.manual_mode
            and active
            and self._capture_state not in {CaptureState.STARTING, CaptureState.STOPPING}
        )
        self.monitor_combo.setEnabled(not active)
        self.character_combo.setEnabled(not active)
        self.profile_combo.setEnabled(not active)

    def set_display_mode(self, mode: str) -> None:
        self._display_mode = mode
        if mode == "single":
            self.select_capture_mode("auto")
        self.capture_mode_combo.setVisible(mode != "single")
        self._refresh_capture_controls()

    def set_panel_active(self, active: bool) -> None:
        self._panel_active = active
        self._refresh_capture_controls()

    def mark_analysis_stale(self) -> None:
        self._panel_result_current = False
        self.presentation_changed.emit()

    def confirm_current_observation(self, observation_id: str) -> None:
        if (
            self._observation is not None
            and self._observation.observation_id == observation_id
            and not self._analysis_pending
        ):
            self._panel_result_current = True
            self.presentation_changed.emit()

    def panel_snapshot(self) -> EchoPanelSnapshot:
        observation, recommendation = self._observation, self._last_recommendation
        missing = (
            ()
            if observation is None
            else tuple(dict.fromkeys(issue.label for issue in observation.review_issues))
        )
        if observation is not None and observation.needs_confirmation and not missing:
            missing = ("辨識資料待覆核",)
        stale = observation is not None and (
            not self._panel_result_current
            or self._analysis_pending
            or self._capture_state not in {CaptureState.STABLE, CaptureState.LOW_CONFIDENCE}
        )
        return EchoPanelSnapshot(
            name=observation.name or "名稱待確認" if observation else "等待聲骸辨識",
            level=observation.level if observation else None,
            score=None if missing else recommendation.score,
            target_score=recommendation.target_score,
            save_advice="資料不足, 請先確認" if missing else recommendation.save_advice,
            upgrade_advice="資料不足, 請先確認"
            if missing
            else recommendation.upgrade_advice or recommendation.conclusion,
            basis=recommendation.combat_summary
            or f"{recommendation.scoring_basis} · {recommendation.scoring_profile_name}",
            status="辨識不足, 需要人工確認"
            if missing
            else "正在更新辨識與評分"
            if self._analysis_pending
            else _CAPTURE_LABELS[self._capture_state],
            detail=self.capture_detail_label.text()
            if stale or observation is None
            else recommendation.reason,
            missing_fields=missing,
            stale=stale,
        )

    def _build_idle_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("EchoIdleCard")
        self.idle_title_label = QLabel("準備第一次聲骸辨識")
        self.idle_title_label.setObjectName("EchoIdleTitle")
        self.idle_steps_label = QLabel(
            "1. 在遊戲中打開聲骸清單, 選取要辨識的聲骸\n"
            "2. 上方選擇遊戲所在的左側/右側螢幕\n"
            "3. 按下「開始辨識」; 培養後返回清單, 即會重新讀取\n\n"
            "若一直沒有結果: 設定 → 一般設定 → 本機資料與備份 → "
            "預覽診斷包 (不含畫面)。"
        )
        self.idle_steps_label.setObjectName("EchoIdleSteps")
        self.idle_steps_label.setWordWrap(True)
        safety = QLabel("工具只觀察畫面, 不會替你點擊、強化、棄置或選擇聲骸。")
        safety.setObjectName("PageDescription")
        safety.setWordWrap(True)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(26, 24, 26, 24)
        layout.setSpacing(14)
        layout.addWidget(self.idle_title_label)
        layout.addWidget(self.idle_steps_label)
        layout.addWidget(safety)
        layout.addStretch(1)
        return panel

    def _build_observation_panel(self) -> QGroupBox:
        group = QGroupBox("目前聲骸")
        group.setObjectName("EchoCurrentPanel")
        self.session_label = QLabel("—")
        self.session_label.setObjectName("EchoCurrentSession")
        self.name_label = QLabel("—")
        self.name_label.setObjectName("EchoCurrentName")
        self.set_label = QLabel("—")
        self.set_label.setObjectName("EchoCurrentSet")
        self.cost_label = QLabel("—")
        self.cost_label.setObjectName("EchoCurrentCost")
        self.level_label = QLabel("—")
        self.level_label.setObjectName("EchoCurrentLevel")
        self.locked_slots_label = QLabel("—")
        self.locked_slots_label.setObjectName("EchoLockedSubstatSlots")
        self.game_equipped_label = QLabel("無法確認")
        self.game_equipped_label.setObjectName("EchoGameEquippedStatus")

        form = QGridLayout()
        form.addWidget(self.name_label, 0, 0, 1, 3)
        self.name_label.setWordWrap(True)
        form.addWidget(self.level_label, 0, 3)
        form.addWidget(self.set_label, 1, 0, 1, 2)
        form.addWidget(QLabel("COST"), 1, 2)
        form.addWidget(self.cost_label, 1, 3)
        form.addWidget(self.game_equipped_label, 2, 0, 1, 4)
        self.game_equipped_label.setWordWrap(True)
        self.session_label.hide()
        self.locked_slots_label.hide()

        self.main_stats_table = _stat_table("EchoMainStatsTable")
        self.sub_stats_table = _stat_table("EchoSubstatsTable")
        self.main_stats_table.setFixedHeight(90)
        for column in (2, 3, 4):
            self.main_stats_table.hideColumn(column)
        self.sub_stats_table.setFixedHeight(180)
        self.sub_stats_table.hideColumn(4)
        self.confirmation_label = QLabel("目前沒有待確認欄位。")
        self.confirmation_label.setObjectName("EchoConfirmationStatus")
        self.confirmation_label.setWordWrap(True)
        self.correction_button = QPushButton("修正 OCR")
        self.correction_button.setObjectName("EchoCorrectionButton")
        self.correction_button.clicked.connect(self._open_correction_dialog)

        layout = QVBoxLayout(group)
        layout.setContentsMargins(12, 18, 12, 8)
        layout.setSpacing(5)
        layout.addLayout(form)
        layout.addWidget(QLabel("主屬性"))
        layout.addWidget(self.main_stats_table)
        layout.addWidget(QLabel("副詞條"))
        layout.addWidget(self.sub_stats_table)
        correction_row = QHBoxLayout()
        correction_row.addWidget(self.confirmation_label, 1)
        correction_row.addWidget(self.correction_button)
        layout.addLayout(correction_row)
        return group

    def _build_recommendation_panel(self) -> QGroupBox:
        group = QGroupBox("分析建議")
        group.setObjectName("EchoRecommendationPanel")
        self.conclusion_label = QLabel()
        self.conclusion_label.setObjectName("EchoDecisionHeadline")
        self.conclusion_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.conclusion_label.setWordWrap(True)
        self.scoring_basis_label = QLabel()
        self.scoring_basis_label.setObjectName("EchoScoringBasis")
        self.scoring_basis_label.setWordWrap(True)
        self.score_label = QLabel()
        self.score_label.setObjectName("EchoCurrentScore")
        apply_score_color(self.score_label, None)
        self.score_label.hide()  # Secondary historical scale is available in the tooltip.
        self.cumulative_score_label = QLabel()
        self.cumulative_score_label.setObjectName("EchoCumulativeScore")
        apply_score_color(self.cumulative_score_label, None)
        self.score_range_label = QLabel()
        self.score_range_label.setObjectName("EchoScoreRange")
        self.probability_label = QLabel()
        self.probability_label.setObjectName("EchoProbabilitySummary")
        self.probability_label.setWordWrap(True)
        self.score_delta_label = QLabel()
        self.score_delta_label.setObjectName("EchoTargetScoreDelta")
        self.score_delta_label.setWordWrap(True)
        self.reason_label = QLabel()
        self.reason_label.setObjectName("EchoDecisionReason")
        self.reason_label.setWordWrap(True)
        self.reason_label.hide()
        self.combat_summary_label = QLabel()
        self.combat_summary_label.setObjectName("EchoCombatSummary")
        self.combat_summary_label.setWordWrap(True)
        self.combat_summary_label.hide()
        self.save_advice_label = QLabel()
        self.save_advice_label.setObjectName("EchoSaveAdvice")
        self.save_advice_label.setWordWrap(True)
        self.upgrade_advice_label = QLabel()
        self.upgrade_advice_label.setObjectName("EchoUpgradeAdvice")
        self.upgrade_advice_label.setWordWrap(True)
        self.breakdown_label = QLabel()
        self.breakdown_label.setObjectName("EchoScoreBreakdown")
        self.breakdown_label.setWordWrap(True)
        self.breakdown_label.setVisible(False)
        self.breakdown_button = QPushButton("展開計算理由")
        self.breakdown_button.setObjectName("EchoBreakdownButton")
        self.breakdown_button.setCheckable(True)
        self.breakdown_button.toggled.connect(self._toggle_breakdown)

        layout = QVBoxLayout(group)
        layout.addWidget(self.conclusion_label)
        layout.addWidget(self.scoring_basis_label)
        layout.addWidget(self.score_label)
        layout.addWidget(self.cumulative_score_label)
        layout.addWidget(self.score_range_label)
        layout.addWidget(self.probability_label)
        layout.addWidget(self.score_delta_label)
        layout.addWidget(self.save_advice_label)
        layout.addWidget(self.upgrade_advice_label)
        layout.addWidget(self.breakdown_button)
        layout.addWidget(self.reason_label)
        layout.addWidget(self.combat_summary_label)
        layout.addWidget(self.breakdown_label)
        layout.addStretch(1)
        self._recommendation_basis = "通用參考"
        self._recommendation_profile_name = "未指定評分設定檔"
        self._refresh_scoring_basis()
        return group

    def _build_result_actions_panel(self) -> QGroupBox:
        group = QGroupBox("辨識完成後")
        group.setObjectName("EchoResultActionsPanel")
        self.disposition_combo = QComboBox()
        self.disposition_combo.setObjectName("EchoDispositionCombo")
        for disposition, label in _DISPOSITION_LABELS.items():
            self.disposition_combo.addItem(label, disposition.value)
        self.disposition_button = QPushButton("儲存最終結果")
        self.disposition_button.setObjectName("EchoDispositionButton")
        self.disposition_button.clicked.connect(self._request_disposition)
        # Retain the generic controls as a compatibility surface for callers, but
        # give the daily workflow explicit one-click actions whose result is visible.
        self.disposition_combo.setVisible(False)
        self.disposition_button.setVisible(False)

        self.keep_button = QPushButton("保留這顆")
        self.keep_button.setObjectName("EchoKeepButton")
        self.keep_button.setProperty("role", "primary")
        self.keep_button.clicked.connect(
            lambda: self._request_specific_disposition(ResultDisposition.KEPT)
        )
        self.discard_button = QPushButton("淘汰這顆")
        self.discard_button.setObjectName("EchoDiscardButton")
        self.discard_button.clicked.connect(
            lambda: self._request_specific_disposition(ResultDisposition.DISCARDED)
        )
        self.result_character_combo = QComboBox()
        self.result_character_combo.setObjectName("EchoResultCharacterCombo")
        _set_choices(
            self.result_character_combo,
            self._character_options,
            "尚未建立/選擇角色",
            allow_empty=True,
        )
        self.result_character_combo.currentIndexChanged.connect(self._update_character_link_enabled)
        self.result_slot_combo = QComboBox()
        self.result_slot_combo.setObjectName("EchoResultSlotCombo")
        self.result_slot_combo.addItem("選擇位置", 0)
        for slot_index in range(1, 6):
            self.result_slot_combo.addItem(f"位置 {slot_index}", slot_index)
        self.result_slot_combo.currentIndexChanged.connect(self._update_character_link_enabled)
        self.link_character_button = QPushButton("保存至角色位置")
        self.link_character_button.setObjectName("EchoLinkCharacterButton")
        self.link_character_button.clicked.connect(self._request_character_link)
        self.link_character_button.setToolTip("套用至角色頁; 該位置原有的聲骸會保留在清單中。")
        self.result_status_label = QLabel(
            "已自動保存, 不用按任何按鈕; 可直接在遊戲切到下一顆。下列整理動作皆為可選。"
        )
        self.result_status_label.setObjectName("EchoResultStatus")
        self.result_status_label.setWordWrap(True)

        actions_widget = QWidget()
        actions = QHBoxLayout(actions_widget)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.addWidget(self.keep_button)
        actions.addWidget(self.discard_button)
        actions.addSpacing(12)
        actions.addWidget(self.result_character_combo, 1)
        actions.addWidget(self.result_slot_combo)
        actions.addWidget(self.link_character_button)

        layout = QVBoxLayout(group)
        layout.setContentsMargins(12, 16, 12, 8)
        row = QHBoxLayout()
        row.addWidget(self.result_status_label, 1)
        self.organize_toggle = QPushButton("保留 / 淘汰 / 調整位置")
        self.organize_toggle.setCheckable(True)
        self.organize_toggle.toggled.connect(actions_widget.setVisible)
        row.addWidget(self.organize_toggle)
        layout.addLayout(row)
        layout.addWidget(actions_widget)
        actions_widget.hide()
        return group

    def _build_timeline_panel(self) -> QGroupBox:
        group = QGroupBox("這顆聲骸的辨識紀錄")
        group.setObjectName("EchoTimelinePanel")
        self.capture_run_label = QLabel(self._capture_run_label)
        self.capture_run_label.setObjectName("EchoCaptureRunLabel")
        self.capture_run_label.setWordWrap(True)
        self.timeline_model = TimelineTableModel(self)
        self.timeline_table = QTableView()
        self.timeline_table.setObjectName("EchoTimelineTable")
        self.timeline_table.setModel(self.timeline_model)
        self.timeline_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.timeline_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.timeline_table.horizontalHeader().setStretchLastSection(True)

        previous_button = QPushButton("上一顆")
        previous_button.setObjectName("EchoPreviousButton")
        previous_button.clicked.connect(lambda: self.session_navigation_requested.emit(-1))
        next_button = QPushButton("下一顆")
        next_button.setObjectName("EchoNextButton")
        next_button.clicked.connect(lambda: self.session_navigation_requested.emit(1))
        history_navigation = QHBoxLayout()
        previous_button.setVisible(False)
        next_button.setVisible(False)
        history_navigation.addWidget(previous_button)
        history_navigation.addWidget(next_button)
        history_navigation.addStretch(1)
        history_navigation.addWidget(QLabel("完整紀錄請到「歷史紀錄」查看。"))

        layout = QVBoxLayout(group)
        layout.addWidget(self.capture_run_label)
        layout.addWidget(self.timeline_table)
        layout.addLayout(history_navigation)
        return group

    @Slot(object)
    def apply_capture_status(self, status: object) -> None:
        if not isinstance(status, CaptureStatusView):
            raise TypeError("status must be CaptureStatusView")
        self._capture_state = status.state
        if status.state not in {CaptureState.STABLE, CaptureState.LOW_CONFIDENCE}:
            self._panel_result_current = False
        self.presentation_changed.emit()
        state_label = _CAPTURE_LABELS[status.state]
        self.capture_status_label.setText(state_label)
        self.capture_status_label.setProperty("captureState", status.state.value)
        self.capture_detail_label.setText(status.detail)
        self.capture_status_label.setToolTip(status.detail)
        if status.state is CaptureState.ERROR:
            self.advanced_toggle.setChecked(True)
        self.confidence_label.setText(f"OCR 信心: {_format_confidence(status.confidence)}")
        self.confidence_label.setProperty(
            "lowConfidence", status.state is CaptureState.LOW_CONFIDENCE
        )
        _repolish(self.capture_status_label)
        _repolish(self.confidence_label)

        self._refresh_capture_controls()
        if self._observation is None:
            if status.state in {
                CaptureState.STARTING,
                CaptureState.WAITING_FOR_PAGE,
                CaptureState.TRANSITIONING,
                CaptureState.RECOGNIZING,
            }:
                self.idle_title_label.setText("正在尋找聲骸畫面")
                self.idle_steps_label.setText(
                    (
                        "正在讀取目前聲骸清單; 完成後會停止讀取, 等待下一次按鈕。\n"
                        if self.manual_mode
                        else "僅辨識聲骸清單。培養期間暫停讀取, 返回清單後自動恢復。\n"
                    )
                    + "如果數秒後仍沒有偵測結果, 請確認是否選到正確的左側/右側螢幕。\n"
                    "診斷紀錄: 設定 → 一般設定 → 本機資料與備份 → "
                    "預覽診斷包 (不含畫面)。"
                )
            elif status.state is CaptureState.WAITING_FOR_REQUEST:
                self.idle_title_label.setText("等待手動辨識")
                self.idle_steps_label.setText(
                    status.detail or "請在遊戲聲骸清單選好聲骸, 再按「辨識目前畫面」。"
                )
            elif status.state in {CaptureState.INACTIVE, CaptureState.STOPPED}:
                self._show_capture_instructions()
            elif status.state is CaptureState.ERROR:
                self.idle_title_label.setText("辨識未成功")
                self.idle_steps_label.setText(
                    status.detail
                    or (
                        "請確認螢幕選擇與本機辨識元件, 再停止後重新開始。\n"
                        "診斷紀錄: 設定 → 一般設定 → 本機資料與備份 → "
                        "預覽診斷包 (不含畫面)。"
                    )
                )

    @Slot(str, object, bool)
    def apply_page_detection(
        self,
        page_type: str,
        confidence: float | None,
        relevant: bool,
    ) -> None:
        """Show actionable Traditional-Chinese guidance for the latest stable page."""

        _validate_confidence(confidence)
        normalized = page_type.strip().casefold()
        states: dict[str, tuple[str, str]] = {
            "character_echo_overview": (
                "角色聲骸總覽",
                "請打開聲骸清單並選擇一顆聲骸。",
            ),
            "echo_selection": (
                "聲骸選擇",
                "畫面正確, 會自動讀取所選聲骸; 培養後請返回此清單。",
            ),
            "echo_enhancement": (
                "聲骸強化",
                "培養期間暫停讀取, 返回聲骸清單後自動恢復。",
            ),
            "enhancement_material_selection": (
                "強化素材選擇",
                "素材選擇期間暫停讀取, 返回聲骸清單後自動恢復。",
            ),
        }
        label, guidance = states.get(
            normalized,
            (
                "未看到支援的聲骸頁",
                "僅辨識聲骸清單。請確認遊戲是否在所選螢幕, 培養完成後返回清單; "
                "可按「立即重新辨識」重新讀取。\n"
                "診斷紀錄: 設定 → 一般設定 → 本機資料與備份 → "
                "預覽診斷包 (不含畫面)。",
            ),
        )
        if self.manual_mode:
            guidance = (
                "僅讀取本次要求的聲骸; 下次換聲骸或培養返回清單後, 再按「辨識目前畫面」。"
                if normalized == "echo_selection"
                else "只辨識聲骸清單; 請返回清單, 再按「辨識目前畫面」。"
            )
        self.page_detection_label.setText(f"目前偵測: {label} · {guidance}")
        self.page_detection_label.setProperty("pageType", normalized or "unknown")
        self.page_detection_label.setProperty("relevant", relevant)
        self.confidence_label.setText(f"OCR 信心: {_format_confidence(confidence)}")
        self.confidence_label.setProperty(
            "lowConfidence", confidence is not None and confidence < 0.85
        )
        _repolish(self.page_detection_label)
        _repolish(self.confidence_label)
        if self._observation is None:
            self.idle_title_label.setText(f"目前偵測: {label}")
            self.idle_steps_label.setText(guidance)

    @Slot(object)
    def apply_observation(self, observation: object) -> None:
        if not isinstance(observation, EchoObservationView):
            raise TypeError("observation must be EchoObservationView")
        if observation.session_id != self._last_observation_session_id:
            self.result_slot_combo.setCurrentIndex(0)
            self.result_character_combo.setCurrentIndex(self.result_character_combo.findData(""))
            self._suggested_character_id = ""
            self._shown_assignment = (None, None)
        self._last_observation_session_id = observation.session_id
        self._observation = observation
        self.presentation_changed.emit()
        self.idle_panel.setVisible(False)
        self.results_container.setVisible(True)
        session_number = _local_session_number(observation.session_id)
        item_label = f"第 {session_number} 顆" if session_number is not None else "已保存"
        self.capture_run_label.setText(f"{self._capture_run_label} · 本次{item_label}")
        self.session_label.setText(item_label)
        self.name_label.setText(observation.name or "需要人工確認")
        self.set_label.setText(observation.set_name or "—")
        self.cost_label.setText(str(observation.cost) if observation.cost is not None else "—")
        self.level_label.setText(
            f"+{observation.level}" if observation.level is not None else "需要人工確認"
        )
        self.locked_slots_label.setText(
            str(observation.locked_substat_slots)
            if observation.locked_substat_slots is not None
            else "—"
        )
        equipped_label = {
            True: "已裝備 (由遊戲畫面辨識)",
            False: "未裝備 (由遊戲畫面辨識)",
            None: "無法確認",
        }[observation.is_equipped]
        if observation.is_equipped is True and observation.equipped_character_name:
            equipped_label = f"已裝備於 {observation.equipped_character_name} (由遊戲畫面辨識)"
            if observation.assigned_slot_index is not None:
                equipped_label += f" · 已同步位置 {observation.assigned_slot_index}"
            elif observation.equipped_slot_index is not None:
                equipped_label += f" · 已辨識位置 {observation.equipped_slot_index}, 尚未保存位置"
            else:
                equipped_label += " · 位置待確認"
        self.game_equipped_label.setText(equipped_label)
        self.game_equipped_label.setProperty(
            "equippedState",
            "equipped"
            if observation.is_equipped is True
            else "not_equipped"
            if observation.is_equipped is False
            else "unknown",
        )
        if observation.equipped_slot_index is not None and not self.result_slot_combo.currentData():
            slot_combo_index = self.result_slot_combo.findData(observation.equipped_slot_index)
            if slot_combo_index >= 0:
                self.result_slot_combo.setCurrentIndex(slot_combo_index)
        self._sync_result_character()
        _repolish(self.game_equipped_label)
        _fill_stat_table(self.main_stats_table, observation.main_stats)
        _fill_stat_table(self.sub_stats_table, observation.sub_stats)
        self._field_values = _observation_fields(observation)
        self.correction_button.setEnabled(bool(self._field_values))
        self.disposition_button.setEnabled(True)
        self.keep_button.setEnabled(True)
        self.discard_button.setEnabled(True)
        self._update_character_link_enabled()
        issues = observation.review_issues
        for path, label in (
            ("/name", self.name_label),
            ("/set_name", self.set_label),
            ("/cost", self.cost_label),
            ("/level", self.level_label),
        ):
            reasons = [issue.message for issue in issues if issue.field_path == path]
            label.setProperty("needsConfirmation", bool(reasons))
            label.setToolTip("\n".join(reasons))
            label.setStyleSheet("color: #ebc17c;" if reasons else "")
        for prefix, table in (
            ("main_stats", self.main_stats_table),
            ("sub_stats", self.sub_stats_table),
        ):
            model = table.model()
            if isinstance(model, _StatTableModel):
                model.set_review_issues(prefix, issues)
        if issues or observation.needs_confirmation:
            field_labels = tuple(dict.fromkeys(issue.label for issue in issues))
            summary = "、".join(field_labels[:2])
            if len(field_labels) > 2:
                summary += f"等 {len(field_labels)} 項"
            self.confirmation_label.setText(
                f"待確認: {summary} · 按「修正 OCR」查看。"
                if issues
                else "整筆辨識仍待覆核, 請重新辨識或修正欄位。"
            )
            self.confirmation_label.setToolTip("\n".join(issue.message for issue in issues))
            self.confirmation_label.setProperty("needsConfirmation", True)
        else:
            self.confirmation_label.setText("目前辨識結果已達信心門檻。")
            self.confirmation_label.setToolTip("")
            self.confirmation_label.setProperty("needsConfirmation", False)
        _repolish(self.confirmation_label)

    @Slot()
    def clear_observation(self) -> None:
        self._panel_result_current = False
        self._last_recommendation = RecommendationView()
        self.presentation_changed.emit()
        self.set_analysis_pending(False)
        self._observation = None
        self._last_observation_session_id = None
        self._suggested_character_id = ""
        self._shown_assignment = (None, None)
        self._field_values = {}
        self.results_container.setVisible(False)
        self.idle_panel.setVisible(True)
        for label in (
            self.session_label,
            self.name_label,
            self.set_label,
            self.cost_label,
            self.level_label,
            self.locked_slots_label,
            self.game_equipped_label,
        ):
            label.setText("—")
        _fill_stat_table(self.main_stats_table, ())
        _fill_stat_table(self.sub_stats_table, ())
        self.confirmation_label.setText("等待穩定的聲骸畫面。")
        self.confirmation_label.setToolTip("")
        self.confirmation_label.setProperty("needsConfirmation", False)
        self.correction_button.setEnabled(False)
        self.disposition_button.setEnabled(False)
        self.keep_button.setEnabled(False)
        self.discard_button.setEnabled(False)
        self.link_character_button.setEnabled(False)
        self.result_status_label.setText(
            "已自動保存, 不用按任何按鈕; 可直接在遊戲切到下一顆。下列整理動作皆為可選。"
        )
        self.result_status_label.setProperty("disposition", ResultDisposition.UNDECIDED.value)
        self.timeline_model.set_entries(())
        self.result_slot_combo.setCurrentIndex(0)

    @Slot(str)
    def begin_capture_run(self, label: str) -> None:
        """Visually isolate a newly started passive-capture run."""

        normalized = label.strip() or "本次辨識"
        self._capture_run_label = normalized
        self.clear_observation()
        self.capture_run_label.setText(normalized)
        self.idle_title_label.setText(f"{normalized}: 等待聲骸畫面")
        self.idle_steps_label.setText(
            "請在遊戲中自行切到聲骸清單; 畫面穩定後會自動顯示這一顆的結果。"
        )

    @Slot(str)
    def apply_disposition(self, disposition: str) -> None:
        try:
            normalized = ResultDisposition(disposition)
        except ValueError:
            normalized = ResultDisposition.UNDECIDED
        label = _DISPOSITION_LABELS[normalized]
        if normalized is ResultDisposition.UNDECIDED:
            message = "已自動保存, 不用按任何按鈕; 保留、淘汰與角色關聯皆為可選整理。"
        else:
            message = f"已處理: {label}。可繼續在遊戲中查看下一顆。"
        observation = self._observation
        if (
            normalized is ResultDisposition.EQUIPPED
            and observation is not None
            and observation.assigned_character_id
            and observation.assigned_slot_index is not None
        ):
            character = next(
                (
                    item.label
                    for item in self._character_options
                    if item.identifier == observation.assigned_character_id
                ),
                "指定角色",
            )
            message = (
                f"已保存至 {character} · 位置 {observation.assigned_slot_index}。"
                "角色頁已同步, 不必重選聲骸。"
            )
        elif observation is not None and observation.assigned_character_id:
            if observation.equipped_slot_index is not None:
                message = (
                    f"遊戲已辨識位置 {observation.equipped_slot_index}, 本機尚未保存位置。"
                    "展開「調整位置」並按「保存至角色位置」確認替換; 原聲骸會保留在清單。"
                )
            else:
                message = (
                    f"已登記於 {observation.equipped_character_name or '角色'}, 位置待確認。"
                    "可點遊戲左側已裝備的圓形聲骸再掃描, 或展開「調整位置」。"
                )
        self.result_status_label.setText(message)
        self.result_status_label.setProperty("disposition", normalized.value)
        self.result_status_label.setProperty("revisitStatus", "")
        _repolish(self.result_status_label)

    @Slot(str)
    def apply_revisit_notice(self, status: str) -> None:
        """Explain safe same-run revisit handling without exposing internal IDs."""

        normalized = status.strip().casefold()
        if normalized == "reused":
            message = "已辨識為本次先前看過的同一顆聲骸, 已接回原紀錄; 沒有重複建立聲骸或評分紀錄。"
        elif normalized == "possible":
            message = "這顆與本次先前紀錄的畫面相似, 但證據不足; 為避免誤合併, 已安全分成新的紀錄。"
        else:
            return
        self.result_status_label.setText(message)
        self.result_status_label.setProperty("revisitStatus", normalized)
        _repolish(self.result_status_label)

    @Slot(bool)
    def set_analysis_pending(self, pending: bool, *, retain_previous: bool = True) -> None:
        """Keep one coherent result visible while its replacement is calculated."""

        self._analysis_pending = pending
        self.presentation_changed.emit()
        self.analysis_refresh_label.setText(
            "正在更新辨識與評分; 下方保留上次完成的結果, 完成後會一起更新。"
            if retain_previous
            else "正在計算本次辨識結果…"
        )
        self.analysis_refresh_label.setVisible(pending)
        available = self._observation is not None and not pending
        self.correction_button.setEnabled(available and bool(self._field_values))
        self.disposition_button.setEnabled(available)
        self.keep_button.setEnabled(available)
        self.discard_button.setEnabled(available)
        self._update_character_link_enabled()

    def apply_analysis_result(
        self,
        observation: EchoObservationView,
        recommendation: RecommendationView,
        *,
        current: bool = True,
    ) -> None:
        """Publish stats, score and guidance in the same Qt paint cycle."""

        updates_enabled = self.results_container.updatesEnabled()
        self.results_container.setUpdatesEnabled(False)
        try:
            self.apply_observation(observation)
            self.apply_recommendation(recommendation)
            self.set_analysis_pending(False)
            self._panel_result_current = current
        finally:
            self.results_container.setUpdatesEnabled(updates_enabled)

    @Slot(object)
    def apply_recommendation(self, recommendation: object) -> None:
        if not isinstance(recommendation, RecommendationView):
            raise TypeError("recommendation must be RecommendationView")
        self._last_recommendation = recommendation
        self.presentation_changed.emit()
        self.save_advice_label.setText("保存建議: " + recommendation.save_advice)
        self.upgrade_advice_label.setText(
            "升級建議: " + (recommendation.upgrade_advice or recommendation.conclusion)
        )
        self.conclusion_label.setText(recommendation.conclusion)
        self._recommendation_basis = recommendation.scoring_basis
        self._recommendation_profile_name = recommendation.scoring_profile_name
        self._refresh_scoring_basis()
        self.score_label.setText(
            f"已開詞條品質 {recommendation.opened_slot_quality_score:.1f}/100"
            if recommendation.opened_slot_quality_score is not None
            else "已開詞條品質 —/100"
        )
        apply_score_color(self.score_label, recommendation.opened_slot_quality_score)
        self.cumulative_score_label.setText(
            f"累積分 {recommendation.score:.1f}/100"
            if recommendation.score is not None
            else "累積分 —/100"
        )
        apply_score_color(self.cumulative_score_label, recommendation.score)
        self.cumulative_score_label.setToolTip(
            "副詞條累積分以 +25 滿五詞條為共同基準。\n"
            + self.score_label.text()
            + "\n此補充指標依已開欄數的理想種類計算, 與實用分基準不同。"
        )
        expected = score_span(
            "—"
            if recommendation.expected_score is None
            else f"{recommendation.expected_score:.1f}",
            recommendation.expected_score,
        )
        score_range = score_span("—", None)
        if recommendation.estimated_score_range is not None:
            minimum, maximum = recommendation.estimated_score_range
            score_range = (
                score_span(f"{minimum:.1f}", minimum)
                + "\u2013"
                + score_span(f"{maximum:.1f}", maximum)
            )
        self.score_range_label.setText(f"期望 {expected} · 範圍 {score_range}")
        self.score_range_label.setToolTip("升滿 +25、調諧五條副詞條後的預估得分。")
        target = recommendation.target_score
        self.probability_label.setVisible(not recommendation.is_same_echo)
        if target is not None:
            self.probability_label.setText(
                f"超越現有機率 {_format_probability(recommendation.target_probability)}"
            )
            self.probability_label.setToolTip(
                f"升滿後副詞條分數嚴格高於現有 {target:.2f} 分的機率; "
                "同分不算超越, 套裝與主詞條仍依配裝建議判斷。"
            )
            delta = (
                "—" if recommendation.score_delta is None else f"{recommendation.score_delta:+.1f}"
            )
            self.score_delta_label.setText(
                f"與現有差距 {delta} 分 · 現有 " + score_span(f"{target:.1f}", target)
            )
            self.score_delta_label.setToolTip("目前累積分減去所選位置現有聲骸的分數。")
        else:
            self.probability_label.setText("超越現有機率 —")
            self.probability_label.setToolTip("從角色聲骸選擇替換目標, 即可比較分差與超越機率。")
        self.score_delta_label.setVisible(target is not None)
        generic_probabilities = (
            "通用門檻參考: "
            f"及格 {_format_probability(recommendation.pass_probability)} · "
            f"完美 {_format_probability(recommendation.perfect_probability)} · "
            f"極品 {_format_probability(recommendation.exceptional_probability)}"
        )
        self.reason_label.setText(recommendation.reason)
        self.combat_summary_label.setText(recommendation.combat_summary)
        self.combat_summary_label.setVisible(bool(recommendation.combat_summary))
        details = (
            generic_probabilities,
            f"評分依據: {recommendation.scoring_basis} · {recommendation.scoring_profile_name}",
            *recommendation.breakdown,
        )
        self.breakdown_label.setText("\n".join(f"• {item}" for item in details))
        has_details = bool(recommendation.reason or recommendation.breakdown)
        self.breakdown_button.setEnabled(has_details)
        if not has_details:
            self.breakdown_button.setChecked(False)

    @Slot(object)
    def set_timeline(self, entries: object) -> None:
        if not isinstance(entries, (list, tuple)) or not all(
            isinstance(item, TimelineEntryView) for item in entries
        ):
            raise TypeError("entries must be a list or tuple of TimelineEntryView")
        self.timeline_model.set_entries(entries)

    @Slot(object)
    def append_timeline_entry(self, entry: object) -> None:
        if not isinstance(entry, TimelineEntryView):
            raise TypeError("entry must be TimelineEntryView")
        self.timeline_model.append_entry(entry)
        self.timeline_table.scrollToBottom()

    def set_monitors(self, options: Sequence[ChoiceOption]) -> None:
        _set_choices(self.monitor_combo, options, "尚未偵測顯示器")
        self._refresh_capture_controls()

    @Slot(str)
    def select_monitor(self, monitor_id: str) -> None:
        index = self.monitor_combo.findData(monitor_id)
        if index >= 0:
            self.monitor_combo.setCurrentIndex(index)

    def set_characters(self, options: Sequence[ChoiceOption]) -> None:
        previous_character_id = str(self.character_combo.currentData() or "")
        self._character_options = tuple(options)
        _set_choices(self.character_combo, options, "未指定角色", allow_empty=True)
        if previous_character_id:
            previous_index = self.character_combo.findData(previous_character_id)
            if previous_index >= 0:
                self.character_combo.setCurrentIndex(previous_index)
            elif len(options) == 1:
                self.character_combo.setCurrentIndex(0)
            else:
                empty_index = self.character_combo.findData("")
                if empty_index >= 0:
                    self.character_combo.setCurrentIndex(empty_index)
        elif len(options) == 1:
            self.character_combo.setCurrentIndex(0)
        else:
            empty_index = self.character_combo.findData("")
            if empty_index >= 0:
                self.character_combo.setCurrentIndex(empty_index)
        if hasattr(self, "result_character_combo"):
            previous_result = self.result_character_combo.currentData()
            _set_choices(
                self.result_character_combo,
                options,
                "尚未建立/選擇角色",
                allow_empty=True,
            )
            if previous_result and self.result_character_combo.findData(previous_result) < 0:
                self.result_character_combo.setCurrentIndex(
                    self.result_character_combo.findData("")
                )
            self._sync_result_character()
            self._update_character_link_enabled()
        if hasattr(self, "scoring_basis_label"):
            self._refresh_scoring_basis()

    def set_scoring_profiles(self, options: Sequence[ChoiceOption]) -> None:
        from wuwa_assistant.scoring.character_guides import AUTO_PROFILE_ID

        _set_choices(
            self.profile_combo,
            (ChoiceOption(AUTO_PROFILE_ID, "依所屬角色需求(自動)"), *options),
            "尚未建立評分設定檔",
        )
        if hasattr(self, "scoring_basis_label"):
            self._refresh_scoring_basis()

    def _refresh_scoring_basis(self, _index: int | None = None) -> None:
        if not hasattr(self, "scoring_basis_label"):
            return
        # The selected mode is a request, not evidence that the next observation
        # has a known owner. Only the completed evaluation names a character.
        profile_label = self._recommendation_profile_name
        if profile_label == "未指定評分設定檔" and self.profile_combo.currentData():
            profile_label = self.profile_combo.currentText().strip()
        basis = self._recommendation_basis
        if self._recommendation_profile_name == "未指定評分設定檔":
            basis = "等待角色與評分結果"
        self.scoring_basis_label.setText(f"評分: {basis}")
        self.scoring_basis_label.setToolTip(f"評分依據: {basis} · {profile_label}")

    @Slot(str)
    def select_scoring_profile(self, profile_id: str) -> None:
        index = self.profile_combo.findData(profile_id)
        if index >= 0:
            self.profile_combo.setCurrentIndex(index)

    @Slot(str)
    def select_result_character(self, character_id: str) -> None:
        index = self.result_character_combo.findData(character_id)
        if index >= 0:
            self.result_character_combo.setCurrentIndex(index)

    @Slot(object)
    def apply_scored_substats(self, stats: object) -> None:
        if not isinstance(stats, (list, tuple)) or not all(
            isinstance(stat, EchoStatView) for stat in stats
        ):
            raise TypeError("stats must be a list or tuple of EchoStatView")
        _fill_stat_table(self.sub_stats_table, stats)

    def submit_correction(self, field_path: str, corrected_value: str) -> None:
        """Validate and emit one audit-ready correction request."""

        observation = self._observation
        if observation is None:
            raise RuntimeError("no observation is available to correct")
        if field_path not in self._field_values:
            raise ValueError(f"field is not editable: {field_path}")
        request = CorrectionRequest(
            session_id=observation.session_id,
            observation_id=observation.observation_id,
            field_path=field_path,
            previous_value=self._field_values[field_path],
            corrected_value=corrected_value.strip(),
        )
        self.correction_requested.emit(request)

    def _request_start(self) -> None:
        if self.manual_mode and self._capture_state is CaptureState.WAITING_FOR_REQUEST:
            self.apply_capture_status(
                CaptureStatusView(CaptureState.RECOGNIZING, "正在讀取這次指定的聲骸畫面…")
            )
            self.recheck_requested.emit()
            return
        monitor_id = str(self.monitor_combo.currentData() or "")
        if not monitor_id:
            return
        self.apply_capture_status(CaptureStatusView(CaptureState.STARTING, "正在啟動…"))
        self.start_requested.emit(
            monitor_id,
            str(self.character_combo.currentData() or ""),
            str(self.profile_combo.currentData() or ""),
        )

    def _request_stop(self) -> None:
        self.apply_capture_status(
            CaptureStatusView(CaptureState.STOPPING, "正在安全停止並保存結構化紀錄…")
        )
        self.stop_requested.emit()

    def _request_disposition(self) -> None:
        if self._observation is None:
            return
        self.disposition_requested.emit(
            self._observation.session_id,
            str(self.disposition_combo.currentData()),
        )

    def _request_specific_disposition(self, disposition: ResultDisposition) -> None:
        if self._observation is None:
            return
        if disposition is ResultDisposition.KEPT and self.link_character_button.isEnabled():
            self._request_character_link()
            return
        self.result_status_label.setText("正在保存處理結果…")
        self.disposition_requested.emit(self._observation.session_id, disposition.value)

    def _request_character_link(self) -> None:
        if self._observation is None:
            return
        character_id = str(self.result_character_combo.currentData() or "")
        slot_index = int(self.result_slot_combo.currentData() or 0)
        if not character_id or not 1 <= slot_index <= 5:
            self.result_status_label.setText(
                "請先選擇角色與位置 1-5; 沒有資料時可先按「保留這顆」。"
            )
            return
        self.result_status_label.setText("正在關聯角色…")
        self.character_link_requested.emit(self._observation.session_id, character_id, slot_index)

    def _update_character_link_enabled(self, _index: int | None = None) -> None:
        # This action binds to the displayed observation's session, independently
        # of a newer score still being calculated. Never retarget it to the
        # presenter's latest session while retaining the previous visible result.
        self.link_character_button.setEnabled(
            self._observation is not None
            and bool(self.result_character_combo.currentData())
            and 1 <= int(self.result_slot_combo.currentData() or 0) <= 5
        )
        self.keep_button.setText(
            "保留並套用位置" if self.link_character_button.isEnabled() else "保留這顆"
        )

    def _sync_result_character(self) -> None:
        observation = self._observation
        if observation is None:
            return
        assignment = (observation.assigned_character_id, observation.assigned_slot_index)
        if assignment != self._shown_assignment:
            index = self.result_character_combo.findData(assignment[0] or "")
            if index < 0:
                return  # Wait for the asynchronous character roster refresh.
            self.result_character_combo.setCurrentIndex(index)
            self.result_slot_combo.setCurrentIndex(
                self.result_slot_combo.findData(
                    assignment[1] or observation.equipped_slot_index or 0
                )
            )
            self._shown_assignment = assignment
            self._suggested_character_id = assignment[0] or ""
        if observation.assigned_character_id:
            return
        current = str(self.result_character_combo.currentData() or "")
        if current not in {"", self._suggested_character_id}:
            return  # Keep an explicit unsaved user choice on repeated observations.
        owner = character_name_key(observation.equipped_character_name or "")
        matches = [
            item.identifier
            for item in self._character_options
            if owner and character_name_key(item.label) == owner
        ]
        suggested = matches[0] if len(matches) == 1 else ""
        self.result_character_combo.setCurrentIndex(self.result_character_combo.findData(suggested))
        self._suggested_character_id = suggested

    def _open_correction_dialog(self) -> None:
        if self._observation is None or not self._field_values:
            return
        dialog = CorrectionDialog(
            self._field_values, parent=self, issues=self._observation.review_issues
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.submit_correction(dialog.field_path, dialog.corrected_value)

    def _toggle_breakdown(self, checked: bool) -> None:
        self.reason_label.setVisible(checked)
        self.breakdown_label.setVisible(checked)
        self.breakdown_button.setText("收合計算理由" if checked else "展開計算理由")

    def _toggle_advanced_options(self, checked: bool) -> None:
        self.advanced_widget.setVisible(checked)
        self.advanced_toggle.setText("收合設定" if checked else "螢幕與評分設定")


class CorrectionDialog(QDialog):
    """Small manual-correction dialog; persistence/audit remains controller-owned."""

    def __init__(
        self,
        fields: dict[str, str],
        parent: QWidget | None = None,
        *,
        issues: tuple[FieldReviewIssue, ...] = (),
    ) -> None:
        if not fields:
            raise ValueError("at least one editable field is required")
        super().__init__(parent)
        self.setObjectName("EchoCorrectionDialog")
        self.setWindowTitle("修正 OCR 辨識結果")
        self.setModal(True)
        self.field_combo = QComboBox()
        self.field_combo.setObjectName("EchoCorrectionField")
        self._issues = {
            path: "; ".join(i.reason for i in issues if i.field_path == path) for path in fields
        }
        ordered = sorted(fields, key=lambda path: not bool(self._issues[path]))
        for field_path in ordered:
            label = _field_label(field_path)
            self.field_combo.addItem(
                f"待確認 · {label}" if self._issues[field_path] else label, field_path
            )
        self.issue_label = QLabel()
        self.issue_label.setObjectName("EchoCorrectionReason")
        self.issue_label.setWordWrap(True)
        self.previous_value_label = QLabel()
        self.previous_value_label.setObjectName("EchoCorrectionPreviousValue")
        self.corrected_value_edit = QLineEdit()
        self.corrected_value_edit.setObjectName("EchoCorrectionNewValue")

        form = QFormLayout()
        form.addRow("欄位", self.field_combo)
        form.addRow("確認原因", self.issue_label)
        form.addRow("原辨識值", self.previous_value_label)
        form.addRow("修正值", self.corrected_value_edit)
        warning = QLabel(
            "若數值正確, 可直接儲存以確認此欄位。其他待確認欄位仍會保留; "
            "原始 OCR 信心與修正紀錄均保留於本機。"
        )
        warning.setWordWrap(True)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.setObjectName("EchoCorrectionButtons")
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("確認此欄位")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(warning)
        layout.addWidget(buttons)
        self._fields = dict(fields)
        self.field_combo.currentIndexChanged.connect(self._load_current_value)
        self._load_current_value()

    @property
    def field_path(self) -> str:
        return str(self.field_combo.currentData())

    @property
    def corrected_value(self) -> str:
        return self.corrected_value_edit.text().strip()

    def _load_current_value(self) -> None:
        current = self._fields[self.field_path]
        self.issue_label.setText(
            self._issues[self.field_path] or "此欄位未標記問題, 仍可手動修正。"
        )
        self.previous_value_label.setText(current or "<空值>")
        self.corrected_value_edit.setText(current)
        self.corrected_value_edit.selectAll()

    def _accept_if_valid(self) -> None:
        if self.corrected_value or self.field_path.endswith("/unit"):
            self.accept()


def _stat_table(object_name: str) -> QTableView:
    table = QTableView()
    table.setObjectName(object_name)
    table.setModel(_StatTableModel(table))
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.verticalHeader().hide()
    table.verticalHeader().setDefaultSectionSize(29)
    table.horizontalHeader().setFixedHeight(28)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    table.setMinimumHeight(70)
    return table


class _StatTableModel(QAbstractTableModel):
    _HEADERS = ("屬性", "數值", "Roll", "得分", "信心")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._stats: list[EchoStatView] = []
        self._issues: dict[tuple[int, int], str] = {}

    def rowCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._stats)

    def columnCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._HEADERS)

    def data(self, index: ModelIndex, role: int = int(Qt.ItemDataRole.DisplayRole)) -> object:
        if not index.isValid() or not 0 <= index.row() < len(self._stats):
            return None
        stat = self._stats[index.row()]
        issue = self._issues.get((index.row(), index.column()))
        if role == int(Qt.ItemDataRole.BackgroundRole) and issue:
            return QColor("#483a26")
        if role == int(Qt.ItemDataRole.ForegroundRole) and issue:
            return QColor("#ffe0a3")
        if role == int(Qt.ItemDataRole.ToolTipRole):
            return (
                f"{stat.name or ''}\n{stat.character_value or ''}\n"
                f"信心 {_format_confidence(stat.confidence)}"
                + (f"\n待確認: {issue}" if issue else "")
            )
        if role != int(Qt.ItemDataRole.DisplayRole):
            return None
        values = (
            stat.name or "需要人工確認",
            stat.value_text,
            stat.roll_tier or "—",
            f"{stat.score_contribution:+.2f}" if stat.score_contribution is not None else "—",
            _format_confidence(stat.confidence),
        )
        return values[index.column()]

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = int(Qt.ItemDataRole.DisplayRole),
    ) -> object:
        if (
            role == int(Qt.ItemDataRole.DisplayRole)
            and orientation == Qt.Orientation.Horizontal
            and 0 <= section < len(self._HEADERS)
        ):
            return self._HEADERS[section]
        return None

    def set_stats(self, stats: Sequence[EchoStatView]) -> None:
        self.beginResetModel()
        self._stats = list(stats)
        self.endResetModel()

    def set_review_issues(self, prefix: str, issues: tuple[FieldReviewIssue, ...]) -> None:
        self._issues = {}
        for issue in issues:
            parts = issue.field_path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == prefix and parts[1].isdigit():
                key = (int(parts[1]), 0 if parts[2] == "name" else 1)
                self._issues[key] = (self._issues.get(key, "") + " " + issue.reason).strip()
        if self._stats:
            self.dataChanged.emit(
                self.index(0, 0), self.index(len(self._stats) - 1, len(self._HEADERS) - 1)
            )


def _fill_stat_table(table: QTableView, stats: Sequence[EchoStatView]) -> None:
    model = table.model()
    if not isinstance(model, _StatTableModel):
        raise TypeError("stat table has an unexpected model")
    model.set_stats(stats)


def _set_choices(
    combo: QComboBox,
    options: Sequence[ChoiceOption],
    empty_label: str,
    *,
    allow_empty: bool = False,
) -> None:
    previous = combo.currentData()
    combo.clear()
    for option in options:
        combo.addItem(option.label, option.identifier)
    if allow_empty or not options:
        combo.addItem(empty_label, "")
    if previous is not None:
        index = combo.findData(previous)
        if index >= 0:
            combo.setCurrentIndex(index)


def _observation_fields(observation: EchoObservationView) -> dict[str, str]:
    fields = {
        "/name": observation.name or "",
        "/set_name": observation.set_name or "",
        "/cost": "" if observation.cost is None else str(observation.cost),
        "/level": "" if observation.level is None else str(observation.level),
    }
    for prefix, stats in (
        ("main_stats", observation.main_stats),
        ("sub_stats", observation.sub_stats),
    ):
        for index, stat in enumerate(stats):
            fields[f"/{prefix}/{index}/name"] = stat.name or ""
            fields[f"/{prefix}/{index}/value"] = (
                "" if stat.value is None else _format_number(stat.value)
            )
            fields[f"/{prefix}/{index}/unit"] = stat.unit or ""
    return fields


def _field_label(field_path: str) -> str:
    labels = {
        "/name": "聲骸名稱",
        "/set_name": "合鳴套裝",
        "/cost": "COST",
        "/level": "等級",
    }
    if field_path in labels:
        return labels[field_path]
    prefix, index, part = field_path.removeprefix("/").split("/")
    group = "主屬性" if prefix == "main_stats" else "副詞條"
    part_label = {"name": "名稱", "value": "數值", "unit": "單位"}[part]
    return f"{group} {int(index) + 1} · {part_label}"


def _validate_confidence(value: float | None) -> None:
    if value is not None and (not isfinite(value) or not 0 <= value <= 1):
        raise ValueError("confidence/probability must be within 0..1 or null")


def _format_confidence(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def _format_probability(value: float | None) -> str:
    if value is None:
        return "—"
    if 0 < value < 0.001:
        return "<0.1%"
    if 0.999 < value < 1:
        return ">99.9%"
    return f"{value * 100:.1f}".rstrip("0").rstrip(".") + "%"


def _format_number(value: int | float) -> str:
    if isinstance(value, int) or value.is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _local_session_number(session_id: str) -> int | None:
    """Extract the capture-scoped ordinal without exposing internal identifiers."""

    prefix, separator, suffix = session_id.rpartition("-")
    if not separator or not prefix or not suffix.isdigit():
        return None
    number = int(suffix)
    return number if number > 0 else None


def _repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


__all__ = [
    "CaptureState",
    "CaptureStatusView",
    "ChoiceOption",
    "CorrectionDialog",
    "CorrectionRequest",
    "EchoLivePage",
    "EchoObservationView",
    "EchoStatView",
    "RecommendationView",
    "ResultDisposition",
    "TimelineEntryView",
    "TimelineTableModel",
]
