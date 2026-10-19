"""Traditional-Chinese character cultivation master/detail view.

The page is deliberately a passive Qt view.  It contains no repository or
database dependency: presenters provide immutable DTOs through setters and the
page reports user intent through signals.  This keeps persistence, validation
beyond basic UI constraints, and long-running work outside the GUI thread.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from math import isfinite

from PySide6.QtCore import (
    QAbstractTableModel,
    QEvent,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QSignalBlocker,
    QSize,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QLineEdit,
    QListView,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from wuwa_assistant.application.character_stats import CORE_STATS, STAT_LABELS, CharacterPanel
from wuwa_assistant.scoring.character_guides import guide_for_character
from wuwa_assistant.ui.character_portraits import CharacterPortraitLabel
from wuwa_assistant.ui.character_roster import CharacterRosterDelegate
from wuwa_assistant.ui.pages.equipment_detail import EquipmentDetailPage, GearCharacterDetailView
from wuwa_assistant.ui.score_colors import apply_score_color
from wuwa_assistant.ui.widgets.choice_selector import ChoiceSelector as QComboBox
from wuwa_assistant.ui.widgets.text_details import TextDetailsButton

from .equipment_overview import EquipmentOverviewPage

_INVALID_INDEX = QModelIndex()
type ModelIndex = QModelIndex | QPersistentModelIndex

_ELEMENTS = ("氣動", "熱熔", "冷凝", "導電", "衍射", "湮滅")


def _validate_percentage(value: float, field_name: str) -> None:
    if not isfinite(value) or not 0 <= value <= 100:
        raise ValueError(f"{field_name} must be finite and within 0..100")


@dataclass(frozen=True, slots=True)
class ChoiceOptionView:
    """Controller-provided identifier/label option."""

    identifier: str
    label: str

    def __post_init__(self) -> None:
        if not self.identifier.strip() or not self.label.strip():
            raise ValueError("choice identifier and label cannot be empty")


@dataclass(frozen=True, slots=True)
class CharacterSummaryView:
    """One row in the character master list."""

    identifier: str
    name: str
    element: str
    role: str
    is_candidate: bool
    priority: int
    completion_percent: float
    archived: bool = False

    def __post_init__(self) -> None:
        if not self.identifier.strip() or not self.name.strip():
            raise ValueError("character identifier and name cannot be empty")
        if self.priority < 0:
            raise ValueError("priority cannot be negative")
        _validate_percentage(self.completion_percent, "completion_percent")


@dataclass(frozen=True, slots=True)
class WeaponProgressView:
    name: str | None = None
    level: int | None = None
    resonance_rank: int | None = None

    def __post_init__(self) -> None:
        if self.level is not None and not 0 <= self.level <= 90:
            raise ValueError("weapon level must be within 0..90 or null")
        if self.resonance_rank is not None and not 1 <= self.resonance_rank <= 5:
            raise ValueError("weapon resonance_rank must be within 1..5 or null")


@dataclass(frozen=True, slots=True)
class SkillProgressView:
    skill_key: str
    label: str
    current_level: int
    target_level: int
    is_core: bool

    def __post_init__(self) -> None:
        if not self.skill_key.strip() or not self.label.strip():
            raise ValueError("skill key and label cannot be empty")
        if not 0 <= self.current_level <= 10:
            raise ValueError("current skill level must be within 0..10")
        if not 1 <= self.target_level <= 10:
            raise ValueError("target skill level must be within 1..10")


@dataclass(frozen=True, slots=True)
class PermanentEchoSlotView:
    position: int
    echo_id: str | None = None
    echo_name: str | None = None
    set_name: str | None = None
    main_stat: str | None = None
    cost: int | None = None
    level: int | None = None
    score: float | None = None
    is_passing: bool | None = False
    set_is_valid: bool | None = None
    main_stat_is_valid: bool | None = None
    score_note: str | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.position <= 5:
            raise ValueError("echo position must be within 1..5")
        if self.cost is not None and self.cost not in {1, 3, 4}:
            raise ValueError("echo cost must be 1, 3, 4, or null")
        if self.level is not None and not 0 <= self.level <= 25:
            raise ValueError("echo level must be within 0..25 or null")
        if self.score is not None:
            _validate_percentage(self.score, "echo score")


@dataclass(frozen=True, slots=True)
class CharacterDetailView:
    """Complete presenter-owned snapshot rendered by the detail editor."""

    identifier: str
    name: str
    element: str
    role: str
    is_candidate: bool
    priority: int
    level: int
    resonance_chain: int
    scoring_profile_id: str | None
    scoring_profile_name: str | None
    weapon: WeaponProgressView
    team_id: str | None
    team_name: str | None
    skills: tuple[SkillProgressView, ...]
    attribute_nodes: tuple[bool, ...]
    echo_slots: tuple[PermanentEchoSlotView, ...]
    cultivation_goal: str = ""
    notes: str = ""
    completion_percent: float = 0
    passing_echoes: int = 0
    weakest_position: str = "尚未評估"
    next_suggestion: str = "尚未建立養成建議"
    archived: bool = False
    unpositioned_echoes: tuple[str, ...] = ()
    panel_energy_regen: float | None = None
    energy_target_override: float | None = None
    calculated_panel: CharacterPanel | None = None
    character_ascension: int | None = None
    weapon_ascension: int | None = None

    def __post_init__(self) -> None:
        if not self.identifier.strip() or not self.name.strip():
            raise ValueError("character identifier and name cannot be empty")
        if self.priority < 0:
            raise ValueError("priority cannot be negative")
        if not 0 <= self.level <= 90:
            raise ValueError("character level must be within 0..90")
        if not 0 <= self.resonance_chain <= 6:
            raise ValueError("resonance_chain must be within 0..6")
        if len(self.skills) != 5:
            raise ValueError("exactly five skill rows are required")
        if len(self.attribute_nodes) != 10:
            raise ValueError("exactly ten attribute node states are required")
        if len(self.echo_slots) != 5 or {slot.position for slot in self.echo_slots} != set(
            range(1, 6)
        ):
            raise ValueError("exactly one echo slot for each position 1..5 is required")
        if not 0 <= self.passing_echoes <= 5:
            raise ValueError("passing_echoes must be within 0..5")
        _validate_percentage(self.completion_percent, "completion_percent")


@dataclass(frozen=True, slots=True)
class CharacterEditRequest:
    """Immutable character editor payload emitted to a presenter."""

    identifier: str | None
    name: str
    element: str
    role: str
    is_candidate: bool
    priority: int
    level: int
    resonance_chain: int
    scoring_profile_id: str | None
    weapon: WeaponProgressView
    team_id: str | None
    skills: tuple[SkillProgressView, ...]
    attribute_nodes: tuple[bool, ...]
    cultivation_goal: str
    notes: str
    echo_validations: tuple[tuple[bool | None, bool | None], ...] = ((None, None),) * 5
    panel_energy_regen: float | None = None
    energy_target_override: float | None = None
    character_ascension: int | None = None
    weapon_ascension: int | None = None

    def __post_init__(self) -> None:
        if self.identifier is not None and not self.identifier.strip():
            raise ValueError("character identifier cannot be blank")
        if not self.name.strip():
            raise ValueError("character name cannot be empty")
        if self.priority < 0:
            raise ValueError("priority cannot be negative")
        if not 0 <= self.level <= 90:
            raise ValueError("character level must be within 0..90")
        if not 0 <= self.resonance_chain <= 6:
            raise ValueError("resonance_chain must be within 0..6")
        if len(self.skills) != 5 or len(self.attribute_nodes) != 10:
            raise ValueError("edit request requires five skills and ten attribute nodes")
        if len(self.echo_validations) != 5 or any(
            len(pair) != 2 or any(value is not None and type(value) is not bool for value in pair)
            for pair in self.echo_validations
        ):
            raise ValueError("edit request requires five set/main-stat validity pairs")


class CharacterListModel(QAbstractTableModel):
    """Read-only model for the resizable character master list."""

    _HEADERS = ("角色", "屬性", "定位", "上場", "優先")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[CharacterSummaryView] = []

    def rowCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._HEADERS)

    def data(self, index: ModelIndex, role: int = int(Qt.ItemDataRole.DisplayRole)) -> object:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        row = self._rows[index.row()]
        if role == int(Qt.ItemDataRole.UserRole):
            return row.identifier
        if role == int(Qt.ItemDataRole.DisplayRole):
            display_values = (
                f"已封存 · {row.name}" if row.archived else row.name,
                row.element or "—",
                row.role or "—",
                "是" if row.is_candidate else "否",
                str(row.priority),
            )
            return display_values[index.column()]
        if role == int(Qt.ItemDataRole.EditRole):
            sort_values = (
                row.name,
                row.element,
                row.role,
                row.is_candidate,
                row.priority,
            )
            return sort_values[index.column()]
        if role == int(Qt.ItemDataRole.ToolTipRole) and row.archived:
            return "此角色已封存"
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

    def set_rows(self, rows: Sequence[CharacterSummaryView]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def character_id_at(self, row: int) -> str | None:
        return self._rows[row].identifier if 0 <= row < len(self._rows) else None

    def row_for_character(self, identifier: str) -> int | None:
        for row_number, row in enumerate(self._rows):
            if row.identifier == identifier:
                return row_number
        return None


class SkillProgressTableModel(QAbstractTableModel):
    """Editable five-row skill target model used only inside the page."""

    _HEADERS = ("技能", "目前", "目標", "升級順序")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[SkillProgressView] = []

    def rowCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._HEADERS)

    def data(self, index: ModelIndex, role: int = int(Qt.ItemDataRole.DisplayRole)) -> object:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        row = self._rows[index.row()]
        column = index.column()
        if role in (int(Qt.ItemDataRole.DisplayRole), int(Qt.ItemDataRole.EditRole)):
            return (row.label, row.current_level, row.target_level, index.row() + 1)[column]
        if role == int(Qt.ItemDataRole.UserRole):
            return row.skill_key
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
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
        if index.column() in {1, 2}:
            flags |= Qt.ItemFlag.ItemIsEditable
        return flags

    def setData(
        self,
        index: ModelIndex,
        value: object,
        role: int = int(Qt.ItemDataRole.EditRole),
    ) -> bool:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return False
        old = self._rows[index.row()]
        if index.column() in {1, 2} and role == int(Qt.ItemDataRole.EditRole):
            try:
                level = _coerce_level(value, minimum=0 if index.column() == 1 else 1)
            except (TypeError, ValueError):
                return False
            current = level if index.column() == 1 else old.current_level
            target = level if index.column() == 2 else old.target_level
            replacement = SkillProgressView(old.skill_key, old.label, current, target, old.is_core)
        else:
            return False
        if replacement == old:
            return False
        self._rows[index.row()] = replacement
        self.dataChanged.emit(index, index, [role])
        return True

    def set_rows(self, rows: Sequence[SkillProgressView]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def rows(self) -> tuple[SkillProgressView, ...]:
        return tuple(self._rows)

    def move_priority(self, row: int, direction: int) -> bool:
        target = row + direction
        if (
            direction not in (-1, 1)
            or not 0 <= row < len(self._rows)
            or not 0 <= target < len(self._rows)
        ):
            return False
        self.beginMoveRows(QModelIndex(), row, row, QModelIndex(), target + (direction > 0))
        self._rows.insert(target, self._rows.pop(row))
        self.endMoveRows()
        self.dataChanged.emit(self.index(min(row, target), 0), self.index(max(row, target), 3))
        return True


class _EchoSlotCard(QFrame):
    def __init__(self, position: int, edit_callback: Callable[[], None]) -> None:
        super().__init__()
        self.position = position
        self._edit_callback = edit_callback
        self.setObjectName(f"CharacterEchoSlot{position}")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        title = QLabel(f"位置 {position}")
        title.setObjectName(f"CharacterEchoSlot{position}Title")
        self.name_label = QLabel("尚未裝備")
        self.name_label.setObjectName(f"CharacterEchoSlot{position}Name")
        self.name_label.setWordWrap(True)
        self.detail_label = QLabel("—")
        self.detail_label.setWordWrap(True)
        self.score_label = QLabel("尚未評分")
        self.score_label.setObjectName("CharacterEchoScore")
        apply_score_color(self.score_label, None)
        self.score_label.setWordWrap(True)
        self.score_details_button = TextDetailsButton("查看評分說明")
        self.score_details_button.setObjectName(f"CharacterEchoSlot{position}ScoreDetails")
        self.set_validity_combo = _validity_combo(f"CharacterEchoSlot{position}SetValidity")
        self.main_validity_combo = _validity_combo(f"CharacterEchoSlot{position}MainValidity")
        self.edit_button = QPushButton("管理")
        self.edit_button.setObjectName(f"CharacterEchoSlot{position}EditButton")
        self.edit_button.clicked.connect(self._request_edit)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.addWidget(title)
        layout.addWidget(self.name_label)
        layout.addWidget(self.detail_label)
        layout.addWidget(self.score_label)
        layout.addWidget(self.score_details_button)
        self.overrides = QWidget()
        overrides_layout = QVBoxLayout(self.overrides)
        explanation = QLabel("只在採用不同配裝時覆寫自動判斷; 儲存角色後生效。")
        explanation.setWordWrap(True)
        overrides_layout.addWidget(explanation)
        overrides_layout.addWidget(QLabel("自訂套裝判定"))
        overrides_layout.addWidget(self.set_validity_combo)
        overrides_layout.addWidget(QLabel("自訂主詞條判定"))
        overrides_layout.addWidget(self.main_validity_combo)
        self.overrides.setVisible(False)
        self.override_toggle = QPushButton("自訂配裝判定")
        self.override_toggle.setCheckable(True)
        self.override_toggle.toggled.connect(self.overrides.setVisible)
        layout.addWidget(self.override_toggle)
        layout.addWidget(self.overrides)
        layout.addWidget(self.edit_button)

    def apply_slot(self, slot: PermanentEchoSlotView) -> None:
        apply_score_color(self.score_label, slot.score)
        self.name_label.setText(slot.echo_name or "尚未裝備")
        details = [value for value in (slot.set_name, slot.main_stat) if value]
        if slot.cost is not None:
            details.append(f"COST {slot.cost}")
        if slot.level is not None:
            details.append(f"+{slot.level}")
        self.detail_label.setText(" · ".join(details) or "—")
        if slot.score is None:
            self.score_label.setText("尚未評分")
        else:
            result = (
                "適配待確認" if slot.is_passing is None else "及格" if slot.is_passing else "未及格"
            )
            self.score_label.setText(f"{slot.score:.1f} 分 · {result}")
        self.score_details_button.setText(
            "查看未評分原因" if slot.score is None else "查看評分說明"
        )
        self.score_details_button.set_details(
            f"位置 {self.position} · 評分說明", slot.score_note or ""
        )
        _select_validity(self.set_validity_combo, slot.set_is_valid)
        _select_validity(self.main_validity_combo, slot.main_stat_is_valid)

    def validity(self) -> tuple[bool | None, bool | None]:
        return (_combo_validity(self.set_validity_combo), _combo_validity(self.main_validity_combo))

    def _request_edit(self) -> None:
        self._edit_callback()


class CharactersPage(QWidget):
    """Resizable character manager with a signal-only persistence boundary."""

    refresh_requested = Signal()
    catalog_requested = Signal()
    character_selected = Signal(str)
    add_requested = Signal()
    edit_requested = Signal(object)
    archive_requested = Signal(str)
    weapon_edit_requested = Signal(str)
    team_edit_requested = Signal(str)
    echo_slot_edit_requested = Signal(str, int)
    training_requested = Signal(str, int)
    equipment_requested = Signal()
    cultivation_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("CharactersPage")
        self._detail: CharacterDetailView | None = None
        self._creating = False
        self._loading = False
        self._equipment_details: dict[str, GearCharacterDetailView] = {}
        self._all_character_rows: tuple[CharacterSummaryView, ...] = ()

        title = QLabel("角色養成")
        title.setObjectName("PageTitle")
        self.catalog_button = QPushButton("從角色目錄加入", self)
        self.catalog_button.setObjectName("CharacterCatalogButton")
        self.catalog_button.setProperty("role", "primary")
        self.catalog_button.clicked.connect(lambda _checked=False: self.catalog_requested.emit())
        self.add_button = QPushButton("手動新增", self)
        self.add_button.setObjectName("CharacterAddButton")
        self.add_button.clicked.connect(self._request_add)
        self.refresh_button = QPushButton("重新整理", self)
        self.refresh_button.setObjectName("CharacterRefreshButton")
        self.refresh_button.clicked.connect(self._request_refresh)
        self.status_label = QLabel("尚未載入角色。")
        self.status_label.setObjectName("CharacterLoadStatus")
        self.status_label.setWordWrap(True)

        # Recognition already refreshes the roster through the presenter. Retain
        # these integration actions without exposing unused creation controls.
        for action in (self.catalog_button, self.add_button, self.refresh_button):
            action.hide()
        toolbar = QHBoxLayout()
        toolbar.addWidget(title)
        toolbar.addStretch(1)
        toolbar.addWidget(self.status_label)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setObjectName("CharacterMasterDetailSplitter")
        self.splitter.setChildrenCollapsible(False)
        self.master_panel = self._build_master_panel()
        self.splitter.addWidget(self._build_detail_stack())
        self.splitter.addWidget(self.master_panel)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setHandleWidth(0)
        self._set_roster_expanded(False)

        self.overview_page = EquipmentOverviewPage()
        self.overview_page.character_requested.connect(self.open_equipment_character)
        self.overview_page.piece_requested.connect(self.open_equipment_piece)
        self.equipment_detail_page = EquipmentDetailPage()
        from wuwa_assistant.ui.widgets.equipment_workspace import EquipmentWorkspace

        self.equipment_workspace = EquipmentWorkspace(
            self.overview_page, self.equipment_detail_page, self
        )
        self.equipment_workspace.hide()
        self.equipment_detail_page.back_requested.connect(self.go_equipment_overview)
        self.equipment_detail_page.edit_requested.connect(self.open_character_editor)
        self.equipment_detail_page.training_requested.connect(self.training_requested)
        self.equipment_detail_page.slot_selected.connect(self._equipment_slot_selected)
        self.equipment_detail_page.sources_requested.connect(
            self.overview_page.show_character_guidance
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addLayout(toolbar)
        layout.addWidget(self.splitter, 1)

        self.clear_detail()
        for spin in (
            self.level_spin,
            self.weapon_level_spin,
            self.chain_spin,
            self.weapon_rank_spin,
        ):
            spin.valueChanged.connect(self._update_progress_summary)
        self.weapon_name_edit.textChanged.connect(self._update_progress_summary)
        self.name_edit.textChanged.connect(self._update_progress_summary)
        self.skill_model.dataChanged.connect(self._update_progress_summary)

    def _build_master_panel(self) -> QWidget:
        group = QWidget()
        group.setObjectName("CharacterMasterPanel")
        self.roster_expand_button = QPushButton("展開角色")
        self.roster_expand_button.setObjectName("CharacterRosterExpandButton")
        self.roster_expand_button.setCheckable(True)
        self.roster_expand_button.toggled.connect(self._set_roster_expanded)
        self.show_archived_check = QCheckBox("封存角色")
        self.show_archived_check.setObjectName("CharacterShowArchivedCheck")
        self.show_archived_check.setToolTip("顯示已封存角色")
        self.show_archived_check.toggled.connect(self._toggle_archived_visibility)
        self.character_model = CharacterListModel(self)
        self.character_table = QListView()
        self.character_table.setObjectName("CharacterMasterTable")
        self.character_table.setModel(self.character_model)
        self.character_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.character_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.character_table.setViewMode(QListView.ViewMode.IconMode)
        self.character_table.setMovement(QListView.Movement.Static)
        self.character_table.setResizeMode(QListView.ResizeMode.Adjust)
        self.character_table.setGridSize(QSize(88, 104))
        self.character_table.setFlow(QListView.Flow.LeftToRight)
        self.character_table.setWrapping(True)
        self.character_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.character_table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.character_table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.character_table.setItemDelegate(
            CharacterRosterDelegate(self.character_table, portrait_tile=True)
        )
        selection_model = self.character_table.selectionModel()
        if selection_model is not None:
            selection_model.currentRowChanged.connect(self._on_current_character_changed)
        layout = QVBoxLayout(group)
        layout.setContentsMargins(6, 0, 0, 0)
        layout.addWidget(self.roster_expand_button)
        layout.addWidget(self.show_archived_check)
        layout.addWidget(self.character_table)
        return group

    @Slot(bool)
    def _set_roster_expanded(self, expanded: bool) -> None:
        width = 296 if expanded else 120
        self.master_panel.setFixedWidth(width)
        self.splitter.setSizes([max(1, self.splitter.width() - width), width])
        self.roster_expand_button.setText("收合角色" if expanded else "展開角色")
        self.roster_expand_button.setToolTip("每排顯示三名角色" if not expanded else "回到單排頭像")

    def _build_detail_stack(self) -> QStackedWidget:
        self.detail_stack = QStackedWidget()
        self.detail_stack.setObjectName("CharacterDetailStack")

        empty = QWidget()
        empty_layout = QVBoxLayout(empty)
        self.empty_state_label = QLabel(
            "掃描已裝備的聲骸後, 辨識到的角色會自動加入。\n新角色的攻略與評分設定可再請助理更新。"
        )
        self.empty_state_label.setObjectName("CharacterEmptyState")
        self.empty_state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_state_label.setWordWrap(True)
        empty_layout.addStretch(1)
        empty_layout.addWidget(self.empty_state_label)
        empty_layout.addStretch(1)

        self.detail_scroll = QScrollArea()
        self.detail_scroll.setObjectName("CharacterDetailScrollArea")
        self.detail_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.detail_scroll.setWidgetResizable(True)
        detail = QWidget()
        detail.setObjectName("CharacterDetailContent")
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(0, 0, 8, 0)
        detail_layout.setSpacing(10)
        detail_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        detail_layout.addWidget(self._build_progress_panel())
        detail_layout.addWidget(self._build_stats_panel())
        self.frequent_layout = QGridLayout()
        self.skill_group = self._build_skill_panel()
        self.node_group = self._build_node_panel()
        self.frequent_layout.addWidget(self.skill_group, 0, 0)
        self.frequent_layout.addWidget(self.node_group, 0, 1)
        self.frequent_layout.setColumnStretch(0, 3)
        self.frequent_layout.setColumnStretch(1, 2)
        detail_layout.addLayout(self.frequent_layout)
        detail_layout.addWidget(self._build_equipment_summary())
        self.edit_settings = QWidget()
        self.edit_settings.setObjectName("CharacterEditSettings")
        settings = QGridLayout(self.edit_settings)
        self.settings_layout = settings
        settings.setContentsMargins(0, 0, 0, 0)
        self.setting_groups = (
            self._build_level_panel(),
            self._build_weapon_team_panel(),
            self._build_identity_panel(),
            self._build_goal_panel(),
        )
        for index, group in enumerate(self.setting_groups):
            settings.addWidget(group, index // 2, index % 2)
        self.echo_management_toggle = QPushButton("展開配裝管理")
        self.echo_management_toggle.setCheckable(True)
        self.echo_management_toggle.setObjectName("CharacterEchoManagementToggle")
        settings.addWidget(self.echo_management_toggle, 2, 0, 1, 2)
        settings.addWidget(self._build_echo_panel(), 3, 0, 1, 2)
        self.echo_group.hide()
        self.echo_management_toggle.toggled.connect(self.echo_group.setVisible)
        settings.setColumnStretch(0, 1)
        settings.setColumnStretch(1, 1)
        self.edit_settings.hide()
        detail_layout.addWidget(self.edit_settings)
        detail_layout.addStretch(1)
        self.detail_scroll.setWidget(detail)
        self._detail_layout_columns: tuple[int, int, int] | None = None
        self.detail_scroll.viewport().installEventFilter(self)

        editor = QWidget()
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.addLayout(self._build_detail_actions())
        editor_layout.addWidget(self.detail_scroll, 1)

        self.detail_stack.addWidget(empty)
        self.detail_stack.addWidget(editor)
        return self.detail_stack

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.detail_scroll.viewport() and event.type() == QEvent.Type.Resize:
            self._arrange_detail_panels(self.detail_scroll.viewport().width())
        return super().eventFilter(watched, event)

    def _arrange_detail_panels(self, width: int) -> None:
        frequent_columns = 1 if width < 660 else 2
        setting_columns = 1 if width < 820 else 2
        echo_columns = 1 if width < 620 else 2 if width < 900 else 3
        columns = (frequent_columns, setting_columns, echo_columns)
        if columns == self._detail_layout_columns:
            return
        self._detail_layout_columns = columns
        for group in (self.skill_group, self.node_group):
            self.frequent_layout.removeWidget(group)
        self.frequent_layout.addWidget(self.skill_group, 0, 0)
        self.frequent_layout.addWidget(
            self.node_group,
            1 if frequent_columns == 1 else 0,
            0 if frequent_columns == 1 else 1,
        )
        self.frequent_layout.setColumnStretch(0, 3)
        self.frequent_layout.setColumnStretch(1, 0 if frequent_columns == 1 else 2)
        node_columns = 2
        for check in self.attribute_node_checks:
            self.node_grid.removeWidget(check)
        for index, check in enumerate(self.attribute_node_checks):
            self.node_grid.addWidget(check, index // node_columns, index % node_columns)
        for widget in (*self.setting_groups, self.echo_management_toggle, self.echo_group):
            self.settings_layout.removeWidget(widget)
        for index, group in enumerate(self.setting_groups):
            self.settings_layout.addWidget(group, index // setting_columns, index % setting_columns)
        next_row = len(self.setting_groups) // setting_columns
        self.settings_layout.addWidget(self.echo_management_toggle, next_row, 0, 1, setting_columns)
        self.settings_layout.addWidget(self.echo_group, next_row + 1, 0, 1, setting_columns)
        self.settings_layout.setColumnStretch(1, 0 if setting_columns == 1 else 1)
        for card in self.echo_cards:
            self.echo_grid.removeWidget(card)
        self.echo_grid.removeWidget(self.unpositioned_echoes_label)
        for index, card in enumerate(self.echo_cards):
            self.echo_grid.addWidget(card, index // echo_columns, index % echo_columns)
        self.echo_grid.addWidget(
            self.unpositioned_echoes_label,
            (len(self.echo_cards) - 1) // echo_columns + 1,
            0,
            1,
            echo_columns,
        )
        for column in range(3):
            self.echo_grid.setColumnStretch(column, 1 if column < echo_columns else 0)

    def _build_progress_panel(self) -> QFrame:
        group = QFrame()
        group.setObjectName("CharacterSummaryCard")
        self.character_portrait = CharacterPortraitLabel("", 72)
        self.character_name_label = QLabel("選擇角色")
        self.character_name_label.setObjectName("CharacterSelectedName")
        self.character_identity_label = QLabel()
        self.character_identity_label.setObjectName("CharacterSelectedIdentity")
        self.character_identity_label.setWordWrap(True)
        self.character_equipment_label = QLabel()
        self.character_equipment_label.setWordWrap(True)
        self.equipment_button = QPushButton("查看聲骸配裝")
        self.equipment_button.setObjectName("CharacterEquipmentButton")
        self.equipment_button.clicked.connect(self._open_current_equipment)
        self.echo_average_label = QLabel("資料待補")
        self.echo_average_label.setObjectName("CharacterEchoAverageValue")
        self.echo_average_label.setWordWrap(True)
        self.progress_label = QLabel("—")
        self.progress_label.setObjectName("CharacterSeparateProgress")
        self.progress_label.setWordWrap(True)
        self.passing_echoes_label = QLabel("—")
        self.passing_echoes_label.setObjectName("CharacterPassingEchoesValue")
        self.weakest_position_label = QLabel("—")
        self.weakest_position_label.setObjectName("CharacterWeakestPositionValue")
        self.next_suggestion_label = QLabel("—")
        self.next_suggestion_label.setObjectName("CharacterNextSuggestionValue")
        self.next_suggestion_label.setWordWrap(True)
        # Full equipment diagnostics belong to the equipment workspace.
        for label in (
            self.echo_average_label,
            self.passing_echoes_label,
            self.weakest_position_label,
            self.next_suggestion_label,
        ):
            label.setParent(group)
            label.hide()
        grid = QGridLayout(group)
        grid.setContentsMargins(14, 12, 14, 12)
        grid.addWidget(self.character_portrait, 0, 0, 3, 1)
        grid.addWidget(self.character_name_label, 0, 1)
        grid.addWidget(self.character_identity_label, 1, 1)
        grid.addWidget(self.character_equipment_label, 2, 1)
        grid.addWidget(self.equipment_button, 0, 2)
        grid.addWidget(self.progress_label, 3, 0, 1, 3)
        grid.setColumnStretch(1, 1)
        return group

    def _open_current_equipment(self) -> None:
        if self._detail is not None:
            self.open_equipment_character(self._detail.identifier)

    def _build_equipment_summary(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("CharacterEquipmentSummary")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 12, 16, 12)
        title = QLabel("目前配裝 · 五件聲骸")
        title.setObjectName("CharacterEquipmentSummaryTitle")
        layout.addWidget(title)
        self.equipment_summary_label = QLabel("尚未取得配裝資料")
        self.equipment_summary_label.setObjectName("CharacterEquipmentScoreSummary")
        apply_score_color(self.equipment_summary_label, None)
        self.equipment_summary_label.setWordWrap(True)
        layout.addWidget(self.equipment_summary_label)
        slots = QHBoxLayout()
        self.equipment_summary_slots: dict[int, tuple[QLabel, QLabel, QPushButton]] = {}
        for position in range(1, 6):
            column = QVBoxLayout()
            score = QLabel("待評分")
            score.setObjectName("EquipmentSlotScore")
            apply_score_color(score, None)
            score.setAlignment(Qt.AlignmentFlag.AlignCenter)
            main = QLabel("主詞條待辨識")
            main.setWordWrap(True)
            main.setAlignment(Qt.AlignmentFlag.AlignCenter)
            button = QPushButton(f"位置 {position}")
            button.setToolTip(f"查看位置 {position} 的聲骸與副詞條")
            button.setProperty("role", "quiet")
            button.clicked.connect(partial(self._open_summary_piece, position))
            column.addWidget(score)
            column.addWidget(main)
            column.addWidget(button)
            slots.addLayout(column, 1)
            self.equipment_summary_slots[position] = score, main, button
        layout.addLayout(slots)
        return panel

    def _open_summary_piece(self, position: int) -> None:
        if self._detail is not None:
            self.open_equipment_piece(self._detail.identifier, position)

    def _update_progress_summary(self) -> None:
        detail = self._detail
        equipment = None if detail is None else self._equipment_details.get(detail.identifier)
        self.echo_average_label.setText(
            "資料待補" if equipment is None else equipment.score_summary
        )
        self.equipment_summary_label.setText(
            "尚未取得配裝資料" if equipment is None else equipment.score_summary
        )
        for summary in (self.echo_average_label, self.equipment_summary_label):
            apply_score_color(summary, None if equipment is None else equipment.average_score)
        pieces = {} if equipment is None else {piece.position: piece for piece in equipment.pieces}
        for position, (score_label, main_label, button) in self.equipment_summary_slots.items():
            piece = pieces.get(position)
            score = None if piece is None else piece.score
            score_label.setText("待評分" if score is None else f"{score:.1f}")
            apply_score_color(score_label, score)
            main_label.setText(
                piece.main_stats[0] if piece is not None and piece.main_stats else "主詞條待辨識"
            )
            button.setEnabled(piece is not None)
        skills = self.skill_model.rows()
        done = sum(skill.current_level >= skill.target_level for skill in skills)
        weapon = (
            f"武器 Lv.{self.weapon_level_spin.value()}"
            if self.weapon_name_edit.text().strip()
            else "武器待登記"
        )
        self.progress_label.setText(
            f"目前填寫: 角色 Lv.{self.level_spin.value()} · {weapon} · 技能 {done}/5 達目標"
        )
        self.character_name_label.setText(self.name_edit.text() or "新角色")
        element = self.element_combo.currentText()
        self.character_identity_label.setText(
            f"{element} · {self.role_edit.text() or '定位待設定'} · "
            f"Lv.{self.level_spin.value()} · 共鳴鏈 {self.chain_spin.value()}"
        )
        weapon_name = self.weapon_name_edit.text().strip()
        self.character_equipment_label.setText(
            f"{weapon_name} · Lv.{self.weapon_level_spin.value()} · "
            f"諧振 {self.weapon_rank_spin.value()}"
            if weapon_name
            else "武器待登記"
        )

    def _build_level_panel(self) -> QGroupBox:
        group = QGroupBox("等級與共鳴鏈")
        self.level_spin = _spin("CharacterLevelSpin", 0, 90)
        self.chain_spin = _spin("CharacterChainSpin", 0, 6)
        self.level_max_button = QPushButton("填入 90")
        self.level_max_button.setObjectName("CharacterLevelMaxButton")
        self.level_max_button.clicked.connect(lambda: self.level_spin.setValue(90))
        level_row = QHBoxLayout()
        level_row.addWidget(self.level_spin, 1)
        level_row.addWidget(self.level_max_button)
        form = QFormLayout(group)
        form.addRow("等級", level_row)
        self.character_ascension_combo = _ascension_combo("CharacterAscension")
        form.addRow("突破階段", self.character_ascension_combo)
        form.addRow("共鳴鏈", self.chain_spin)
        return group

    def _build_identity_panel(self) -> QGroupBox:
        group = QGroupBox("角色資料")
        self.name_edit = QLineEdit()
        self.name_edit.setObjectName("CharacterNameEdit")
        self.element_combo = QComboBox()
        self.element_combo.setObjectName("CharacterElementCombo")
        self.element_combo.addItem("尚未設定", "")
        for element in _ELEMENTS:
            self.element_combo.addItem(element, element)
        self.role_edit = QLineEdit()
        self.role_edit.setObjectName("CharacterRoleEdit")
        self.candidate_check = QCheckBox("列入可能上場角色")
        self.candidate_check.setObjectName("CharacterCandidateCheck")
        self.priority_spin = _spin("CharacterPrioritySpin", 0, 999)
        self.scoring_profile_combo = QComboBox()
        self.scoring_profile_combo.setObjectName("CharacterScoringProfileCombo")
        self.scoring_profile_combo.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        self.panel_energy_spin = _NoWheelDoubleSpinBox()
        self.panel_energy_spin.setObjectName("CharacterPanelEnergySpin")
        self.panel_energy_spin.setRange(0, 1000)
        self.panel_energy_spin.setDecimals(1)
        self.panel_energy_spin.setSpecialValueText("未知(暫估)")
        self.panel_energy_spin.setSuffix("%")
        self.energy_target_spin = _NoWheelDoubleSpinBox()
        self.energy_target_spin.setObjectName("CharacterEnergyTargetSpin")
        self.energy_target_spin.setRange(0, 1000)
        self.energy_target_spin.setDecimals(1)
        self.energy_target_spin.setSpecialValueText("依攻略參考")
        self.energy_target_spin.setSuffix("%")
        self.energy_target_spin.setToolTip(
            "可依自己的隊伍或遊戲內推薦覆寫效率目標。只影響自動攻略評分。"
        )
        self.panel_energy_spin.setToolTip(
            "填目前完整配裝的面板共鳴效率, 不是單件加成。更換裝備後請更新; 0 表示未知。"
        )

        form = QFormLayout(group)
        form.addRow("名稱", self.name_edit)
        form.addRow("屬性", self.element_combo)
        form.addRow("定位", self.role_edit)
        form.addRow("使用狀態", self.candidate_check)
        form.addRow("優先級", self.priority_spin)
        form.addRow("評分設定檔", self.scoring_profile_combo)
        self.energy_recommendation_label = QLabel("依角色攻略提供建議")
        self.energy_recommendation_label.setWordWrap(True)
        form.addRow("建議共鳴效率", self.energy_recommendation_label)
        for control in (self.panel_energy_spin, self.energy_target_spin):
            control.setParent(group)
            control.hide()
        return group

    def _build_weapon_team_panel(self) -> QGroupBox:
        group = QGroupBox("武器與隊伍")
        self.weapon_name_edit = QLineEdit()
        self.weapon_name_edit.setObjectName("CharacterWeaponNameEdit")
        self.weapon_name_edit.setPlaceholderText("填寫目前裝備的武器名稱")
        self.weapon_level_spin = _spin("CharacterWeaponLevelSpin", 0, 90)
        self.weapon_rank_spin = _spin("CharacterWeaponRankSpin", 1, 5)
        self.weapon_edit_button = QPushButton("編輯武器資料")
        self.weapon_edit_button.setObjectName("CharacterWeaponEditButton")
        self.weapon_edit_button.clicked.connect(self._request_weapon_edit)
        self.team_label = QLabel("尚未設定")
        self.team_label.setObjectName("CharacterTeamValue")
        self.team_label.setWordWrap(True)
        self.team_edit_button = QPushButton("管理隊伍")
        self.team_edit_button.setObjectName("CharacterTeamEditButton")
        self.team_edit_button.clicked.connect(self._request_team_edit)

        form = QFormLayout(group)
        form.addRow("武器", self.weapon_name_edit)
        self.weapon_level_max_button = QPushButton("填入 90")
        self.weapon_level_max_button.setObjectName("CharacterWeaponLevelMaxButton")
        self.weapon_level_max_button.clicked.connect(lambda: self.weapon_level_spin.setValue(90))
        level_row = QHBoxLayout()
        level_row.addWidget(self.weapon_level_spin, 1)
        level_row.addWidget(self.weapon_level_max_button)
        form.addRow("武器等級", level_row)
        self.weapon_ascension_combo = _ascension_combo("CharacterWeaponAscension")
        form.addRow("突破階段", self.weapon_ascension_combo)
        form.addRow("諧振階級", self.weapon_rank_spin)
        form.addRow("武器操作", self.weapon_edit_button)
        form.addRow("隊伍", self.team_label)
        form.addRow("隊伍操作", self.team_edit_button)
        return group

    def _build_skill_panel(self) -> QGroupBox:
        group = QGroupBox("技能 · 雙擊等級修改, 由上往下優先升級")
        self.skill_model = SkillProgressTableModel(self)
        self.skill_table = QTableView()
        self.skill_table.setObjectName("CharacterSkillTable")
        self.skill_table.setModel(self.skill_model)
        self.skill_table.setItemDelegate(_SkillLevelDelegate(self.skill_table))
        self.skill_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.skill_table.horizontalHeader().setStretchLastSection(False)
        self.skill_table.verticalHeader().setVisible(False)
        self.skill_table.verticalHeader().setDefaultSectionSize(32)
        self.skill_table.setFixedHeight(224)
        self.skill_table.horizontalHeader().setMinimumSectionSize(38)
        self.skill_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.skill_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        layout = QVBoxLayout(group)
        self.skills_target_button = QPushButton("目前技能全部填至目標")
        self.skills_target_button.setObjectName("CharacterSkillsTargetButton")
        self.skills_target_button.clicked.connect(self._fill_skill_targets)
        layout.addWidget(self.skills_target_button)
        priority_controls = QHBoxLayout()
        self.skill_up_button = QPushButton("↑ 提前升級")
        self.skill_down_button = QPushButton("↓ 延後升級")
        self.skill_up_button.clicked.connect(lambda: self._move_skill_priority(-1))
        self.skill_down_button.clicked.connect(lambda: self._move_skill_priority(1))
        priority_controls.addWidget(self.skill_up_button)
        priority_controls.addWidget(self.skill_down_button)
        layout.addLayout(priority_controls)
        layout.addWidget(self.skill_table)
        return group

    def _move_skill_priority(self, direction: int) -> None:
        row = self.skill_table.currentIndex().row()
        if self.skill_model.move_priority(row, direction):
            self.skill_table.setCurrentIndex(self.skill_model.index(row + direction, 0))

    def _fill_skill_targets(self) -> None:
        for row, skill in enumerate(self.skill_model.rows()):
            self.skill_model.setData(
                self.skill_model.index(row, 1), max(skill.current_level, skill.target_level)
            )

    def _build_node_panel(self) -> QGroupBox:
        group = QGroupBox("技能分支節點 · 屬性與固有技能")
        self.attribute_node_checks: list[QCheckBox] = []
        grid = QGridLayout(group)
        self.node_grid = grid
        for index in range(10):
            check = QCheckBox(f"節點 {index + 1}")
            check.setObjectName(f"CharacterAttributeNode{index + 1}")
            self.attribute_node_checks.append(check)
            grid.addWidget(check, index // 2, index % 2)
        self.unlock_nodes_button = QPushButton("全部節點已解鎖")
        self.unlock_nodes_button.setObjectName("CharacterUnlockAllNodes")
        self.unlock_nodes_button.clicked.connect(self._unlock_all_nodes)
        grid.addWidget(self.unlock_nodes_button, 10, 0, 1, 2)
        return group

    def _unlock_all_nodes(self) -> None:
        for check in self.attribute_node_checks:
            check.setChecked(True)

    def _build_stats_panel(self) -> QGroupBox:
        group = QGroupBox("角色數值 · 非戰鬥常態")
        group.setObjectName("CharacterPanelStats")
        layout = QVBoxLayout(group)
        self.panel_status_label = QLabel("依已儲存資料計算;編輯後請儲存角色。")
        self.panel_status_label.setWordWrap(True)
        self.panel_status_label.setObjectName("CharacterPanelStatus")
        layout.addWidget(self.panel_status_label)
        grid = QGridLayout()
        self.panel_stat_labels: dict[str, QLabel] = {}
        for index, key in enumerate(CORE_STATS):
            value = QLabel(f"{STAT_LABELS[key]}　待補資料")
            value.setObjectName(f"CharacterPanel_{key}")
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(value, index // 2, index % 2)
            self.panel_stat_labels[key] = value
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)
        self.panel_details_button = TextDetailsButton("全部屬性與來源明細", group)
        self.panel_details_button.setObjectName("CharacterPanelDetails")
        layout.addWidget(self.panel_details_button)
        return group

    def _apply_calculated_panel(self, detail: CharacterDetailView) -> None:
        panel = detail.calculated_panel
        for key, label in self.panel_stat_labels.items():
            label.setText(f"{STAT_LABELS[key]}　{panel.display(key) if panel else '待補資料'}")
        if panel is None:
            self.panel_status_label.setText("尚無角色數值;儲存角色後重新計算。")
            self.panel_details_button.set_details("角色數值", "")
            for index, check in enumerate(self.attribute_node_checks):
                check.setText(f"節點 {index + 1}")
                check.setToolTip("")
            return
        if panel.complete:
            status = (
                "已與登記的遊戲面板一致"
                if panel.game_verified
                else f"已登記遊戲面板,仍有{len(panel.comparison_differences)}項差異;請查看來源明細"
                if panel.comparison_differences
                else "本配置尚未逐項實測"
            )
            message = f"{status}。依已儲存資料計算;編輯後請儲存角色。"
        else:
            message = "待補資料:" + ";".join(panel.missing[:2])
            if len(panel.missing) > 2:
                message += f",另有{len(panel.missing) - 2}項,詳見來源明細。"
        self.panel_status_label.setText(message)
        self.panel_details_button.set_details("角色數值・全部屬性與來源明細", panel.details())
        for index, check in enumerate(self.attribute_node_checks):
            node_label = (
                panel.node_labels[index] if panel.node_labels else f"節點 {index + 1}(來源待補)"
            )
            short = node_label.replace("常態攻擊", "普攻").replace("共鳴技能", "技能")
            short = short.replace("共鳴解放", "解放").replace("變奏技能", "變奏").replace(" ", "")
            check.setText(short if len(short) <= 17 else short[:16] + "…")
            check.setToolTip(node_label)

    def _build_echo_panel(self) -> QGroupBox:
        group = QGroupBox("目前裝備 · 聲骸評分")
        self.echo_group = group
        self.echo_cards: list[_EchoSlotCard] = []
        grid = QGridLayout(group)
        self.echo_grid = grid
        self.unpositioned_echoes_label = QLabel()
        self.unpositioned_echoes_label.setObjectName("CharacterRecognizedUnpositionedEchoes")
        self.unpositioned_echoes_label.setWordWrap(True)
        self.unpositioned_echoes_label.setVisible(False)
        grid.addWidget(self.unpositioned_echoes_label, 2, 0, 1, 3)
        for position in range(1, 6):
            card = _EchoSlotCard(position, partial(self._request_echo_slot_edit, position))
            self.echo_cards.append(card)
            grid.addWidget(card, (position - 1) // 3, (position - 1) % 3)
        for column in range(3):
            grid.setColumnStretch(column, 1)
        return group

    @Slot(str)
    def open_equipment_character(self, character_id: str) -> None:
        if character_id in self._equipment_details:
            self.equipment_detail_page._focused_slot = None
            self.equipment_detail_page.apply_detail(self._equipment_details[character_id])
            self.overview_page.set_selected_character(character_id)
            self.equipment_workspace.setCurrentIndex(1)
            self.equipment_requested.emit()
            return
        self.open_character_editor(character_id)

    @Slot()
    def go_equipment_overview(self) -> None:
        self.equipment_workspace.setCurrentIndex(0)
        self.equipment_requested.emit()

    @Slot(str)
    def open_character_editor(self, character_id: str) -> None:
        self.select_character(character_id)
        self.detail_scroll.verticalScrollBar().setValue(0)
        self.cultivation_requested.emit()

    @Slot(str, int)
    def open_equipment_piece(self, character_id: str, slot: int) -> None:
        if character_id in self._equipment_details:
            self.equipment_detail_page.apply_detail(self._equipment_details[character_id])
            self.overview_page.set_selected_character(character_id, slot)
            self.equipment_workspace.setCurrentIndex(1)
            self.equipment_detail_page.focus_slot(slot)
            self.equipment_requested.emit()
            return
        self.open_character_editor(character_id)
        if 1 <= slot <= 5:
            self.edit_mode_button.setChecked(True)
            self.echo_management_toggle.setChecked(True)
            self.detail_scroll.ensureWidgetVisible(self.echo_cards[slot - 1])

    @Slot(int)
    def _equipment_slot_selected(self, slot: int) -> None:
        detail = self.equipment_detail_page._detail
        if detail is not None:
            self.overview_page.set_selected_character(detail.identifier, slot)
            self.equipment_workspace.setCurrentIndex(1)
            self.equipment_requested.emit()

    def set_equipment_details(self, details: tuple[GearCharacterDetailView, ...]) -> None:
        self._equipment_details = {item.identifier: item for item in details}
        self._update_progress_summary()
        active = self.equipment_detail_page._detail
        if active is not None and active.identifier in self._equipment_details:
            scroll = self.equipment_detail_page.scroll_area.verticalScrollBar().value()
            self.equipment_detail_page.apply_detail(self._equipment_details[active.identifier])
            self.equipment_detail_page.scroll_area.verticalScrollBar().setValue(scroll)
        elif details:
            first = next(
                (
                    identifier
                    for identifier in self.overview_page.visible_character_ids
                    if identifier in self._equipment_details
                ),
                details[0].identifier,
            )
            self.equipment_detail_page.apply_detail(self._equipment_details[first])
            self.overview_page.set_selected_character(first)
        else:
            self.equipment_detail_page.clear_detail()
            self.overview_page.set_selected_character(None)

    def _build_goal_panel(self) -> QGroupBox:
        group = QGroupBox("養成目標與備註")
        self.goal_edit = QTextEdit()
        self.goal_edit.setObjectName("CharacterGoalEdit")
        self.goal_edit.setPlaceholderText("例如: 核心技能 10、次要技能 6, 五顆聲骸達到及格。")
        self.goal_edit.setMinimumHeight(90)
        self.notes_edit = QTextEdit()
        self.notes_edit.setObjectName("CharacterNotesEdit")
        self.notes_edit.setMinimumHeight(90)
        layout = QFormLayout(group)
        layout.addRow("養成目標", self.goal_edit)
        layout.addRow("備註", self.notes_edit)
        return group

    def _build_detail_actions(self) -> QHBoxLayout:
        self.edit_mode_button = QPushButton("編輯角色資料")
        self.edit_mode_button.setObjectName("CharacterEditModeButton")
        self.edit_mode_button.setCheckable(True)
        self.edit_mode_button.toggled.connect(self._set_edit_mode)
        self.save_button = QPushButton("儲存角色")
        self.save_button.setObjectName("CharacterSaveButton")
        self.save_button.clicked.connect(self._request_edit)
        self.archive_button = QPushButton("封存角色")
        self.archive_button.setObjectName("CharacterArchiveButton")
        self.archive_button.clicked.connect(self._request_archive)
        self.archive_button.hide()
        layout = QHBoxLayout()
        layout.addWidget(self.edit_mode_button)
        layout.addStretch(1)
        layout.addWidget(self.archive_button)
        layout.addWidget(self.save_button)
        return layout

    @Slot(bool)
    def _set_edit_mode(self, editing: bool) -> None:
        self.edit_settings.setVisible(editing)
        self.archive_button.setVisible(editing)
        self.edit_mode_button.setText("完成編輯 · 回到查看" if editing else "編輯角色資料")
        self._update_progress_summary()

    @Slot(object)
    def set_characters(self, rows: object) -> None:
        if not isinstance(rows, (list, tuple)) or not all(
            isinstance(row, CharacterSummaryView) for row in rows
        ):
            raise TypeError("rows must be a list or tuple of CharacterSummaryView")
        selected_id = self.current_character_id()
        self._all_character_rows = tuple(rows)
        visible = self._visible_character_rows()
        self.character_model.set_rows(visible)
        self.set_loading(False)
        self.status_label.setProperty("error", False)
        self.status_label.setText(f"已載入 {len(visible)} 名角色。")
        _repolish(self.status_label)
        if selected_id is not None and self.select_character(selected_id):
            return
        if not visible:
            self.clear_detail(
                "已封存的角色已隱藏; 勾選「封存」可查看或還原。"
                if rows
                else "掃描已裝備的聲骸後, 辨識到的角色會自動加入。\n"
                "新角色的攻略與評分設定可再請助理更新。"
            )
        elif self._detail is not None and not any(
            row.identifier == self._detail.identifier for row in visible
        ):
            self.clear_detail()

    def _visible_character_rows(self) -> tuple[CharacterSummaryView, ...]:
        archived_count = sum(row.archived for row in self._all_character_rows)
        self.show_archived_check.setText(f"封存 ({archived_count})")
        return tuple(
            row
            for row in self._all_character_rows
            if not row.archived or self.show_archived_check.isChecked()
        )

    @Slot(bool)
    def _toggle_archived_visibility(self, _checked: bool) -> None:
        selected_id = self.current_character_id()
        blocker = QSignalBlocker(self.character_table.selectionModel())
        visible = self._visible_character_rows()
        self.character_model.set_rows(visible)
        retained = selected_id is not None and self.select_character(selected_id)
        del blocker
        if retained or self._creating:
            return
        self.clear_detail("沒有可顯示的角色; 掃描已裝備的聲骸或切換封存顯示。")
        if visible:
            self.select_character(visible[0].identifier)

    @Slot(object)
    def set_scoring_profiles(self, options: object) -> None:
        if not isinstance(options, (list, tuple)) or not all(
            isinstance(option, ChoiceOptionView) for option in options
        ):
            raise TypeError("options must be a list or tuple of ChoiceOptionView")
        selected = self.scoring_profile_combo.currentData()
        blocker = QSignalBlocker(self.scoring_profile_combo)
        self.scoring_profile_combo.clear()
        self.scoring_profile_combo.addItem("自動: 依角色攻略(未知角色待設定)", None)
        for option in options:
            self.scoring_profile_combo.addItem(option.label, option.identifier)
        index = self.scoring_profile_combo.findData(selected)
        self.scoring_profile_combo.setCurrentIndex(max(index, 0))
        del blocker

    def apply_external_characters(
        self,
        rows: tuple[CharacterSummaryView, ...],
        details: tuple[CharacterDetailView, ...],
    ) -> None:
        """Update background roster/equipment data without touching editable values."""

        selected_id = self.current_character_id()
        blocker = QSignalBlocker(self.character_table.selectionModel())
        self._all_character_rows = rows
        visible = self._visible_character_rows()
        self.character_model.set_rows(visible)
        if selected_id is not None:
            self.select_character(selected_id)
        else:
            self.character_table.setCurrentIndex(QModelIndex())
        del blocker
        if self._creating:
            return
        by_id = {detail.identifier: detail for detail in details}
        if self._detail is not None and not any(
            row.identifier == self._detail.identifier for row in visible
        ):
            self.clear_detail("已封存的角色已隱藏; 勾選「封存」可查看或還原。")
        if self._detail is None:
            first = next(iter(visible), None)
            if first is not None:
                self.select_character(first.identifier)
                self.apply_character(by_id[first.identifier])
            return
        detail = by_id.get(self._detail.identifier)
        if detail is None:
            return
        old_slots = {slot.position: slot for slot in self._detail.echo_slots}
        for card, slot in zip(
            self.echo_cards, sorted(detail.echo_slots, key=lambda item: item.position), strict=True
        ):
            pending_validity = card.validity()
            card.apply_slot(slot)
            if old_slots[slot.position].echo_id == slot.echo_id:
                # An unrelated roster update must not reset unsaved confirmations.
                _select_validity(card.set_validity_combo, pending_validity[0])
                _select_validity(card.main_validity_combo, pending_validity[1])
        self._detail = detail
        self.team_label.setText(detail.team_name or "尚未設定")
        self._apply_calculated_panel(detail)
        self._apply_unpositioned_echoes(detail)
        self._update_progress_summary()
        self.passing_echoes_label.setText(f"{detail.passing_echoes} / 5")
        self.weakest_position_label.setText(detail.weakest_position or "尚未評估")
        self.next_suggestion_label.setText(detail.next_suggestion or "尚未建立養成建議")

    @Slot(object)
    def apply_character(self, detail: object) -> None:
        if not isinstance(detail, CharacterDetailView):
            raise TypeError("detail must be CharacterDetailView")
        self._detail = detail
        self._creating = False
        self._apply_calculated_panel(detail)
        self.edit_mode_button.setChecked(False)
        self.character_portrait.set_character_name(detail.name)
        self._apply_unpositioned_echoes(detail)
        self._apply_element(detail.element)
        self.name_edit.setText(detail.name)
        self.role_edit.setText(detail.role)
        self.candidate_check.setChecked(detail.is_candidate)
        self.priority_spin.setValue(detail.priority)
        self.level_spin.setValue(detail.level)
        self.character_ascension_combo.setCurrentIndex(
            max(0, self.character_ascension_combo.findData(detail.character_ascension))
        )
        self.chain_spin.setValue(detail.resonance_chain)
        self._apply_profile(detail.scoring_profile_id, detail.scoring_profile_name)
        self.panel_energy_spin.setValue(detail.panel_energy_regen or 0)
        self.energy_target_spin.setValue(detail.energy_target_override or 0)
        guide = guide_for_character(detail.name)
        target = detail.energy_target_override or (guide.energy_target if guide else None)
        self.energy_recommendation_label.setText(
            f"{target:g}% · 配裝建議" if target is not None else "此角色尚無已查核建議值"
        )
        self.weapon_name_edit.setText(detail.weapon.name or "")
        self.weapon_level_spin.setValue(detail.weapon.level or 0)
        self.weapon_ascension_combo.setCurrentIndex(
            max(0, self.weapon_ascension_combo.findData(detail.weapon_ascension))
        )
        self.weapon_rank_spin.setValue(detail.weapon.resonance_rank or 1)
        self.team_label.setText(detail.team_name or "尚未設定")
        self.skill_model.set_rows(detail.skills)
        for check, completed in zip(
            self.attribute_node_checks, detail.attribute_nodes, strict=True
        ):
            check.setChecked(completed)
        for card, slot in zip(
            self.echo_cards, sorted(detail.echo_slots, key=lambda item: item.position), strict=True
        ):
            card.apply_slot(slot)
        self.goal_edit.setPlainText(detail.cultivation_goal)
        self.notes_edit.setPlainText(detail.notes)
        self._update_progress_summary()
        self.passing_echoes_label.setText(f"{detail.passing_echoes} / 5")
        self.weakest_position_label.setText(detail.weakest_position or "尚未評估")
        self.next_suggestion_label.setText(detail.next_suggestion or "尚未建立養成建議")
        self.archive_button.setText("取消封存" if detail.archived else "封存角色")
        self._set_detail_enabled(True)
        self.detail_stack.setCurrentIndex(1)
        self.status_label.setProperty("error", False)
        self.status_label.setText(f"{detail.name} · 已同步")
        _repolish(self.status_label)

    @Slot()
    @Slot(str)
    def clear_detail(self, message: str = "從右側選擇角色; 掃描聲骸會自動更新角色清單。") -> None:
        self._detail = None
        self._creating = False
        self.empty_state_label.setText(message)
        self.detail_stack.setCurrentIndex(0)
        self._set_detail_enabled(False)

    def _apply_unpositioned_echoes(self, detail: CharacterDetailView) -> None:
        self.unpositioned_echoes_label.setVisible(bool(detail.unpositioned_echoes))
        self.unpositioned_echoes_label.setText(
            f"已登記裝備 ({len(detail.unpositioned_echoes)} 筆, 位置待確認)\n"
            + "\n".join(f"• {label}" for label in detail.unpositioned_echoes)
            + "\n這些紀錄已登記所屬角色, 但未能確認位置。新辨識到的位置會自動顯示於上方欄位。"
        )

    @Slot(bool)
    @Slot(bool, str)
    def set_loading(self, loading: bool, message: str = "正在載入角色…") -> None:
        self._loading = loading
        self.catalog_button.setEnabled(not loading)
        self.add_button.setEnabled(not loading)
        self.refresh_button.setEnabled(not loading)
        self.show_archived_check.setEnabled(not loading)
        self.character_table.setEnabled(not loading)
        self.status_label.setProperty("error", False)
        if loading:
            self.status_label.setText(message)
        else:
            self._set_detail_enabled(self._detail is not None or self._creating)
        _repolish(self.status_label)

    @Slot(str)
    def show_error(self, message: str) -> None:
        self.set_loading(False)
        self.status_label.setProperty("error", True)
        self.status_label.setText(f"無法載入角色: {message}")
        _repolish(self.status_label)

    @Slot(str)
    def show_status(self, message: str) -> None:
        self.set_loading(False)
        self.status_label.setProperty("error", False)
        self.status_label.setText(message)
        _repolish(self.status_label)

    def current_character_id(self) -> str | None:
        current = self.character_table.currentIndex()
        if not current.isValid():
            return self._detail.identifier if self._detail is not None else None
        return self.character_model.character_id_at(current.row())

    def select_character(self, identifier: str) -> bool:
        row = self.character_model.row_for_character(identifier)
        if row is None:
            return False
        index = self.character_model.index(row, 0)
        self.character_table.setCurrentIndex(index)
        self.character_table.scrollTo(index)
        return True

    @Slot()
    def focus_weapon_editor(self) -> None:
        """Make the inline weapon editor action explicit and keyboard-ready."""

        self.cultivation_requested.emit()
        self.edit_mode_button.setChecked(True)
        self.detail_scroll.ensureWidgetVisible(self.weapon_name_edit)
        self.weapon_name_edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.status_label.setProperty("error", False)
        self.status_label.setText("可直接編輯武器名稱、等級與諧振階級,完成後儲存角色。")
        _repolish(self.status_label)

    def build_edit_request(self) -> CharacterEditRequest:
        detail = self._detail
        if detail is None and not self._creating:
            raise RuntimeError("no character is selected")
        name = self.name_edit.text().strip()
        if not name:
            raise ValueError("角色名稱不可空白。")
        profile_data = self.scoring_profile_combo.currentData()
        profile_id = profile_data if isinstance(profile_data, str) and profile_data else None
        weapon_name = self.weapon_name_edit.text().strip() or None
        weapon = WeaponProgressView(
            name=weapon_name,
            level=self.weapon_level_spin.value() if weapon_name is not None else None,
            resonance_rank=self.weapon_rank_spin.value() if weapon_name is not None else None,
        )
        return CharacterEditRequest(
            identifier=detail.identifier if detail is not None else None,
            name=name,
            element=str(self.element_combo.currentData() or ""),
            role=self.role_edit.text().strip(),
            is_candidate=self.candidate_check.isChecked(),
            priority=self.priority_spin.value(),
            level=self.level_spin.value(),
            resonance_chain=self.chain_spin.value(),
            scoring_profile_id=profile_id,
            weapon=weapon,
            team_id=detail.team_id if detail is not None else None,
            skills=self.skill_model.rows(),
            attribute_nodes=tuple(check.isChecked() for check in self.attribute_node_checks),
            cultivation_goal=self.goal_edit.toPlainText().strip(),
            notes=self.notes_edit.toPlainText().strip(),
            echo_validations=tuple(card.validity() for card in self.echo_cards),
            panel_energy_regen=self.panel_energy_spin.value() or None,
            energy_target_override=self.energy_target_spin.value() or None,
            character_ascension=self.character_ascension_combo.currentData(),
            weapon_ascension=self.weapon_ascension_combo.currentData(),
        )

    def _apply_element(self, value: str) -> None:
        index = self.element_combo.findData(value)
        if index < 0 and value:
            self.element_combo.addItem(value, value)
            index = self.element_combo.count() - 1
        self.element_combo.setCurrentIndex(max(index, 0))

    def _apply_profile(self, identifier: str | None, label: str | None) -> None:
        index = self.scoring_profile_combo.findData(identifier)
        if identifier is not None and index < 0:
            self.scoring_profile_combo.addItem(label or identifier, identifier)
            index = self.scoring_profile_combo.count() - 1
        self.scoring_profile_combo.setCurrentIndex(max(index, 0))

    def _set_detail_enabled(self, enabled: bool) -> None:
        actual = enabled and not self._loading
        self.detail_scroll.setEnabled(actual)
        self.save_button.setEnabled(actual)
        self.edit_mode_button.setEnabled(actual)
        self.archive_button.setEnabled(actual and not self._creating)

    def _request_add(self) -> None:
        self.cultivation_requested.emit()
        self.character_table.clearSelection()
        self.character_table.setCurrentIndex(QModelIndex())
        self._detail = None
        self._creating = True
        self._reset_editor_for_add()
        self.edit_mode_button.setChecked(True)
        self.detail_stack.setCurrentIndex(1)
        self._set_detail_enabled(True)
        self.status_label.setProperty("error", False)
        self.status_label.setText("正在新增角色; 完成後按下「儲存角色」。")
        _repolish(self.status_label)
        self.name_edit.setFocus()
        self.add_requested.emit()

    def _request_refresh(self) -> None:
        self.set_loading(True)
        self.refresh_requested.emit()

    def _on_current_character_changed(self, current: QModelIndex, previous: QModelIndex) -> None:
        del previous
        if current.isValid():
            identifier = self.character_model.character_id_at(current.row())
            if identifier is not None:
                self.character_selected.emit(identifier)

    def _request_edit(self) -> None:
        try:
            request = self.build_edit_request()
        except (RuntimeError, ValueError) as error:
            self.status_label.setProperty("error", True)
            self.status_label.setText(str(error))
            _repolish(self.status_label)
            return
        self.status_label.setProperty("error", False)
        self.status_label.setText("正在儲存角色資料…")
        _repolish(self.status_label)
        self.edit_requested.emit(request)

    def _request_archive(self) -> None:
        if self._detail is not None:
            self.archive_requested.emit(self._detail.identifier)

    def _request_weapon_edit(self) -> None:
        if self._detail is not None:
            self.weapon_edit_requested.emit(self._detail.identifier)

    def _request_team_edit(self) -> None:
        if self._detail is not None:
            self.team_edit_requested.emit(self._detail.identifier)

    def _request_echo_slot_edit(self, position: int) -> None:
        if self._detail is not None:
            self.echo_slot_edit_requested.emit(self._detail.identifier, position)

    def _reset_editor_for_add(self) -> None:
        self.name_edit.clear()
        self.element_combo.setCurrentIndex(0)
        self.role_edit.clear()
        self.candidate_check.setChecked(True)
        self.priority_spin.setValue(1)
        self.level_spin.setValue(1)
        self.character_ascension_combo.setCurrentIndex(0)
        self.chain_spin.setValue(0)
        self.scoring_profile_combo.setCurrentIndex(0)
        self.panel_energy_spin.setValue(0)
        self.energy_target_spin.setValue(0)
        self.energy_recommendation_label.setText("儲存角色後依攻略顯示建議")
        self.weapon_name_edit.clear()
        self.weapon_level_spin.setValue(1)
        self.weapon_ascension_combo.setCurrentIndex(0)
        self.weapon_rank_spin.setValue(1)
        self.team_label.setText("尚未設定")
        self.skill_model.set_rows(
            (
                SkillProgressView("normal_attack", "普攻", 0, 6, False),
                SkillProgressView("resonance_skill", "共鳴技能", 0, 10, True),
                SkillProgressView("forte_circuit", "共鳴迴路", 0, 10, True),
                SkillProgressView("resonance_liberation", "共鳴解放", 0, 10, True),
                SkillProgressView("intro_skill", "變奏技能", 0, 6, False),
            )
        )
        for index, check in enumerate(self.attribute_node_checks):
            check.setChecked(False)
            check.setText(f"節點 {index + 1}")
            check.setToolTip("")
        for key, label in self.panel_stat_labels.items():
            label.setText(f"{STAT_LABELS[key]}　待補資料")
        self.panel_status_label.setText("儲存角色後計算數值。")
        self.panel_details_button.set_details("角色數值", "")
        for position, card in enumerate(self.echo_cards, start=1):
            card.apply_slot(PermanentEchoSlotView(position))
        self.goal_edit.clear()
        self.notes_edit.clear()
        self.echo_average_label.setText("資料待補")
        apply_score_color(self.echo_average_label, None)
        self.progress_label.setText("填寫角色等級、武器與技能後儲存。")
        self.passing_echoes_label.setText("0 / 5")
        self.weakest_position_label.setText("尚未評估")
        self.next_suggestion_label.setText("先設定養成目標與評分設定檔")
        self.archive_button.setText("封存角色")


class _NoWheelSpinBox(QSpinBox):
    def wheelEvent(self, event: QWheelEvent) -> None:
        event.ignore()


class _NoWheelDoubleSpinBox(QDoubleSpinBox):
    def wheelEvent(self, event: QWheelEvent) -> None:
        event.ignore()


class _SkillLevelDelegate(QStyledItemDelegate):
    def createEditor(
        self, parent: QWidget, option: QStyleOptionViewItem, index: ModelIndex
    ) -> QWidget:
        if index.column() in (1, 2):
            editor = _NoWheelSpinBox(parent)
            editor.setRange(0 if index.column() == 1 else 1, 10)
            return editor
        return super().createEditor(parent, option, index)


def _spin(object_name: str, minimum: int, maximum: int) -> QSpinBox:
    spin = _NoWheelSpinBox()
    spin.setObjectName(object_name)
    spin.setRange(minimum, maximum)
    return spin


def _validity_combo(object_name: str) -> QComboBox:
    combo = QComboBox()
    combo.setObjectName(object_name)
    combo.addItem("待確認", None)
    combo.addItem("符合目標", True)
    combo.addItem("不符合目標", False)
    return combo


def _ascension_combo(object_name: str) -> QComboBox:
    combo = QComboBox()
    combo.setObjectName(object_name)
    combo.addItem("依等級;突破邊界需指定", None)
    for phase in range(7):
        combo.addItem("0階・未突破" if phase == 0 else f"{phase}階突破", phase)
    combo.setToolTip("20/40/50/60/70/80級可能有突破前後兩種數值,請選擇目前階段。")
    return combo


def _select_validity(combo: QComboBox, value: bool | None) -> None:
    index = combo.findData(value)
    combo.setCurrentIndex(max(index, 0))


def _combo_validity(combo: QComboBox) -> bool | None:
    value = combo.currentData()
    return value if type(value) is bool else None


def _coerce_level(value: object, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise TypeError("skill level must be an integer")
    level = int(value)
    if not minimum <= level <= 10:
        raise ValueError(f"skill level must be within {minimum}..10")
    return level


def _repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


__all__ = [
    "CharacterDetailView",
    "CharacterEditRequest",
    "CharacterListModel",
    "CharacterSummaryView",
    "CharactersPage",
    "ChoiceOptionView",
    "PermanentEchoSlotView",
    "SkillProgressTableModel",
    "SkillProgressView",
    "WeaponProgressView",
]
