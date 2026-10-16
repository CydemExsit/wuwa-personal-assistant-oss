"""Short Traditional-Chinese first-run flow for safe passive capture."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Signal, Slot
from PySide6.QtWidgets import (
    QComboBox as MonitorComboBox,
)
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from wuwa_assistant.display import (
    APP_TITLE,
    DEFAULT_RESOLUTION,
    DISPLAY_MODES,
    RESOLUTIONS,
    DisplayPreferences,
)
from wuwa_assistant.ui.widgets.choice_selector import ChoiceSelector as QComboBox


@dataclass(frozen=True, slots=True)
class MonitorChoice:
    identifier: str
    label: str
    width: int = 0
    height: int = 0
    is_primary: bool = False

    def __post_init__(self) -> None:
        if not self.identifier.strip() or not self.label.strip():
            raise ValueError("monitor choice identifier and label must not be blank")


@dataclass(frozen=True, slots=True)
class CaptureTestRequest:
    monitor_identifier: str
    resolution: str = DEFAULT_RESOLUTION

    def __post_init__(self) -> None:
        if not self.monitor_identifier.strip():
            raise ValueError("monitor_identifier must not be blank")
        DisplayPreferences(resolution=self.resolution)


@dataclass(frozen=True, slots=True)
class OnboardingRequest:
    """Backward-compatible service request produced by the short UI flow.

    Safety and the application-owned data path are still asserted at the service
    boundary.  They are no longer checkbox gates that a first-time user must
    understand before seeing the useful part of the application.
    """

    monitor_identifier: str
    data_path: Path
    initial_mode: str = "quick_start"
    character_name: str | None = None
    safety_confirmed: bool = True
    capture_test_confirmed: bool = False
    data_path_confirmed: bool = True
    scoring_template_kind: str = "generic_dps"
    display_mode: str = "dual"
    resolution: str = DEFAULT_RESOLUTION

    def __post_init__(self) -> None:
        DisplayPreferences(mode=self.display_mode, resolution=self.resolution)
        if not self.monitor_identifier.strip():
            raise ValueError("請選擇要被動擷取的顯示器")
        if not self.data_path.is_absolute():
            raise ValueError("本機資料路徑必須是絕對路徑")
        if self.initial_mode not in {"quick_start", "first_character", "demo_data"}:
            raise ValueError("首次資料模式不正確")
        if not self.safety_confirmed:
            raise ValueError("安全邊界確認狀態不正確")
        if not self.capture_test_confirmed:
            raise ValueError("請先測試所選螢幕")
        if not self.data_path_confirmed:
            raise ValueError("本機資料位置確認狀態不正確")
        if self.initial_mode == "first_character" and not (self.character_name or "").strip():
            raise ValueError("請輸入第一名角色名稱")
        if self.scoring_template_kind not in {
            "generic_dps",
            "healing_hp",
            "defense",
            "energy_requirement",
        }:
            raise ValueError("請選擇有效的評分模板")


class _MonitorPage(QWizardPage):
    test_requested = Signal(object)

    def __init__(self, choices: Sequence[MonitorChoice]) -> None:
        super().__init__()
        self.setTitle("選擇遊戲所在螢幕")
        self.setSubTitle("請依照左側/右側與「主要螢幕」標示選擇; 測試不會保存畫面。")

        self.combo = MonitorComboBox()
        self.combo.setMinimumContentsLength(18)
        self.combo.setSizeAdjustPolicy(
            MonitorComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.combo.setObjectName("OnboardingMonitorCombo")
        self.resolution_combo = QComboBox()
        self.resolution_combo.setObjectName("OnboardingResolutionCombo")
        for key in RESOLUTIONS:
            self.resolution_combo.addItem(key.replace("x", " x "), key)
        self.resolution_combo.setCurrentIndex(self.resolution_combo.findData(DEFAULT_RESOLUTION))
        self.test_button = QPushButton("測試這個螢幕")
        self.test_button.setObjectName("OnboardingCaptureTestButton")
        self.test_button.setProperty("role", "primary")

        self.status_label = QLabel("先選擇螢幕, 再按下測試。")
        self.status_label.setObjectName("OnboardingCaptureStatus")
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumHeight(54)
        self.status_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

        hint = QLabel("測試只讀取記憶體中的聲骸區域, 不儲存畫面。")
        hint.setObjectName("PageDescription")
        hint.setWordWrap(True)

        controls = QHBoxLayout()
        controls.addWidget(self.combo, 1)
        controls.addWidget(self.test_button)
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.addLayout(controls)
        resolution_form = QFormLayout()
        resolution_form.addRow("遊戲畫面解析度", self.resolution_combo)
        layout.addLayout(resolution_form)
        resolution_hint = QLabel("無邊框遊戲須填滿此螢幕, 請選擇與該螢幕一致的解析度。")
        resolution_hint.setWordWrap(True)
        layout.addWidget(resolution_hint)
        layout.addWidget(self.status_label)
        layout.addWidget(hint)
        layout.addStretch(1)

        self._tested_identifier: str | None = None
        self.combo.currentIndexChanged.connect(self._selection_changed)
        self.resolution_combo.currentIndexChanged.connect(self._selection_changed)
        self.test_button.clicked.connect(self._request_test)
        self.set_choices(choices)

    def set_choices(self, choices: Sequence[MonitorChoice]) -> None:
        self._choices = {choice.identifier: choice for choice in choices}
        selected = self.selected_identifier()
        self.combo.blockSignals(True)
        self.combo.clear()
        if choices:
            for choice in choices:
                self.combo.addItem(choice.label, choice.identifier)
        else:
            self.combo.addItem("正在偵測螢幕…", "")
        if selected:
            index = self.combo.findData(selected)
            if index >= 0:
                self.combo.setCurrentIndex(index)
        if selected not in self._choices and choices:
            supported = [
                item for item in choices if (item.width, item.height) in RESOLUTIONS.values()
            ]
            preferred = next((item for item in supported if item.is_primary), None)
            preferred = preferred or next(iter(supported), choices[0])
            self.combo.setCurrentIndex(self.combo.findData(preferred.identifier))
        self.combo.blockSignals(False)
        self._selection_changed()

    def selected_identifier(self) -> str:
        value = self.combo.currentData()
        return value if isinstance(value, str) else ""

    def isComplete(self) -> bool:
        selected = self.selected_identifier()
        return bool(selected) and selected == self._tested_identifier

    @Slot(str, bool, str)
    def set_test_result(
        self, monitor_identifier: str, success: bool, message: str, resolution: str | None = None
    ) -> None:
        if monitor_identifier != self.selected_identifier():
            return
        if resolution is not None and resolution != self.resolution_combo.currentData():
            return
        self._tested_identifier = monitor_identifier if success else None
        self.status_label.setText(message.strip() or ("測試成功" if success else "測試失敗"))
        self.status_label.setProperty("error", not success)
        self.status_label.setProperty("captureTest", "success" if success else "error")
        _repolish(self.status_label)
        self.completeChanged.emit()

    def set_discovery_error(self, message: str) -> None:
        self.set_choices(())
        self.combo.setItemText(0, "沒有可用的螢幕")
        self.status_label.setText(message.strip() or "無法偵測螢幕")
        self.status_label.setProperty("error", True)
        self.status_label.setProperty("captureTest", "error")
        _repolish(self.status_label)
        self.completeChanged.emit()

    def _selection_changed(self, _index: int = -1) -> None:
        self._tested_identifier = None
        choice = self._choices.get(self.selected_identifier())
        if choice and self.sender() is not self.resolution_combo:
            resolution = f"{choice.width}x{choice.height}"
            if resolution in RESOLUTIONS:
                self.resolution_combo.blockSignals(True)
                self.resolution_combo.setCurrentIndex(self.resolution_combo.findData(resolution))
                self.resolution_combo.blockSignals(False)
        self.status_label.setText("按下「測試這個螢幕」, 確認是否選到遊戲畫面。")
        self.status_label.setProperty("error", False)
        self.status_label.setProperty("captureTest", "idle")
        self.test_button.setEnabled(bool(self.selected_identifier()))
        _repolish(self.status_label)
        self.completeChanged.emit()

    def _request_test(self) -> None:
        identifier = self.selected_identifier()
        if not identifier:
            return
        self._tested_identifier = None
        self.status_label.setText("正在測試被動擷取與本機頁面辨識…")
        self.status_label.setProperty("error", False)
        self.status_label.setProperty("captureTest", "running")
        self.test_button.setEnabled(False)
        _repolish(self.status_label)
        self.completeChanged.emit()
        self.test_requested.emit(
            CaptureTestRequest(identifier, str(self.resolution_combo.currentData()))
        )


class OnboardingWizard(QWizard):
    """Three-page quick start; character and scoring setup intentionally come later."""

    capture_test_requested = Signal(object)
    completion_requested = Signal(object)

    def __init__(
        self,
        *,
        data_path: Path,
        monitors: Sequence[MonitorChoice] = (),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._data_path = data_path.expanduser().resolve()
        self._submitting = False
        self.setObjectName("OnboardingWizard")
        self.setWindowTitle(f"{APP_TITLE} - 快速開始")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setMinimumSize(640, 480)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage)
        self._mode_chosen = False
        self._mode_initialized = False
        self.display_mode_combo = QComboBox()
        self.display_mode_combo.setObjectName("OnboardingDisplayModeCombo")
        for key, label in DISPLAY_MODES.items():
            self.display_mode_combo.addItem(label, key)
        self.display_mode_combo.currentIndexChanged.connect(self._mode_changed)

        self.monitor_page = _MonitorPage(monitors)
        self.monitor_page.setTitle("確認螢幕, 就能開始")
        self.monitor_page.setSubTitle(
            "已自動帶入建議設定; 可修改模式、螢幕與解析度。之後也能在設定切換。"
        )
        layout = self.monitor_page.layout()
        assert isinstance(layout, QVBoxLayout)
        mode_form = QFormLayout()
        mode_form.addRow("使用方式", self.display_mode_combo)
        layout.insertLayout(0, mode_form)
        safety = QLabel(
            "只被動讀取畫面, 不操作遊戲、不讀取遊戲記憶體、不上傳畫面。"
            "單螢幕使用工具自己的獨立小面板。"
        )
        safety.setObjectName("OnboardingSafetyText")
        safety.setWordWrap(True)
        layout.addWidget(safety)
        self.completion_status_label = QLabel()
        self.completion_status_label.setObjectName("OnboardingCompletionStatus")
        self.completion_status_label.setWordWrap(True)
        layout.addWidget(self.completion_status_label)
        # Keep wrapped errors readable at high DPI and on 1080p screens. The
        # wizard's action buttons stay outside the scrollable content.
        content = QWidget()
        content.setLayout(layout)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        scroll = QScrollArea()
        scroll.setObjectName("OnboardingScrollArea")
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        page_layout = QVBoxLayout(self.monitor_page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.addWidget(scroll)
        self.addPage(self.monitor_page)
        self.monitor_page.test_requested.connect(self.capture_test_requested.emit)
        self.setButtonText(QWizard.WizardButton.FinishButton, "進入工具")
        self.setButtonText(QWizard.WizardButton.CancelButton, "稍後再開")
        self.button(QWizard.WizardButton.FinishButton).setProperty("role", "primary")
        self._set_default_mode(monitors)

    def _mode_changed(self, _index: int) -> None:
        self._mode_chosen = True

    def _set_default_mode(self, choices: Sequence[MonitorChoice]) -> None:
        if choices and not self._mode_chosen and not self._mode_initialized:
            self.display_mode_combo.blockSignals(True)
            self.display_mode_combo.setCurrentIndex(
                self.display_mode_combo.findData("single" if len(choices) == 1 else "dual")
            )
            self.display_mode_combo.blockSignals(False)
            self._mode_initialized = True

    def set_monitors(self, choices: Sequence[MonitorChoice]) -> None:
        self.monitor_page.set_choices(choices)
        self._set_default_mode(choices)

    @Slot(str)
    def set_monitor_discovery_error(self, message: str) -> None:
        self.monitor_page.set_discovery_error(message)

    @Slot(str, bool, str)
    def set_capture_test_result(
        self,
        monitor_identifier: str,
        success: bool,
        message: str,
        resolution: str | None = None,
    ) -> None:
        self.monitor_page.set_test_result(monitor_identifier, success, message, resolution)
        self.monitor_page.test_button.setEnabled(bool(self.monitor_page.selected_identifier()))

    def request(self) -> OnboardingRequest:
        return OnboardingRequest(
            monitor_identifier=self.monitor_page.selected_identifier(),
            data_path=self._data_path,
            capture_test_confirmed=self.monitor_page.isComplete(),
            display_mode=str(self.display_mode_combo.currentData()),
            resolution=str(self.monitor_page.resolution_combo.currentData()),
        )

    def accept(self) -> None:
        if self._submitting:
            return
        try:
            request = self.request()
        except ValueError as exc:
            self.show_error(str(exc))
            return
        self._submitting = True
        self.completion_status_label.setText("正在建立本機快速設定…")
        self.completion_status_label.setProperty("error", False)
        self.button(QWizard.WizardButton.FinishButton).setEnabled(False)
        self.completion_requested.emit(request)

    @Slot(str)
    def complete_success(self, message: str = "快速設定已完成。") -> None:
        self.completion_status_label.setText(message)
        self.completion_status_label.setProperty("error", False)
        super().accept()

    @Slot(str)
    def show_error(self, message: str) -> None:
        self._submitting = False
        self.completion_status_label.setText(message)
        self.completion_status_label.setProperty("error", True)
        self.button(QWizard.WizardButton.FinishButton).setEnabled(True)


def _repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


__all__ = [
    "CaptureTestRequest",
    "MonitorChoice",
    "OnboardingRequest",
    "OnboardingWizard",
]
