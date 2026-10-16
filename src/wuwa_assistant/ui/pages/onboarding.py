"""Short Traditional-Chinese first-run flow for safe passive capture."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Signal, Slot
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
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

        self.combo = QComboBox()
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

        hint = QLabel(
            "測試只會在記憶體中讀取固定的小範圍, 辨識目前是哪種聲骸頁面; "
            "完整畫面會立即釋放, 不會寫入硬碟。"
        )
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
        self.setMinimumSize(720, 500)

        self.welcome_page = QWizardPage()
        self.welcome_page.setTitle("先讓聲骸辨識可以使用")
        self.welcome_page.setSubTitle("選擇使用方式與遊戲解析度; 之後可隨時到設定切換。")
        self.display_mode_combo = QComboBox()
        self.display_mode_combo.setObjectName("OnboardingDisplayModeCombo")
        for key, label in DISPLAY_MODES.items():
            self.display_mode_combo.addItem(label, key)
        welcome_text = QLabel(
            "下一頁會列出你的螢幕。選擇遊戲所在的左側或右側螢幕, 再按一次測試即可進入工具。"
        )
        welcome_text.setWordWrap(True)

        safety_card = QFrame()
        safety_card.setObjectName("SafetyCard")
        safety_layout = QVBoxLayout(safety_card)
        safety_title = QLabel("安全邊界")
        safety_title.setObjectName("SafetyTitle")
        safety_text = QLabel(
            "本工具只被動讀取畫面; 單螢幕模式使用獨立小面板顯示結果。\n"
            "不操作遊戲、不讀取遊戲記憶體、不注入。\n"
            "不模擬輸入、不上傳畫面、不錄影, 也不保存完整即時畫面。"
        )
        safety_text.setObjectName("OnboardingSafetyText")
        safety_text.setWordWrap(True)
        safety_layout.addWidget(safety_title)
        safety_layout.addWidget(safety_text)

        welcome_layout = QVBoxLayout(self.welcome_page)
        welcome_layout.setSpacing(18)
        welcome_layout.addWidget(welcome_text)
        display_form = QFormLayout()
        display_form.addRow("使用方式", self.display_mode_combo)
        welcome_layout.addLayout(display_form)
        welcome_layout.addWidget(safety_card)
        welcome_layout.addStretch(1)

        self.monitor_page = _MonitorPage(monitors)

        self.summary_page = QWizardPage()
        self.summary_page.setTitle("可以開始了")
        self.summary_page.setSubTitle("完成後會進入總覽, 工具不會自動開始擷取。")
        self.summary_label = QLabel()
        self.summary_label.setObjectName("OnboardingSummary")
        self.summary_label.setWordWrap(True)
        self.completion_status_label = QLabel()
        self.completion_status_label.setObjectName("OnboardingCompletionStatus")
        self.completion_status_label.setWordWrap(True)
        later_card = QFrame()
        later_card.setObjectName("MetricCard")
        later_layout = QVBoxLayout(later_card)
        later_title = QLabel("稍後設定")
        later_title.setObjectName("MetricTitle")
        later_text = QLabel(
            "角色、評分模板、示範資料、備份位置與線上公開資料都可進入工具後再處理。"
        )
        later_text.setWordWrap(True)
        later_layout.addWidget(later_title)
        later_layout.addWidget(later_text)
        summary_layout = QVBoxLayout(self.summary_page)
        summary_layout.setSpacing(18)
        summary_layout.addWidget(self.summary_label)
        summary_layout.addWidget(later_card)
        summary_layout.addWidget(self.completion_status_label)
        summary_layout.addStretch(1)

        for page in (self.welcome_page, self.monitor_page, self.summary_page):
            self.addPage(page)

        self.monitor_page.test_requested.connect(self.capture_test_requested.emit)
        self.currentIdChanged.connect(lambda _page_id: self._update_summary())
        self.setButtonText(QWizard.WizardButton.BackButton, "上一步")
        self.setButtonText(QWizard.WizardButton.NextButton, "下一步")
        self.setButtonText(QWizard.WizardButton.FinishButton, "進入工具")
        self.setButtonText(QWizard.WizardButton.CancelButton, "取消")
        self.button(QWizard.WizardButton.NextButton).setProperty("role", "primary")
        self.button(QWizard.WizardButton.FinishButton).setProperty("role", "primary")
        self._update_summary()

    def set_monitors(self, choices: Sequence[MonitorChoice]) -> None:
        self.monitor_page.set_choices(choices)

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
        self._update_summary()

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

    def _update_summary(self) -> None:
        monitor = self.monitor_page.combo.currentText() or "尚未選擇"
        self.summary_label.setText(
            f"遊戲螢幕: {monitor}\n\n"
            f"使用方式: {self.display_mode_combo.currentText()}\n"
            f"解析度: {self.monitor_page.resolution_combo.currentText()}\n\n"
            "進入後請前往「聲骸強化」, 遊戲切到聲骸強化頁, 再手動按下「開始辨識」。"
        )


def _repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


__all__ = [
    "CaptureTestRequest",
    "MonitorChoice",
    "OnboardingRequest",
    "OnboardingWizard",
]
