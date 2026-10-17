"""User-editable desktop settings that remain independent of persistence details."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, QTime, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from wuwa_assistant import __version__
from wuwa_assistant.display import (
    DEFAULT_RESOLUTION,
    DISPLAY_MODE_KEY,
    DISPLAY_MODES,
    PANEL_PRESET_KEY,
    PANEL_PRESETS,
    RESOLUTION_KEY,
    RESOLUTIONS,
)
from wuwa_assistant.ui.widgets.choice_selector import ChoiceSelector as QComboBox
from wuwa_assistant.ui.widgets.software_update import SoftwareUpdatePanel

from ..settings_store import (
    SettingsStore,
    setting_bool,
    setting_float,
    setting_int,
    setting_str,
)

MONITOR_KEY = "capture/monitor_id"
SAMPLE_RATE_KEY = "capture/sample_rate_hz"
STABILITY_WAIT_KEY = "capture/stability_wait_seconds"
OCR_CONFIDENCE_KEY = "ocr/confidence_threshold"
SAVE_LOW_CONFIDENCE_ROI_KEY = "debug/save_low_confidence_roi"
DEBUG_RETENTION_DAYS_KEY = "debug/retention_days"
PASS_THRESHOLD_KEY = "scoring/pass_threshold"
PERFECT_THRESHOLD_KEY = "scoring/perfect_threshold"
EXCEPTIONAL_THRESHOLD_KEY = "scoring/exceptional_threshold"
DEFAULT_PROFILE_KEY = "scoring/default_profile"
THEME_KEY = "ui/theme"
PUBLIC_AUTO_UPDATE_KEY = "public_data/auto_update"
BACKGROUND_REMINDERS_KEY = "reminders/background_enabled"
REMINDER_HOUR_KEY = "reminders/weekend_hour"
REMINDER_MINUTE_KEY = "reminders/weekend_minute"


@dataclass(frozen=True, slots=True)
class MonitorOption:
    identifier: str
    label: str


class SettingsPage(QWidget):
    """Basic V1 settings editor with inline validation and explicit save."""

    settings_saved = Signal(object)
    backup_requested = Signal()
    export_requested = Signal()
    import_requested = Signal()
    diagnostics_requested = Signal()
    demo_create_requested = Signal()
    demo_clear_requested = Signal()
    public_data_update_requested = Signal()
    background_reminders_requested = Signal(bool, int, int)

    def __init__(
        self,
        settings: SettingsStore,
        *,
        monitors: Sequence[MonitorOption] = (),
        data_root: str | Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("SettingsPage")
        self._settings = settings

        title = QLabel("設定")
        title.setObjectName("PageTitle")
        subtitle = QLabel("調整被動擷取、OCR、評分門檻與本機資料保留方式。")
        subtitle.setObjectName("PageDescription")

        self.monitor_combo = QComboBox()
        self.monitor_combo.setObjectName("MonitorCombo")
        self.set_monitors(monitors)

        self.display_mode_combo = QComboBox()
        self.display_mode_combo.setObjectName("DisplayModeCombo")
        for key, label in DISPLAY_MODES.items():
            self.display_mode_combo.addItem(label, key)
        self.resolution_combo = QComboBox()
        self.resolution_combo.setObjectName("CaptureResolutionCombo")
        for key in RESOLUTIONS:
            self.resolution_combo.addItem(key.replace("x", " x "), key)
        self.panel_preset_combo = QComboBox()
        self.panel_preset_combo.setObjectName("PanelPresetCombo")
        for key, label in PANEL_PRESETS.items():
            self.panel_preset_combo.addItem(label, key)

        self.sample_rate_spin = QDoubleSpinBox()
        self.sample_rate_spin.setObjectName("SampleRateSpin")
        self.sample_rate_spin.setRange(2.0, 8.0)
        self.sample_rate_spin.setSingleStep(0.5)
        self.sample_rate_spin.setDecimals(1)
        self.sample_rate_spin.setSuffix(" Hz")

        self.stability_wait_spin = QDoubleSpinBox()
        self.stability_wait_spin.setObjectName("StabilityWaitSpin")
        self.stability_wait_spin.setRange(0.25, 2.0)
        self.stability_wait_spin.setSingleStep(0.05)
        self.stability_wait_spin.setDecimals(2)
        self.stability_wait_spin.setSuffix(" 秒")

        capture_group = QGroupBox("被動擷取")
        capture_form = QFormLayout(capture_group)
        capture_form.addRow("使用方式", self.display_mode_combo)
        capture_form.addRow("擷取顯示器", self.monitor_combo)
        capture_form.addRow("遊戲畫面解析度", self.resolution_combo)
        capture_form.addRow("小面板位置", self.panel_preset_combo)
        display_hint = QLabel(
            "請讓無邊框遊戲填滿所選螢幕, 解析度須與該螢幕一致。\n"
            "變更模式、螢幕或解析度後會停止辨識, 請重新啟動。\n"
            "小面板啟動後持續顯示; 按住 Ctrl+Shift 可拖曳, 放開後滑鼠穿透。"
        )
        display_hint.setWordWrap(True)
        capture_form.addRow(display_hint)
        capture_form.addRow("取樣頻率", self.sample_rate_spin)
        capture_form.addRow("畫面穩定等待", self.stability_wait_spin)

        self.ocr_confidence_spin = QDoubleSpinBox()
        self.ocr_confidence_spin.setObjectName("OcrConfidenceSpin")
        self.ocr_confidence_spin.setRange(0.50, 0.99)
        self.ocr_confidence_spin.setSingleStep(0.01)
        self.ocr_confidence_spin.setDecimals(2)

        self.save_debug_check = QCheckBox("低信心時保存小尺寸 ROI debug 圖")
        self.save_debug_check.setObjectName("SaveDebugCheck")
        self.retention_days_spin = QSpinBox()
        self.retention_days_spin.setObjectName("RetentionDaysSpin")
        self.retention_days_spin.setRange(1, 365)
        self.retention_days_spin.setSuffix(" 天")

        ocr_group = QGroupBox("OCR 與 Debug")
        ocr_form = QFormLayout(ocr_group)
        ocr_form.addRow("人工確認門檻", self.ocr_confidence_spin)
        ocr_form.addRow("", self.save_debug_check)
        ocr_form.addRow("Debug 圖保留", self.retention_days_spin)

        self.pass_threshold_spin = _score_spin("PassThresholdSpin")
        self.perfect_threshold_spin = _score_spin("PerfectThresholdSpin")
        self.exceptional_threshold_spin = _score_spin("ExceptionalThresholdSpin")
        self.profile_combo = QComboBox()
        self.profile_combo.setObjectName("DefaultProfileCombo")
        self.profile_combo.addItem("通用輸出型", "generic_dps")
        self.profile_combo.addItem("治療/生命型", "healing_hp")
        self.profile_combo.addItem("防禦型", "defense")
        self.profile_combo.addItem("共鳴效率需求型", "energy_requirement")

        scoring_group = QGroupBox("評分")
        scoring_group.setVisible(False)
        scoring_group.setToolTip("評分門檻與模板已移至「評分設定檔」頁籤。")
        scoring_form = QFormLayout(scoring_group)
        scoring_form.addRow("及格門檻", self.pass_threshold_spin)
        scoring_form.addRow("完美門檻", self.perfect_threshold_spin)
        scoring_form.addRow("極品門檻", self.exceptional_threshold_spin)
        scoring_form.addRow("預設評分模板", self.profile_combo)

        self.theme_combo = QComboBox()
        self.theme_combo.setObjectName("ThemeCombo")
        self.theme_combo.addItem("深色", "dark")
        appearance_group = QGroupBox("外觀")
        appearance_form = QFormLayout(appearance_group)
        appearance_form.addRow("主題", self.theme_combo)

        public_data_group = QGroupBox("Asia 區服・公開資料與週末提醒")
        public_data_layout = QVBoxLayout(public_data_group)
        public_data_explanation = QLabel(
            "卡池與活動依 Asia 區服時間(UTC+8)顯示。"
            "連網只讀取公開資料,失敗時顯示上次快取與更新時間。"
        )
        public_data_explanation.setWordWrap(True)
        self.public_auto_update_check = QCheckBox("啟動時及每 6 小時自動更新公開資料")
        self.background_reminders_check = QCheckBox("程式關閉後仍提供週末 Windows 提醒")
        self.reminder_time_edit = QTimeEdit(QTime(10, 0))
        self.reminder_time_edit.setObjectName("ReminderTimeEdit")
        self.reminder_time_edit.setDisplayFormat("HH:mm")
        self.reminder_time_edit.setMaximumWidth(160)
        self.reminder_time_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.reminder_time_edit.setWrapping(True)
        self.reminder_time_edit.setToolTip("選取時或分, 使用上下按鈕或方向鍵調整 (Asia / UTC+8)")
        self.reminder_apply_button = QPushButton("套用背景提醒設定")
        self.reminder_apply_button.clicked.connect(
            lambda: self.background_reminders_requested.emit(
                self.background_reminders_check.isChecked(),
                self.reminder_time_edit.time().hour(),
                self.reminder_time_edit.time().minute(),
            )
        )
        self.reminder_status_label = QLabel(
            "週六提醒;週日補提醒未完成項目。電腦需開機且已登入;完成勾選在活動頁。"
        )
        self.reminder_status_label.setWordWrap(True)
        reminder_form = QFormLayout()
        reminder_form.addRow(self.background_reminders_check)
        reminder_form.addRow("週六、週日提醒時間 (UTC+8)", self.reminder_time_edit)
        reminder_form.addRow(self.reminder_apply_button)
        reminder_form.addRow(self.reminder_status_label)
        self.public_data_update_button = QPushButton("更新官方公開資料")
        self.public_data_update_button.setObjectName("PublicDataUpdateButton")
        self.public_data_status_label = QLabel("尚未讀取本機官方資料快取。")
        self.public_data_status_label.setObjectName("PublicDataStatus")
        self.public_data_status_label.setWordWrap(True)
        self.public_data_update_button.clicked.connect(self.public_data_update_requested.emit)
        public_data_actions = QHBoxLayout()
        public_data_actions.addWidget(self.public_data_update_button)
        public_data_actions.addWidget(self.public_data_status_label, 1)
        public_data_layout.addWidget(public_data_explanation)
        public_data_layout.addWidget(self.public_auto_update_check)
        public_data_layout.addLayout(public_data_actions)
        public_data_layout.addLayout(reminder_form)

        self.data_root_label = QLabel(
            "尚未提供" if data_root is None else str(Path(data_root).resolve())
        )
        self.data_root_label.setObjectName("DataRootPath")
        self.data_root_label.setWordWrap(True)
        self.data_root_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.backup_button = QPushButton("建立 SQLite 備份")
        self.backup_button.setObjectName("BackupDatabaseButton")
        self.export_button = QPushButton("匯出 JSON 備份")
        self.export_button.setObjectName("ExportDataButton")
        self.import_button = QPushButton("驗證並匯入")
        self.import_button.setObjectName("ImportDataButton")
        self.diagnostics_button = QPushButton("預覽診斷包")
        self.diagnostics_button.setObjectName("DiagnosticsButton")
        self.demo_create_button = QPushButton("建立示範資料")
        self.demo_create_button.setObjectName("CreateDemoDataButton")
        self.demo_clear_button = QPushButton("清除示範資料")
        self.demo_clear_button.setObjectName("ClearDemoDataButton")
        self.backup_button.clicked.connect(self.backup_requested.emit)
        self.export_button.clicked.connect(self.export_requested.emit)
        self.import_button.clicked.connect(self.import_requested.emit)
        self.diagnostics_button.clicked.connect(self.diagnostics_requested.emit)
        self.demo_create_button.clicked.connect(self.demo_create_requested.emit)
        self.demo_clear_button.clicked.connect(self.demo_clear_requested.emit)
        backup_actions = QHBoxLayout()
        backup_actions.addWidget(self.backup_button)
        backup_actions.addWidget(self.export_button)
        backup_actions.addWidget(self.import_button)
        diagnostic_actions = QHBoxLayout()
        diagnostic_actions.addWidget(self.diagnostics_button)
        diagnostic_actions.addWidget(self.demo_create_button)
        diagnostic_actions.addWidget(self.demo_clear_button)
        data_group = QGroupBox("本機資料與備份")
        data_layout = QVBoxLayout(data_group)
        data_layout.addWidget(QLabel("資料路徑"))
        data_layout.addWidget(self.data_root_label)
        data_layout.addLayout(backup_actions)
        data_layout.addLayout(diagnostic_actions)

        about_group = QGroupBox("關於")
        about_layout = QVBoxLayout(about_group)
        self.version_label = QLabel(f"Wuthering Waves Personal Assistant {__version__}")
        self.version_label.setObjectName("ApplicationVersion")
        about_layout.addWidget(self.version_label)
        about_layout.addWidget(QLabel("鳴潮個人養成助理 - 單機、離線優先、被動辨識。"))

        safety_group = QFrame()
        safety_group.setObjectName("SafetyCard")
        safety_layout = QVBoxLayout(safety_group)
        safety_title = QLabel("安全邊界")
        safety_title.setObjectName("SafetyTitle")
        safety_text = QLabel(
            "本工具只被動讀取畫面, 使用獨立小面板顯示結果; "
            "不操作遊戲、不讀取遊戲記憶體、不注入、不附掛遊戲視窗。"
        )
        safety_text.setWordWrap(True)
        safety_layout.addWidget(safety_title)
        safety_layout.addWidget(safety_text)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 12, 0)
        content_layout.setSpacing(16)
        self.software_update = SoftwareUpdatePanel()
        content_layout.addWidget(self.software_update)
        content_layout.addWidget(capture_group)
        content_layout.addWidget(ocr_group)
        content_layout.addWidget(scoring_group)
        content_layout.addWidget(appearance_group)
        content_layout.addWidget(public_data_group)
        content_layout.addWidget(data_group)
        content_layout.addWidget(about_group)
        content_layout.addWidget(safety_group)
        content_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setObjectName("SettingsScrollArea")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)

        self.status_label = QLabel()
        self.status_label.setObjectName("SettingsStatus")
        self.status_label.setWordWrap(True)
        self.save_button = QPushButton("儲存設定")
        self.save_button.setObjectName("SettingsSaveButton")
        self.save_button.clicked.connect(self.save)
        reset_button = QPushButton("恢復預設值")
        reset_button.setObjectName("SettingsResetButton")
        reset_button.clicked.connect(self.reset_controls)

        actions = QHBoxLayout()
        actions.addWidget(self.status_label, 1)
        actions.addWidget(reset_button)
        actions.addWidget(self.save_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 36, 40, 24)
        layout.setSpacing(12)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addWidget(scroll, 1)
        layout.addLayout(actions)

        self.load()

    def set_monitors(self, monitors: Sequence[MonitorOption]) -> None:
        selected = self.monitor_combo.currentData() if self.monitor_combo.count() else None
        preferred = (
            selected
            if isinstance(selected, str) and selected
            else setting_str(self._settings.get(MONITOR_KEY), "")
        )
        self.monitor_combo.clear()
        if monitors:
            for monitor in monitors:
                self.monitor_combo.addItem(monitor.label, monitor.identifier)
        else:
            self.monitor_combo.addItem("尚未偵測顯示器", "")
        if isinstance(preferred, str):
            selected_index = self.monitor_combo.findData(preferred)
            if selected_index >= 0:
                self.monitor_combo.setCurrentIndex(selected_index)

    def load(self) -> None:
        self.public_auto_update_check.setChecked(
            setting_bool(self._settings.get(PUBLIC_AUTO_UPDATE_KEY), False)
        )
        self.background_reminders_check.setChecked(
            setting_bool(self._settings.get(BACKGROUND_REMINDERS_KEY), False)
        )
        self.reminder_time_edit.setTime(
            QTime(
                setting_int(self._settings.get(REMINDER_HOUR_KEY), 10),
                setting_int(self._settings.get(REMINDER_MINUTE_KEY), 0),
            )
        )
        monitor_id = setting_str(self._settings.get(MONITOR_KEY), "")
        _select_data(
            self.display_mode_combo, setting_str(self._settings.get(DISPLAY_MODE_KEY), "dual")
        )
        _select_data(
            self.resolution_combo,
            setting_str(self._settings.get(RESOLUTION_KEY), DEFAULT_RESOLUTION),
        )
        _select_data(
            self.panel_preset_combo, setting_str(self._settings.get(PANEL_PRESET_KEY), "top_left")
        )
        monitor_index = self.monitor_combo.findData(monitor_id)
        if monitor_index >= 0:
            self.monitor_combo.setCurrentIndex(monitor_index)

        self.sample_rate_spin.setValue(setting_float(self._settings.get(SAMPLE_RATE_KEY), 5.0))
        self.stability_wait_spin.setValue(
            setting_float(self._settings.get(STABILITY_WAIT_KEY), 0.60)
        )
        self.ocr_confidence_spin.setValue(
            setting_float(self._settings.get(OCR_CONFIDENCE_KEY), 0.85)
        )
        self.save_debug_check.setChecked(
            setting_bool(self._settings.get(SAVE_LOW_CONFIDENCE_ROI_KEY), False)
        )
        self.retention_days_spin.setValue(
            setting_int(self._settings.get(DEBUG_RETENTION_DAYS_KEY), 14)
        )
        self.pass_threshold_spin.setValue(setting_int(self._settings.get(PASS_THRESHOLD_KEY), 70))
        self.perfect_threshold_spin.setValue(
            setting_int(self._settings.get(PERFECT_THRESHOLD_KEY), 88)
        )
        self.exceptional_threshold_spin.setValue(
            setting_int(self._settings.get(EXCEPTIONAL_THRESHOLD_KEY), 95)
        )
        _select_data(
            self.profile_combo,
            setting_str(self._settings.get(DEFAULT_PROFILE_KEY), "generic_dps"),
        )
        _select_data(self.theme_combo, setting_str(self._settings.get(THEME_KEY), "dark"))
        self.status_label.clear()

    def reset_controls(self) -> None:
        _select_data(self.display_mode_combo, "dual")
        _select_data(self.resolution_combo, DEFAULT_RESOLUTION)
        _select_data(self.panel_preset_combo, "top_left")
        self.sample_rate_spin.setValue(5.0)
        self.stability_wait_spin.setValue(0.60)
        self.ocr_confidence_spin.setValue(0.85)
        self.save_debug_check.setChecked(False)
        self.retention_days_spin.setValue(14)
        self.pass_threshold_spin.setValue(70)
        self.perfect_threshold_spin.setValue(88)
        self.exceptional_threshold_spin.setValue(95)
        _select_data(self.profile_combo, "generic_dps")
        _select_data(self.theme_combo, "dark")
        self.status_label.setText("已載入預設值; 按下「儲存設定」才會套用。")

    def save(self) -> None:
        passing = self.pass_threshold_spin.value()
        perfect = self.perfect_threshold_spin.value()
        exceptional = self.exceptional_threshold_spin.value()
        if not 0 <= passing < perfect < exceptional <= 100:
            self.status_label.setText("門檻需依序為: 及格 < 完美 < 極品。")
            self.status_label.setProperty("error", True)
            self.status_label.style().unpolish(self.status_label)
            self.status_label.style().polish(self.status_label)
            return

        values: dict[str, object] = {
            DISPLAY_MODE_KEY: str(self.display_mode_combo.currentData()),
            RESOLUTION_KEY: str(self.resolution_combo.currentData()),
            PANEL_PRESET_KEY: str(self.panel_preset_combo.currentData()),
            PUBLIC_AUTO_UPDATE_KEY: self.public_auto_update_check.isChecked(),
            MONITOR_KEY: str(self.monitor_combo.currentData() or ""),
            SAMPLE_RATE_KEY: self.sample_rate_spin.value(),
            STABILITY_WAIT_KEY: self.stability_wait_spin.value(),
            OCR_CONFIDENCE_KEY: self.ocr_confidence_spin.value(),
            SAVE_LOW_CONFIDENCE_ROI_KEY: self.save_debug_check.isChecked(),
            DEBUG_RETENTION_DAYS_KEY: self.retention_days_spin.value(),
            PASS_THRESHOLD_KEY: passing,
            PERFECT_THRESHOLD_KEY: perfect,
            EXCEPTIONAL_THRESHOLD_KEY: exceptional,
            DEFAULT_PROFILE_KEY: str(self.profile_combo.currentData()),
            THEME_KEY: str(self.theme_combo.currentData()),
        }
        for key, value in values.items():
            self._settings.set(key, value)
        self._settings.sync()
        self.status_label.setProperty("error", False)
        self.status_label.setText("設定已儲存。")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        self.settings_saved.emit(values)

    def set_data_operation_status(self, message: str, *, error: bool = False) -> None:
        """Show the result of an explicit local data operation."""

        self.status_label.setProperty("error", error)
        self.status_label.setText(message)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def set_data_actions_enabled(self, enabled: bool) -> None:
        for button in (
            self.backup_button,
            self.export_button,
            self.import_button,
            self.diagnostics_button,
            self.demo_create_button,
            self.demo_clear_button,
        ):
            button.setEnabled(enabled)

    def set_public_data_sync_state(
        self,
        message: str,
        *,
        busy: bool = False,
        error: bool = False,
    ) -> None:
        """Show state for the explicit public-data operation only."""

        self.public_data_update_button.setEnabled(not busy)
        self.public_data_status_label.setText(message)
        self.public_data_status_label.setProperty("error", error)
        self.public_data_status_label.style().unpolish(self.public_data_status_label)
        self.public_data_status_label.style().polish(self.public_data_status_label)


def _score_spin(object_name: str) -> QSpinBox:
    spin = QSpinBox()
    spin.setObjectName(object_name)
    spin.setRange(0, 100)
    spin.setSuffix(" 分")
    return spin


def _select_data(combo: QComboBox, value: str) -> None:
    index = combo.findData(value)
    if index >= 0:
        combo.setCurrentIndex(index)
