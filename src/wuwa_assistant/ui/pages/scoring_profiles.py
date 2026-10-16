"""Traditional-Chinese scoring-profile editor and score presentation views.

This module is intentionally a passive Qt view layer.  It accepts immutable,
array-free DTOs and emits immutable edit requests.  Loading and saving profiles,
running the scoring engine, and every database operation belong to an external
controller or presenter.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import isfinite

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    QSignalBlocker,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableView,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from wuwa_assistant.ui.score_colors import apply_score_color, score_span
from wuwa_assistant.ui.widgets.choice_selector import ChoiceSelector as QComboBox

_INVALID_INDEX = QModelIndex()
type ModelIndex = QModelIndex | QPersistentModelIndex


def _validate_score(value: float, field_name: str) -> None:
    if not isfinite(value) or not 0 <= value <= 100:
        raise ValueError(f"{field_name} must be finite and within 0..100")


def _validate_probability(value: float | None, field_name: str) -> None:
    if value is not None and (not isfinite(value) or not 0 <= value <= 1):
        raise ValueError(f"{field_name} must be finite and within 0..1 or null")


@dataclass(frozen=True, slots=True)
class ProfileSummaryView:
    """One controller-provided entry in the profile selector."""

    identifier: str
    name: str
    template_label: str
    is_system_template: bool = False
    is_default: bool = False

    def __post_init__(self) -> None:
        if not self.identifier.strip() or not self.name.strip() or not self.template_label.strip():
            raise ValueError("profile identifier, name, and template label cannot be empty")

    @property
    def display_label(self) -> str:
        suffix = " · 預設" if self.is_default else ""
        return f"{self.name} ({self.template_label}){suffix}"


@dataclass(frozen=True, slots=True)
class ScoreThresholdsView:
    """User-configurable category boundaries for a 0..100 score."""

    usable: float = 55.0
    passing: float = 70.0
    perfect: float = 88.0
    exceptional: float = 95.0

    def __post_init__(self) -> None:
        for field_name, value in (
            ("usable", self.usable),
            ("passing", self.passing),
            ("perfect", self.perfect),
            ("exceptional", self.exceptional),
        ):
            _validate_score(value, field_name)
        if not self.usable < self.passing < self.perfect < self.exceptional:
            raise ValueError("thresholds must be strictly increasing")


@dataclass(frozen=True, slots=True)
class StatWeightView:
    """Editable stat-weight row; the condition is a versioned rule key."""

    stat_name: str
    unit: str
    weight: float
    cap_value: float | None = None
    condition: str = "always"

    def __post_init__(self) -> None:
        if not self.stat_name.strip():
            raise ValueError("stat_name cannot be empty")
        if not isfinite(self.weight) or self.weight < 0:
            raise ValueError("weight must be finite and nonnegative")
        if self.cap_value is not None and (not isfinite(self.cap_value) or self.cap_value < 0):
            raise ValueError("cap_value must be finite and nonnegative or null")
        if not self.condition.strip():
            raise ValueError("condition cannot be empty")


@dataclass(frozen=True, slots=True)
class ScoringProfileView:
    """Complete profile snapshot supplied by a controller."""

    identifier: str
    name: str
    template_kind: str
    template_label: str
    description: str = ""
    thresholds: ScoreThresholdsView = ScoreThresholdsView()
    stat_weights: tuple[StatWeightView, ...] = ()
    is_system_template: bool = False
    is_default: bool = False
    version: int = 1

    def __post_init__(self) -> None:
        if not self.identifier.strip() or not self.name.strip():
            raise ValueError("profile identifier and name cannot be empty")
        if not self.template_kind.strip() or not self.template_label.strip():
            raise ValueError("template kind and label cannot be empty")
        if self.version < 1:
            raise ValueError("version must be positive")
        names = [(row.stat_name, row.unit, row.condition) for row in self.stat_weights]
        if len(names) != len(set(names)):
            raise ValueError("stat weight rows must be unique by stat, unit, and condition")


@dataclass(frozen=True, slots=True)
class ScoringProfileEditRequest:
    """Audit-ready profile values emitted by the Save action."""

    identifier: str
    name: str
    template_kind: str
    description: str
    thresholds: ScoreThresholdsView
    stat_weights: tuple[StatWeightView, ...]
    expected_version: int

    def __post_init__(self) -> None:
        if not self.identifier.strip() or not self.name.strip() or not self.template_kind.strip():
            raise ValueError("profile identifier, name, and template kind cannot be empty")
        if self.expected_version < 1:
            raise ValueError("expected_version must be positive")


@dataclass(frozen=True, slots=True)
class ScoreBreakdownItemView:
    """One explainable contribution from the pure scoring engine."""

    label: str
    observed_value: str
    roll_tier: str
    weight: float
    contribution: float
    note: str = ""

    def __post_init__(self) -> None:
        if not self.label.strip():
            raise ValueError("breakdown label cannot be empty")
        if not isfinite(self.weight) or not isfinite(self.contribution):
            raise ValueError("breakdown weight and contribution must be finite")


@dataclass(frozen=True, slots=True)
class EchoScoreView:
    """Pure presentation target for an EchoScore and its breakdown."""

    score: float | None
    grade_label: str
    breakdown: tuple[ScoreBreakdownItemView, ...] = ()
    is_provisional: bool = False

    def __post_init__(self) -> None:
        if self.score is not None:
            _validate_score(self.score, "score")
        if not self.grade_label.strip():
            raise ValueError("grade_label cannot be empty")


@dataclass(frozen=True, slots=True)
class UpgradeDecisionView:
    """Pure presentation target for a rule-engine UpgradeDecision."""

    action_label: str
    reason: str
    projected_score_range: tuple[float, float] | None = None
    pass_probability: float | None = None
    perfect_probability: float | None = None
    exceptional_probability: float | None = None
    assumptions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.action_label.strip() or not self.reason.strip():
            raise ValueError("decision action and reason cannot be empty")
        if self.projected_score_range is not None:
            minimum, maximum = self.projected_score_range
            _validate_score(minimum, "projected minimum")
            _validate_score(maximum, "projected maximum")
            if minimum > maximum:
                raise ValueError("projected score range must be ordered")
        _validate_probability(self.pass_probability, "pass_probability")
        _validate_probability(self.perfect_probability, "perfect_probability")
        _validate_probability(self.exceptional_probability, "exceptional_probability")


@dataclass(frozen=True, slots=True)
class ScoreDecisionPresentationView:
    """Array-free score and decision payload accepted by the preview slot."""

    score: EchoScoreView
    decision: UpgradeDecisionView
    profile_name: str = "未指定設定檔"

    def __post_init__(self) -> None:
        if not self.profile_name.strip():
            raise ValueError("profile_name cannot be empty")


class StatWeightTableModel(QAbstractTableModel):
    """Editable model for weight, cap, and condition columns."""

    _HEADERS = ("屬性", "單位", "權重", "上限", "條件")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[StatWeightView] = []
        self._read_only = True

    def rowCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._HEADERS)

    def data(self, index: ModelIndex, role: int = int(Qt.ItemDataRole.DisplayRole)) -> object:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        row = self._rows[index.row()]
        values: tuple[object, ...] = (
            row.stat_name,
            row.unit or "—",
            row.weight,
            "無上限" if row.cap_value is None else row.cap_value,
            row.condition,
        )
        if role == int(Qt.ItemDataRole.DisplayRole):
            return values[index.column()]
        if role == int(Qt.ItemDataRole.EditRole):
            edit_values: tuple[object, ...] = (
                row.stat_name,
                row.unit,
                row.weight,
                "" if row.cap_value is None else row.cap_value,
                row.condition,
            )
            return edit_values[index.column()]
        if role == int(Qt.ItemDataRole.TextAlignmentRole) and index.column() in {2, 3}:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

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

    def flags(self, index: ModelIndex) -> Qt.ItemFlag:
        flags = super().flags(index)
        if index.isValid() and not self._read_only and index.column() in {2, 3, 4}:
            return flags | Qt.ItemFlag.ItemIsEditable
        return flags

    def setData(
        self,
        index: ModelIndex,
        value: object,
        role: int = int(Qt.ItemDataRole.EditRole),
    ) -> bool:
        if (
            role != int(Qt.ItemDataRole.EditRole)
            or self._read_only
            or not index.isValid()
            or not 0 <= index.row() < len(self._rows)
            or index.column() not in {2, 3, 4}
        ):
            return False
        previous = self._rows[index.row()]
        try:
            if index.column() == 2:
                replacement = StatWeightView(
                    previous.stat_name,
                    previous.unit,
                    _coerce_float(value),
                    previous.cap_value,
                    previous.condition,
                )
            elif index.column() == 3:
                cap = None if value is None or not str(value).strip() else _coerce_float(value)
                replacement = StatWeightView(
                    previous.stat_name,
                    previous.unit,
                    previous.weight,
                    cap,
                    previous.condition,
                )
            else:
                condition = str(value).strip()
                replacement = StatWeightView(
                    previous.stat_name,
                    previous.unit,
                    previous.weight,
                    previous.cap_value,
                    condition,
                )
        except (TypeError, ValueError):
            return False
        self._rows[index.row()] = replacement
        self.dataChanged.emit(index, index, [role, int(Qt.ItemDataRole.DisplayRole)])
        return True

    def set_rows(self, rows: Sequence[StatWeightView], *, read_only: bool) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self._read_only = read_only
        self.endResetModel()

    def rows(self) -> tuple[StatWeightView, ...]:
        return tuple(self._rows)

    def append_row(self, row: StatWeightView) -> bool:
        if self._read_only:
            return False
        position = len(self._rows)
        self.beginInsertRows(QModelIndex(), position, position)
        self._rows.append(row)
        self.endInsertRows()
        return True

    def remove_row(self, row: int) -> bool:
        if self._read_only or not 0 <= row < len(self._rows):
            return False
        self.beginRemoveRows(QModelIndex(), row, row)
        del self._rows[row]
        self.endRemoveRows()
        return True


class ScoreBreakdownTableModel(QAbstractTableModel):
    """Read-only presentation model for a score explanation."""

    _HEADERS = ("項目", "辨識值", "Roll", "權重", "得分", "說明")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[ScoreBreakdownItemView] = []

    def rowCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._HEADERS)

    def data(self, index: ModelIndex, role: int = int(Qt.ItemDataRole.DisplayRole)) -> object:
        if (
            role != int(Qt.ItemDataRole.DisplayRole)
            or not index.isValid()
            or not 0 <= index.row() < len(self._rows)
        ):
            return None
        row = self._rows[index.row()]
        return (
            row.label,
            row.observed_value,
            row.roll_tier or "—",
            f"{row.weight:.2f}",
            f"{row.contribution:+.2f}",
            row.note,
        )[index.column()]

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

    def set_rows(self, rows: Sequence[ScoreBreakdownItemView]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()


class ScoringProfilesPage(QWidget):
    """Passive profile editor with Model/View tables and score preview."""

    profile_selected = Signal(str)
    create_requested = Signal()
    clone_requested = Signal(str)
    save_requested = Signal(object)
    delete_requested = Signal(str)
    default_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ScoringProfilesPage")
        self._profile: ScoringProfileView | None = None
        self._profiles: tuple[ProfileSummaryView, ...] = ()
        self._capture_active = False
        self._loading = False

        title = QLabel("評分設定檔")
        title.setObjectName("PageTitle")
        subtitle = QLabel("調整角色詞條價值與分數門檻; 所有計算由可測試的規則引擎完成。")
        subtitle.setObjectName("PageDescription")

        toolbar = self._build_toolbar()
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setObjectName("ScoringProfilesSplitter")
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_editor())
        splitter.addWidget(self._build_score_preview())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 30, 40, 24)
        layout.setSpacing(12)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addWidget(toolbar)
        layout.addWidget(splitter, 1)
        self.clear_profile()
        self.clear_score_preview()

    def _build_toolbar(self) -> QWidget:
        toolbar = QGroupBox("設定檔選擇")
        toolbar.setObjectName("ScoringProfileToolbar")
        self.profile_selector = QComboBox()
        self.profile_selector.setObjectName("ScoringProfileSelector")
        self.profile_selector.currentIndexChanged.connect(self._emit_selected_profile)

        self.create_button = QPushButton("新增設定檔")
        self.create_button.setObjectName("ScoringCreateButton")
        self.create_button.clicked.connect(self.create_requested.emit)
        self.clone_button = QPushButton("複製設定檔")
        self.clone_button.setObjectName("ScoringCloneButton")
        self.clone_button.clicked.connect(self._emit_clone_requested)
        self.default_button = QPushButton("設為預設")
        self.default_button.setObjectName("ScoringDefaultButton")
        self.default_button.clicked.connect(self._emit_default_requested)

        row = QHBoxLayout(toolbar)
        row.addWidget(QLabel("目前設定檔"))
        row.addWidget(self.profile_selector, 1)
        row.addWidget(self.create_button)
        row.addWidget(self.clone_button)
        row.addWidget(self.default_button)
        return toolbar

    def _build_editor(self) -> QWidget:
        group = QGroupBox("設定檔內容")
        group.setObjectName("ScoringProfileEditor")
        self.system_template_banner = QLabel(
            "這是內建系統模板, 為確保可重現性不可直接修改; 請先複製後再調整。"
        )
        self.system_template_banner.setObjectName("ScoringSystemTemplateBanner")
        self.system_template_banner.setWordWrap(True)
        self.system_template_banner.setProperty("notice", True)

        self.name_edit = QLineEdit()
        self.name_edit.setObjectName("ScoringProfileNameEdit")
        self.template_label = QLabel("—")
        self.template_label.setObjectName("ScoringTemplateLabel")
        self.description_edit = QTextEdit()
        self.description_edit.setObjectName("ScoringProfileDescriptionEdit")
        self.description_edit.setAcceptRichText(False)
        self.description_edit.setMaximumHeight(90)

        identity_form = QFormLayout()
        identity_form.addRow("設定檔名稱", self.name_edit)
        identity_form.addRow("模板類型", self.template_label)
        identity_form.addRow("說明", self.description_edit)

        threshold_group = QGroupBox("分數門檻")
        threshold_group.setObjectName("ScoringThresholdGroup")
        self.usable_threshold_spin = _threshold_spin("ScoringUsableThresholdSpin")
        self.passing_threshold_spin = _threshold_spin("ScoringPassingThresholdSpin")
        self.perfect_threshold_spin = _threshold_spin("ScoringPerfectThresholdSpin")
        self.exceptional_threshold_spin = _threshold_spin("ScoringExceptionalThresholdSpin")
        threshold_form = QFormLayout(threshold_group)
        threshold_form.addRow("可用", self.usable_threshold_spin)
        threshold_form.addRow("及格", self.passing_threshold_spin)
        threshold_form.addRow("完美", self.perfect_threshold_spin)
        threshold_form.addRow("極品", self.exceptional_threshold_spin)

        self.weight_model = StatWeightTableModel(self)
        self.weight_table = QTableView()
        self.weight_table.setObjectName("ScoringStatWeightTable")
        self.weight_table.setModel(self.weight_model)
        self.weight_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.weight_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.weight_table.horizontalHeader().setStretchLastSection(True)
        self.weight_table.setMinimumHeight(180)

        self.add_weight_button = QPushButton("新增詞條")
        self.add_weight_button.setObjectName("ScoringAddWeightButton")
        self.add_weight_button.clicked.connect(self._append_weight_row)
        self.remove_weight_button = QPushButton("移除詞條")
        self.remove_weight_button.setObjectName("ScoringRemoveWeightButton")
        self.remove_weight_button.clicked.connect(self._remove_selected_weight_row)
        self.add_weight_button.setVisible(False)
        self.remove_weight_button.setVisible(False)
        weight_actions = QHBoxLayout()
        weight_actions.addStretch(1)
        weight_actions.addWidget(self.add_weight_button)
        weight_actions.addWidget(self.remove_weight_button)

        self.status_label = QLabel()
        self.status_label.setObjectName("ScoringProfileStatus")
        self.status_label.setWordWrap(True)
        self.save_button = QPushButton("儲存設定檔")
        self.save_button.setObjectName("ScoringSaveButton")
        self.save_button.clicked.connect(self._emit_save_requested)
        self.delete_button = QPushButton("刪除設定檔")
        self.delete_button.setObjectName("ScoringDeleteButton")
        self.delete_button.clicked.connect(self._emit_delete_requested)
        action_row = QHBoxLayout()
        action_row.addWidget(self.status_label, 1)
        action_row.addWidget(self.delete_button)
        action_row.addWidget(self.save_button)

        editor_content = QWidget()
        editor_layout = QVBoxLayout(editor_content)
        editor_layout.setContentsMargins(0, 0, 12, 0)
        editor_layout.addWidget(self.system_template_banner)
        editor_layout.addLayout(identity_form)
        editor_layout.addWidget(threshold_group)
        editor_layout.addWidget(QLabel("詞條權重 (權重、上限與條件可調整)"))
        editor_layout.addWidget(self.weight_table, 1)
        editor_layout.addLayout(weight_actions)
        editor_layout.addLayout(action_row)

        scroll = QScrollArea()
        scroll.setObjectName("ScoringProfileEditorScrollArea")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(editor_content)
        layout = QVBoxLayout(group)
        layout.addWidget(scroll)
        return group

    def _build_score_preview(self) -> QWidget:
        group = QGroupBox("評分與強化建議預覽")
        group.setObjectName("ScoringPreviewPanel")
        self.score_label = QLabel("—")
        self.score_label.setObjectName("ScoringCurrentScore")
        apply_score_color(self.score_label, None)
        self.preview_profile_label = QLabel("—")
        self.preview_profile_label.setObjectName("ScoringPreviewProfile")
        self.grade_label = QLabel("尚無評分")
        self.grade_label.setObjectName("ScoringGradeLabel")
        self.decision_label = QLabel("資料不足, 需要人工確認")
        self.decision_label.setObjectName("ScoringDecisionLabel")
        self.decision_label.setWordWrap(True)
        self.decision_reason_label = QLabel("選擇有聲骸資料的 Session 後顯示計算結果。")
        self.decision_reason_label.setObjectName("ScoringDecisionReason")
        self.decision_reason_label.setWordWrap(True)
        self.probability_label = QLabel("機率: —")
        self.probability_label.setObjectName("ScoringProbabilitySummary")
        self.probability_label.setWordWrap(True)
        self.assumptions_label = QLabel()
        self.assumptions_label.setObjectName("ScoringAssumptions")
        self.assumptions_label.setWordWrap(True)

        summary_form = QFormLayout()
        summary_form.addRow("使用設定檔", self.preview_profile_label)
        summary_form.addRow("目前分數", self.score_label)
        summary_form.addRow("分級", self.grade_label)
        summary_form.addRow("建議", self.decision_label)
        summary_form.addRow("理由", self.decision_reason_label)
        summary_form.addRow("達標機率", self.probability_label)
        summary_form.addRow("假設", self.assumptions_label)

        self.breakdown_model = ScoreBreakdownTableModel(self)
        self.breakdown_table = QTableView()
        self.breakdown_table.setObjectName("ScoringBreakdownTable")
        self.breakdown_table.setModel(self.breakdown_model)
        self.breakdown_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.breakdown_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.breakdown_table.horizontalHeader().setStretchLastSection(True)
        self.breakdown_table.setMinimumHeight(140)

        layout = QVBoxLayout(group)
        layout.addLayout(summary_form)
        layout.addWidget(QLabel("分數 Breakdown"))
        layout.addWidget(self.breakdown_table, 1)
        return group

    @Slot(object)
    def set_profiles(self, profiles: object) -> None:
        if not isinstance(profiles, (list, tuple)) or not all(
            isinstance(profile, ProfileSummaryView) for profile in profiles
        ):
            raise TypeError("profiles must be a list or tuple of ProfileSummaryView")
        selected = self.current_profile_id()
        self._profiles = tuple(profiles)
        blocker = QSignalBlocker(self.profile_selector)
        self.profile_selector.clear()
        for profile in self._profiles:
            self.profile_selector.addItem(profile.display_label, profile.identifier)
        if selected is not None:
            index = self.profile_selector.findData(selected)
            if index >= 0:
                self.profile_selector.setCurrentIndex(index)
        del blocker
        has_profile = self.profile_selector.count() > 0
        mutations_allowed = not self._capture_active and not self._loading
        self.clone_button.setEnabled(has_profile and mutations_allowed)
        self.default_button.setEnabled(has_profile and mutations_allowed)
        if has_profile:
            current = self.current_profile_id()
            if current is not None and current != selected:
                self.profile_selected.emit(current)
        else:
            self.clear_profile()

    @Slot(object)
    def apply_profile(self, profile: object) -> None:
        if not isinstance(profile, ScoringProfileView):
            raise TypeError("profile must be ScoringProfileView")
        self._profile = profile
        index = self.profile_selector.findData(profile.identifier)
        if index >= 0:
            blocker = QSignalBlocker(self.profile_selector)
            self.profile_selector.setCurrentIndex(index)
            del blocker
        self.name_edit.setText(profile.name)
        self.template_label.setText(profile.template_label)
        self.description_edit.setPlainText(profile.description)
        self.usable_threshold_spin.setValue(profile.thresholds.usable)
        self.passing_threshold_spin.setValue(profile.thresholds.passing)
        self.perfect_threshold_spin.setValue(profile.thresholds.perfect)
        self.exceptional_threshold_spin.setValue(profile.thresholds.exceptional)
        self.weight_model.set_rows(
            profile.stat_weights,
            read_only=profile.is_system_template,
        )
        self.status_label.setProperty("error", False)
        self.status_label.setText("預設設定檔" if profile.is_default else "")
        self._apply_editability(profile)

    @Slot()
    def clear_profile(self) -> None:
        self._profile = None
        self.name_edit.clear()
        self.template_label.setText("—")
        self.description_edit.clear()
        defaults = ScoreThresholdsView()
        self.usable_threshold_spin.setValue(defaults.usable)
        self.passing_threshold_spin.setValue(defaults.passing)
        self.perfect_threshold_spin.setValue(defaults.perfect)
        self.exceptional_threshold_spin.setValue(defaults.exceptional)
        self.weight_model.set_rows((), read_only=True)
        self.system_template_banner.hide()
        for widget in self._editable_widgets():
            widget.setEnabled(False)
        self.clone_button.setEnabled(False)
        self.default_button.setEnabled(False)
        self.save_button.setEnabled(False)
        self.delete_button.setEnabled(False)
        self.add_weight_button.setEnabled(False)
        self.remove_weight_button.setEnabled(False)
        self.status_label.setProperty("error", False)
        self.status_label.setText("請選擇或新增設定檔。")

    @Slot(object)
    def apply_score_preview(self, presentation: object) -> None:
        if not isinstance(presentation, ScoreDecisionPresentationView):
            raise TypeError("presentation must be ScoreDecisionPresentationView")
        score = presentation.score
        decision = presentation.decision
        self.preview_profile_label.setText(presentation.profile_name)
        self.score_label.setText("—" if score.score is None else f"{score.score:.1f} / 100")
        apply_score_color(self.score_label, score.score)
        self.score_label.setProperty("provisional", score.is_provisional)
        self.grade_label.setText(
            f"{score.grade_label} (暫定)" if score.is_provisional else score.grade_label
        )
        self.decision_label.setText(decision.action_label)
        self.decision_reason_label.setText(decision.reason)
        probability_parts = (
            f"及格 {_format_probability(decision.pass_probability)}",
            f"完美 {_format_probability(decision.perfect_probability)}",
            f"極品 {_format_probability(decision.exceptional_probability)}",
        )
        range_text = ""
        if decision.projected_score_range is not None:
            minimum, maximum = decision.projected_score_range
            range_text = (
                "; 預估最終 "
                + score_span(f"{minimum:.1f}", minimum)
                + "-"
                + score_span(f"{maximum:.1f}", maximum)
            )
        self.probability_label.setText("、".join(probability_parts) + range_text)
        self.assumptions_label.setText(
            "; ".join(decision.assumptions) if decision.assumptions else "無額外假設"
        )
        self.breakdown_model.set_rows(score.breakdown)

    @Slot()
    def clear_score_preview(self) -> None:
        self.preview_profile_label.setText("—")
        self.score_label.setText("—")
        apply_score_color(self.score_label, None)
        self.score_label.setProperty("provisional", False)
        self.grade_label.setText("尚無評分")
        self.decision_label.setText("資料不足, 需要人工確認")
        self.decision_reason_label.setText("選擇有聲骸資料的 Session 後顯示計算結果。")
        self.probability_label.setText("機率: —")
        self.assumptions_label.setText("—")
        self.breakdown_model.set_rows(())

    @Slot(bool, str)
    def set_loading(self, loading: bool, message: str = "正在讀取本機設定檔…") -> None:
        self._loading = loading
        self.profile_selector.setEnabled(not loading)
        self.create_button.setEnabled(not loading and not self._capture_active)
        if loading:
            for button in self._mutation_buttons():
                button.setEnabled(False)
            self.status_label.setProperty("error", False)
            self.status_label.setText(message)
        elif self._profile is not None:
            self._apply_editability(self._profile)

    @Slot(bool)
    def set_capture_active(self, active: bool) -> None:
        self._capture_active = active
        self.profile_selector.setEnabled(not self._loading)
        self.create_button.setEnabled(not self._loading and not active)
        if self._profile is not None:
            self._apply_editability(self._profile)
        if active:
            self.status_label.setProperty("error", False)
            self.status_label.setText("辨識進行中; 停止辨識後才可修改評分設定檔。")

    @Slot(str)
    def show_error(self, message: str) -> None:
        self.status_label.setProperty("error", True)
        self.status_label.setText(f"評分設定檔操作失敗: {message}")
        _repolish(self.status_label)

    @Slot(str)
    def show_status(self, message: str) -> None:
        self.status_label.setProperty("error", False)
        self.status_label.setText(message)
        _repolish(self.status_label)

    def current_profile_id(self) -> str | None:
        value = self.profile_selector.currentData()
        return value if isinstance(value, str) and value else None

    def _apply_editability(self, profile: ScoringProfileView) -> None:
        mutations_allowed = not self._capture_active and not self._loading
        editable = not profile.is_system_template and mutations_allowed
        self.system_template_banner.setVisible(profile.is_system_template)
        for widget in self._editable_widgets():
            widget.setEnabled(editable)
        self.weight_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
            if profile.is_system_template
            else QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.save_button.setEnabled(editable)
        self.delete_button.setEnabled(editable)
        self.add_weight_button.setEnabled(editable)
        self.remove_weight_button.setEnabled(editable and self.weight_model.rowCount() > 0)
        self.clone_button.setEnabled(mutations_allowed)
        self.default_button.setEnabled(mutations_allowed and not profile.is_default)

    def _editable_widgets(self) -> tuple[QWidget, ...]:
        return (
            self.name_edit,
            self.description_edit,
            self.usable_threshold_spin,
            self.passing_threshold_spin,
            self.perfect_threshold_spin,
            self.exceptional_threshold_spin,
            self.weight_table,
        )

    def _mutation_buttons(self) -> tuple[QPushButton, ...]:
        return (
            self.create_button,
            self.clone_button,
            self.default_button,
            self.save_button,
            self.delete_button,
            self.add_weight_button,
            self.remove_weight_button,
        )

    def _emit_selected_profile(self, index: int) -> None:
        if index < 0:
            return
        identifier = self.current_profile_id()
        if identifier is not None:
            self.profile_selected.emit(identifier)

    def _emit_clone_requested(self) -> None:
        identifier = self.current_profile_id()
        if identifier is not None:
            self.clone_requested.emit(identifier)

    def _emit_default_requested(self) -> None:
        identifier = self.current_profile_id()
        if identifier is not None:
            self.default_requested.emit(identifier)

    def _emit_delete_requested(self) -> None:
        if self._profile is not None and not self._profile.is_system_template:
            self.delete_requested.emit(self._profile.identifier)

    def _emit_save_requested(self) -> None:
        profile = self._profile
        if profile is None or profile.is_system_template:
            return
        name = self.name_edit.text().strip()
        if not name:
            self._show_validation_error("設定檔名稱不可空白。")
            return
        values = (
            self.usable_threshold_spin.value(),
            self.passing_threshold_spin.value(),
            self.perfect_threshold_spin.value(),
            self.exceptional_threshold_spin.value(),
        )
        if not values[0] < values[1] < values[2] < values[3]:
            self._show_validation_error("門檻必須依序為: 可用 < 及格 < 完美 < 極品。")
            return
        request = ScoringProfileEditRequest(
            identifier=profile.identifier,
            name=name,
            template_kind=profile.template_kind,
            description=self.description_edit.toPlainText().strip(),
            thresholds=ScoreThresholdsView(*values),
            stat_weights=self.weight_model.rows(),
            expected_version=profile.version,
        )
        self.status_label.setProperty("error", False)
        self.status_label.setText("正在儲存本機設定檔…")
        _repolish(self.status_label)
        self.save_requested.emit(request)

    def _append_weight_row(self) -> None:
        if self._profile is None or self._profile.is_system_template:
            return
        existing_names = {row.stat_name for row in self.weight_model.rows()}
        suffix = 1
        name = "自訂屬性"
        while name in existing_names:
            suffix += 1
            name = f"自訂屬性 {suffix}"
        if self.weight_model.append_row(StatWeightView(name, "%", 0.0)):
            self.remove_weight_button.setEnabled(True)

    def _remove_selected_weight_row(self) -> None:
        current = self.weight_table.currentIndex()
        if current.isValid() and self.weight_model.remove_row(current.row()):
            self.remove_weight_button.setEnabled(self.weight_model.rowCount() > 0)

    def _show_validation_error(self, message: str) -> None:
        self.status_label.setProperty("error", True)
        self.status_label.setText(message)
        _repolish(self.status_label)


def _threshold_spin(object_name: str) -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setObjectName(object_name)
    spin.setRange(0, 100)
    spin.setDecimals(1)
    spin.setSingleStep(1.0)
    spin.setSuffix(" 分")
    return spin


def _coerce_float(value: object) -> float:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise TypeError("value must be a string or number")
    return float(value)


def _format_probability(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def _repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


__all__ = [
    "EchoScoreView",
    "ProfileSummaryView",
    "ScoreBreakdownItemView",
    "ScoreBreakdownTableModel",
    "ScoreDecisionPresentationView",
    "ScoreThresholdsView",
    "ScoringProfileEditRequest",
    "ScoringProfileView",
    "ScoringProfilesPage",
    "StatWeightTableModel",
    "StatWeightView",
    "UpgradeDecisionView",
]
