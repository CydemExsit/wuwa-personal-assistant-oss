"""Traditional-Chinese view for the local daily cultivation planner.

The page is intentionally a passive Qt boundary.  It renders immutable view
models and emits immutable requests; planning, persistence, and database work
belong to an external presenter so the GUI thread never performs blocking I/O.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from functools import partial
from math import isfinite

from PySide6.QtCore import (
    QAbstractItemModel,
    QAbstractTableModel,
    QDate,
    QModelIndex,
    QPersistentModelIndex,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDateEdit,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from wuwa_assistant.application.farming import TodayFarmingSnapshot
from wuwa_assistant.ui.widgets.farming_reasons import FarmingReasonsButton

_INVALID_INDEX = QModelIndex()
type ModelIndex = QModelIndex | QPersistentModelIndex


def _require_identifier(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} cannot be empty")


def _validate_count(value: int, field_name: str, *, maximum: int = 999_999_999) -> None:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{field_name} must be an integer within 0..{maximum}")


def _validate_quantity(
    value: object,
    field_name: str,
    *,
    maximum: float = 999_999_999,
) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(float(value))
        or not 0 <= float(value) <= maximum
    ):
        raise ValueError(f"{field_name} must be a finite number within 0..{maximum}")


@dataclass(frozen=True, slots=True)
class TodayPlanRequest:
    """User-selected date and Waveplate budget for a planning run."""

    plan_date: date
    available_waveplates: int

    def __post_init__(self) -> None:
        _validate_count(self.available_waveplates, "available_waveplates", maximum=9_999)


@dataclass(frozen=True, slots=True)
class InventoryEditRequest:
    """One manual inventory count edit emitted to the presenter."""

    material_id: str
    count: float

    def __post_init__(self) -> None:
        _require_identifier(self.material_id, "material_id")
        _validate_quantity(self.count, "count")


@dataclass(frozen=True, slots=True)
class TacetStockEditRequest:
    """Availability of wanted drops, for one field or an explicit bulk edit."""

    field_ids: tuple[str, ...]
    has_stock: bool

    def __post_init__(self) -> None:
        if not self.field_ids or any(not value.strip() for value in self.field_ids):
            raise ValueError("field_ids must be non-empty")
        if type(self.has_stock) is not bool:
            raise ValueError("has_stock must be bool")


@dataclass(frozen=True, slots=True)
class PlanRecommendationView:
    """One ranked, explainable farming recommendation."""

    identifier: str
    title: str
    category: str
    suggested_runs: int | None
    waveplate_cost: int | None
    reason: str
    character_name: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.identifier, "recommendation identifier")
        _require_identifier(self.title, "recommendation title")
        _require_identifier(self.category, "recommendation category")
        _require_identifier(self.reason, "recommendation reason")
        if self.suggested_runs is not None:
            _validate_count(self.suggested_runs, "suggested_runs", maximum=999)
        if self.waveplate_cost is not None:
            _validate_count(self.waveplate_cost, "waveplate_cost", maximum=9_999)
        if self.character_name is not None and not self.character_name.strip():
            raise ValueError("character_name must be non-empty or null")


@dataclass(frozen=True, slots=True)
class WeeklyShortageView:
    """One material gap contributing to the current weekly plan."""

    material_id: str
    material_name: str
    current_count: float
    required_count: float
    character_names: tuple[str, ...] = ()
    expected_completion_date: date | None = None
    current_count_confirmed: bool = True

    def __post_init__(self) -> None:
        _require_identifier(self.material_id, "material_id")
        _require_identifier(self.material_name, "material_name")
        _validate_quantity(self.current_count, "current_count")
        _validate_quantity(self.required_count, "required_count")
        if type(self.current_count_confirmed) is not bool:
            raise ValueError("current_count_confirmed must be bool")
        if any(not name.strip() for name in self.character_names):
            raise ValueError("character_names cannot contain empty values")

    @property
    def missing_count(self) -> float | None:
        if not self.current_count_confirmed:
            return None
        return max(0, self.required_count - self.current_count)


@dataclass(frozen=True, slots=True)
class CharacterTargetSummaryView:
    """Compact target status used by the daily planner."""

    character_id: str
    character_name: str
    completion_percent: float
    target_reached: bool
    next_step: str

    def __post_init__(self) -> None:
        _require_identifier(self.character_id, "character_id")
        _require_identifier(self.character_name, "character_name")
        if not isfinite(self.completion_percent) or not 0 <= self.completion_percent <= 100:
            raise ValueError("completion_percent must be finite and within 0..100")
        _require_identifier(self.next_step, "next_step")


@dataclass(frozen=True, slots=True)
class InventoryItemView:
    """One manually maintained local material count."""

    material_id: str
    material_name: str
    category: str
    count: float
    confirmed: bool = True

    def __post_init__(self) -> None:
        _require_identifier(self.material_id, "material_id")
        _require_identifier(self.material_name, "material_name")
        _require_identifier(self.category, "category")
        _validate_quantity(self.count, "count")
        if type(self.confirmed) is not bool:
            raise ValueError("confirmed must be bool")


@dataclass(frozen=True, slots=True)
class TodayPlanSnapshot:
    """Complete presenter-owned snapshot rendered by :class:`TodayPlanPage`."""

    plan_date: date
    available_waveplates: int
    first_priority: PlanRecommendationView | None = None
    second_priority: PlanRecommendationView | None = None
    weekly_shortages: tuple[WeeklyShortageView, ...] = ()
    character_summaries: tuple[CharacterTargetSummaryView, ...] = ()
    inventory: tuple[InventoryItemView, ...] = ()
    weekly_summary: str = ""
    plan_generation_id: str | None = None
    farming: TodayFarmingSnapshot | None = None
    activity_notices: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        _validate_count(self.available_waveplates, "available_waveplates", maximum=9_999)
        if self.plan_generation_id is not None:
            _require_identifier(self.plan_generation_id, "plan_generation_id")
        if self.second_priority is not None and self.first_priority is None:
            raise ValueError("second_priority requires first_priority")
        for values, field_name in (
            (self.weekly_shortages, "weekly_shortages"),
            (self.character_summaries, "character_summaries"),
            (self.inventory, "inventory"),
        ):
            identifiers = [
                getattr(value, "material_id", getattr(value, "character_id", ""))
                for value in values
            ]
            if len(identifiers) != len(set(identifiers)):
                raise ValueError(f"{field_name} identifiers must be unique")


class WeeklyShortageTableModel(QAbstractTableModel):
    """Read-only material-gap table with stable material identifiers."""

    _HEADERS = ("素材", "目前", "目標", "缺口", "關聯角色", "預估完成日")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[WeeklyShortageView] = []

    def rowCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._HEADERS)

    def data(self, index: ModelIndex, role: int = int(Qt.ItemDataRole.DisplayRole)) -> object:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        row = self._rows[index.row()]
        if role == int(Qt.ItemDataRole.UserRole):
            return row.material_id
        if role == int(Qt.ItemDataRole.DisplayRole):
            values: tuple[object, ...] = (
                row.material_name,
                _display_quantity(row.current_count) if row.current_count_confirmed else "尚未輸入",
                _display_quantity(row.required_count),
                _display_quantity(row.missing_count)
                if row.missing_count is not None
                else "資料不足",
                "、".join(row.character_names) or "—",
                row.expected_completion_date.isoformat()
                if row.expected_completion_date is not None
                else "資料不足",
            )
            return values[index.column()]
        if role == int(Qt.ItemDataRole.EditRole):
            values = (
                row.material_name,
                row.current_count,
                row.required_count,
                row.missing_count,
                "、".join(row.character_names),
                row.expected_completion_date or date.max,
            )
            return values[index.column()]
        if role == int(Qt.ItemDataRole.TextAlignmentRole) and index.column() in {1, 2, 3}:
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

    def set_rows(self, rows: Sequence[WeeklyShortageView]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()


class CharacterTargetTableModel(QAbstractTableModel):
    """Read-only account target summary table."""

    _HEADERS = ("角色", "完成度", "狀態", "下一步")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[CharacterTargetSummaryView] = []

    def rowCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._HEADERS)

    def data(self, index: ModelIndex, role: int = int(Qt.ItemDataRole.DisplayRole)) -> object:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        row = self._rows[index.row()]
        if role == int(Qt.ItemDataRole.UserRole):
            return row.character_id
        if role == int(Qt.ItemDataRole.DisplayRole):
            values: tuple[object, ...] = (
                row.character_name,
                f"{row.completion_percent:.0f}%",
                "已達目前目標" if row.target_reached else "尚未達標",
                row.next_step,
            )
            return values[index.column()]
        if role == int(Qt.ItemDataRole.EditRole):
            values = (
                row.character_name,
                row.completion_percent,
                row.target_reached,
                row.next_step,
            )
            return values[index.column()]
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

    def set_rows(self, rows: Sequence[CharacterTargetSummaryView]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()


class InventoryTableModel(QAbstractTableModel):
    """Locally editable inventory view that emits persistence requests."""

    inventory_edit_requested = Signal(object)
    _HEADERS = ("素材", "類別", "庫存數量")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[InventoryItemView] = []
        self._pending: dict[str, float] = {}

    def rowCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._HEADERS)

    def data(self, index: ModelIndex, role: int = int(Qt.ItemDataRole.DisplayRole)) -> object:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        row = self._rows[index.row()]
        if role == int(Qt.ItemDataRole.UserRole):
            return row.material_id
        if role == int(Qt.ItemDataRole.DisplayRole):
            return (
                row.material_name,
                row.category,
                _display_quantity(row.count) if row.confirmed else "尚未輸入",
            )[index.column()]
        if role == int(Qt.ItemDataRole.EditRole):
            return (row.material_name, row.category, row.count)[index.column()]
        if role == int(Qt.ItemDataRole.TextAlignmentRole) and index.column() == 2:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == int(Qt.ItemDataRole.ToolTipRole) and index.column() == 2:
            return "雙擊可修改本機庫存數量"
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
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == 2:
            flags |= Qt.ItemFlag.ItemIsEditable
        return flags

    def setData(
        self,
        index: ModelIndex,
        value: object,
        role: int = int(Qt.ItemDataRole.EditRole),
    ) -> bool:
        if (
            role != int(Qt.ItemDataRole.EditRole)
            or not index.isValid()
            or index.column() != 2
            or not 0 <= index.row() < len(self._rows)
        ):
            return False
        try:
            count = _coerce_quantity(value)
        except (TypeError, ValueError):
            return False
        old = self._rows[index.row()]
        if count == old.count and old.confirmed:
            return False
        if self._pending.get(old.material_id) == count:
            return False
        self._pending[old.material_id] = count
        self.inventory_edit_requested.emit(InventoryEditRequest(old.material_id, count))
        return True

    def set_rows(self, rows: Sequence[InventoryItemView]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self._pending.clear()
        self.endResetModel()

    def clear_pending(self, material_id: str | None = None) -> None:
        """Allow a failed persistence request to be submitted again."""

        if material_id is None:
            self._pending.clear()
        else:
            self._pending.pop(material_id, None)


class _QuantityDelegate(QStyledItemDelegate):
    """Four-decimal editor matching the SQLite material quantity contract."""

    def createEditor(
        self,
        parent: QWidget,
        option: QStyleOptionViewItem,
        index: ModelIndex,
    ) -> QWidget:
        del option, index
        editor = QDoubleSpinBox(parent)
        editor.setRange(0, 999_999_999)
        editor.setDecimals(4)
        editor.setSingleStep(1)
        return editor

    def setEditorData(self, editor: QWidget, index: ModelIndex) -> None:
        if isinstance(editor, QDoubleSpinBox):
            value = index.data(int(Qt.ItemDataRole.EditRole))
            editor.setValue(float(value))
            return
        super().setEditorData(editor, index)

    def setModelData(
        self,
        editor: QWidget,
        model: QAbstractItemModel,
        index: ModelIndex,
    ) -> None:
        if isinstance(editor, QDoubleSpinBox):
            model.setData(index, editor.value(), int(Qt.ItemDataRole.EditRole))
            return
        super().setModelData(editor, model, index)


class _PriorityCard(QFrame):
    def __init__(self, heading: str, object_name: str) -> None:
        super().__init__()
        self.setObjectName("MetricCard")
        heading_label = QLabel(heading)
        heading_label.setObjectName("MetricTitle")
        self.title_label = QLabel("尚無建議")
        self.title_label.setObjectName(f"{object_name}Title")
        self.title_label.setWordWrap(True)
        self.meta_label = QLabel("建議刷取 0 次 · 使用 0 結晶波片")
        self.meta_label.setObjectName(f"{object_name}Runs")
        self.meta_label.setWordWrap(True)
        self.reason_label = QLabel("請先建立角色目標與素材需求。")
        self.reason_label.setObjectName(f"{object_name}Reason")
        self.reason_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(8)
        layout.addWidget(heading_label)
        layout.addWidget(self.title_label)
        layout.addWidget(self.meta_label)
        layout.addWidget(self.reason_label)
        layout.addStretch(1)

    def set_recommendation(self, recommendation: PlanRecommendationView | None) -> None:
        if recommendation is None:
            self.title_label.setText("尚無建議")
            self.meta_label.setText("建議刷取 0 次 · 使用 0 結晶波片")
            self.reason_label.setText("請先建立角色目標與素材需求。")
            return
        character = f" · {recommendation.character_name}" if recommendation.character_name else ""
        self.title_label.setText(f"{recommendation.title}{character}")
        runs = (
            f"建議刷取 {recommendation.suggested_runs} 次"
            if recommendation.suggested_runs is not None
            else "建議次數資料不足"
        )
        cost = (
            f"使用 {recommendation.waveplate_cost} 結晶波片"
            if recommendation.waveplate_cost is not None
            else "結晶波片成本資料不足"
        )
        self.meta_label.setText(f"{recommendation.category} · {runs} · {cost}")
        self.reason_label.setText(recommendation.reason)


class FarmingTableModel(QAbstractTableModel):
    """Compact read-only rows with full explanations in tooltips."""

    def __init__(self, headers: tuple[str, ...], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._headers = headers
        self._rows: tuple[tuple[str, ...], ...] = ()
        self._details: tuple[str, ...] = ()

    def set_rows(self, rows: tuple[tuple[str, ...], ...], details: tuple[str, ...] = ()) -> None:
        self.beginResetModel()
        self._rows = rows
        self._details = details
        self.endResetModel()

    def rowCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._headers)

    def data(self, index: ModelIndex, role: int = int(Qt.ItemDataRole.DisplayRole)) -> object:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        if role == int(Qt.ItemDataRole.DisplayRole):
            return self._rows[index.row()][index.column()]
        if role == int(Qt.ItemDataRole.ToolTipRole) and index.row() < len(self._details):
            return self._details[index.row()]
        return None

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = int(Qt.ItemDataRole.DisplayRole),
    ) -> object:
        if (
            orientation == Qt.Orientation.Horizontal
            and role == int(Qt.ItemDataRole.DisplayRole)
            and 0 <= section < len(self._headers)
        ):
            return self._headers[section]
        return None


class TodayPlanPage(QWidget):
    """Resizable daily planner page with a signal-only application boundary."""

    plan_requested = Signal(object)
    inventory_edit_requested = Signal(object)
    tacet_stock_edit_requested = Signal(object)
    manage_data_requested = Signal()
    complete_plan_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("TodayPlanPage")
        self._loading = False
        self._plan_generation_id: str | None = None
        self._field_ids: tuple[str, ...] = ()

        title = QLabel("養成規劃")
        title.setObjectName("PageTitle")
        subtitle = QLabel("技能看還差哪些等級; 聲骸看哪個無音區能同時改善兩種套裝。")
        subtitle.setObjectName("PageDescription")
        subtitle.setWordWrap(True)

        self.plan_date_edit = QDateEdit(QDate.currentDate())
        self.plan_date_edit.setObjectName("TodayPlanDateEdit")
        self.plan_date_edit.setCalendarPopup(True)
        self.plan_date_edit.setDisplayFormat("yyyy-MM-dd")
        self.waveplates_spin = QSpinBox()
        self.waveplates_spin.setObjectName("TodayPlanWaveplatesSpin")
        self.waveplates_spin.setRange(0, 9_999)
        self.waveplates_spin.setValue(240)
        self.waveplates_spin.setSuffix(" 點")
        self.plan_button = QPushButton("重新規劃")
        self.plan_button.setObjectName("TodayPlanGenerateButton")
        self.plan_button.clicked.connect(self._request_plan)
        self.manage_data_button = QPushButton("管理素材與目標")
        self.manage_data_button.setObjectName("TodayPlanManageDataButton")
        self.manage_data_button.clicked.connect(self.manage_data_requested)
        self.complete_plan_button = QPushButton("標記養成規劃已執行")
        self.complete_plan_button.setObjectName("TodayPlanCompleteButton")
        self.complete_plan_button.clicked.connect(self._request_complete_plan)
        self.complete_plan_button.setEnabled(False)
        self.status_label = QLabel("尚未產生養成規劃。")
        self.status_label.setObjectName("TodayPlanStatus")
        self.status_label.setWordWrap(True)

        controls = QHBoxLayout()
        self.plan_date_edit.setParent(self)
        self.plan_date_edit.hide()
        controls.addWidget(self.plan_button)
        controls.addWidget(self.status_label, 1)
        # Keep the old persistence boundary available for existing saved plans,
        # without asking users to maintain a material inventory in this page.
        for control in (self.waveplates_spin, self.manage_data_button, self.complete_plan_button):
            control.setParent(self)
            control.hide()

        self.first_priority_card = _PriorityCard("第一順位", "TodayPlanFirstPriority")
        self.first_priority_card.setProperty("priority", "first")
        self.second_priority_card = _PriorityCard("第二順位", "TodayPlanSecondPriority")
        self.second_priority_card.setProperty("priority", "second")
        priority_grid = QGridLayout()
        priority_grid.setSpacing(16)
        priority_grid.addWidget(self.first_priority_card, 0, 0)
        priority_grid.addWidget(self.second_priority_card, 0, 1)
        priority_grid.setColumnStretch(0, 3)
        priority_grid.setColumnStretch(1, 2)

        self.empty_state_label = QLabel("目前沒有可規劃的養成項目。請先建立角色與養成目標。")
        self.empty_state_label.setObjectName("TodayPlanEmptyState")
        self.empty_state_label.setWordWrap(True)
        self.empty_state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.weekly_summary_label = QLabel("本週尚無素材缺口。")
        self.weekly_summary_label.setObjectName("TodayPlanWeeklySummary")
        self.weekly_summary_label.setWordWrap(True)
        self.shortage_model = WeeklyShortageTableModel(self)
        self.shortage_table = _table("TodayPlanShortageTable", self.shortage_model)
        weekly_group = QGroupBox("本週素材缺口與完成日")
        weekly_layout = QVBoxLayout(weekly_group)
        weekly_layout.addWidget(self.weekly_summary_label)
        weekly_layout.addWidget(self.shortage_table)

        self.character_model = CharacterTargetTableModel(self)
        self.character_table = _table("TodayPlanCharacterTable", self.character_model)
        characters_group = QGroupBox("角色目標摘要")
        characters_layout = QVBoxLayout(characters_group)
        characters_layout.addWidget(self.character_table)

        self.inventory_model = InventoryTableModel(self)
        self.inventory_model.inventory_edit_requested.connect(self._forward_inventory_edit)
        self.inventory_table = _table(
            "TodayPlanInventoryTable", self.inventory_model, editable=True
        )
        self.inventory_quantity_delegate = _QuantityDelegate(self.inventory_table)
        self.inventory_table.setItemDelegateForColumn(2, self.inventory_quantity_delegate)
        inventory_help = QLabel("雙擊「庫存數量」即可修改;變更只會傳給本機資料層。")
        inventory_help.setObjectName("TodayPlanInventoryHelp")
        inventory_help.setWordWrap(True)
        inventory_group = QGroupBox("素材庫存")
        inventory_layout = QVBoxLayout(inventory_group)
        inventory_layout.addWidget(inventory_help)
        inventory_layout.addWidget(self.inventory_table)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 12, 0)
        content_layout.setSpacing(16)
        self.legacy_content = QWidget(self)
        legacy_layout = QVBoxLayout(self.legacy_content)
        legacy_layout.addLayout(priority_grid)
        legacy_layout.addWidget(weekly_group)
        legacy_layout.addWidget(characters_group)
        legacy_layout.addWidget(inventory_group)
        self.legacy_content.hide()

        self.activity_label = QLabel("加倍活動尚未查核; 技能素材與無音區兩種方向均保留。")
        self.activity_label.setObjectName("TodayPlanActivityNotice")
        self.activity_label.setWordWrap(True)
        self.skill_group = QGroupBox("技能待升級")
        self.skill_summary_label = QLabel("請先建立角色, 並在角色頁記錄目前與目標技能等級。")
        self.skill_summary_label.setWordWrap(True)
        self.skill_model = FarmingTableModel(("角色", "技能", "目前 → 目標"), self)
        self.skill_table = _table("TodayPlanSkillTable", self.skill_model)
        skill_layout = QVBoxLayout(self.skill_group)
        skill_layout.addWidget(self.skill_summary_label)
        skill_layout.addWidget(self.skill_table, 1)
        self.zone_group = QGroupBox("無音區推薦")
        self.zone_summary_label = QLabel("正在等待角色配裝資料。")
        self.zone_summary_label.setWordWrap(True)
        self.zone_model = FarmingTableModel(("無音區", "套裝", "受益", "庫存"), self)
        self.zone_table = _table("TodayPlanZoneTable", self.zone_model)
        self.stock_hint = QLabel(
            "只要需要的其中一種已用完, 就選「已用完」; "
            "需要的都還有得練, 才選「還有可培養」。點狀態即可切換並儲存。"
        )
        self.stock_hint.setWordWrap(True)
        self.stock_all_available = QPushButton("全部還有可培養")
        self.stock_all_available.setObjectName("TodayPlanStockAllAvailable")
        self.stock_all_depleted = QPushButton("全部已用完")
        self.stock_all_depleted.setObjectName("TodayPlanStockAllDepleted")
        self.stock_all_available.clicked.connect(lambda: self._edit_all_stock(True))
        self.stock_all_depleted.clicked.connect(lambda: self._edit_all_stock(False))
        stock_actions = QHBoxLayout()
        stock_actions.addWidget(self.stock_all_available)
        stock_actions.addWidget(self.stock_all_depleted)
        stock_actions.addStretch()
        self.zone_details = FarmingReasonsButton("查看推薦理由與資料待確認項目", self.zone_group)
        self.catalog_label = QLabel("")
        self.catalog_label.setWordWrap(True)
        zone_layout = QVBoxLayout(self.zone_group)
        zone_layout.addWidget(self.zone_summary_label)
        zone_layout.addWidget(self.stock_hint)
        zone_layout.addLayout(stock_actions)
        zone_layout.addWidget(self.zone_table, 1)
        zone_layout.addWidget(self.zone_details)
        zone_layout.addWidget(self.catalog_label)
        for table in (self.skill_table, self.zone_table):
            table.horizontalHeader().setStretchLastSection(False)
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            table.horizontalHeader().sectionResized.connect(
                table.resizeRowsToContents, Qt.ConnectionType.QueuedConnection
            )
            table.verticalHeader().hide()
            table.setWordWrap(True)
            table.setMinimumHeight(280)
        farming_grid = QGridLayout()
        farming_grid.addWidget(self.skill_group, 0, 0)
        farming_grid.addWidget(self.zone_group, 0, 1)
        farming_grid.setColumnStretch(0, 1)
        farming_grid.setColumnStretch(1, 1)
        content_layout.addWidget(self.activity_label)
        content_layout.addLayout(farming_grid, 1)
        content_layout.addWidget(self.empty_state_label)

        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("TodayPlanScrollArea")
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setWidget(content)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 30, 40, 24)
        layout.setSpacing(12)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addLayout(controls)
        layout.addWidget(self.scroll_area, 1)

    @Slot(object)
    def set_snapshot(self, snapshot: object) -> None:
        if not isinstance(snapshot, TodayPlanSnapshot):
            raise TypeError("snapshot must be TodayPlanSnapshot")
        self.plan_date_edit.setDate(_to_qdate(snapshot.plan_date))
        self.waveplates_spin.setValue(snapshot.available_waveplates)
        self.first_priority_card.set_recommendation(snapshot.first_priority)
        self.second_priority_card.set_recommendation(snapshot.second_priority)
        self.shortage_model.set_rows(snapshot.weekly_shortages)
        self.character_model.set_rows(snapshot.character_summaries)
        self.inventory_model.set_rows(snapshot.inventory)
        self._plan_generation_id = snapshot.plan_generation_id
        self.weekly_summary_label.setText(snapshot.weekly_summary or "本週尚無素材缺口。")
        if snapshot.farming is not None:
            self.set_farming(snapshot.farming)
        if snapshot.activity_notices is not None:
            self.set_activity_notices(snapshot.activity_notices)

        has_plan_data = bool(
            snapshot.first_priority
            or snapshot.weekly_shortages
            or snapshot.character_summaries
            or snapshot.inventory
            or (snapshot.farming is not None and snapshot.farming.character_count)
        )
        self.empty_state_label.setVisible(not has_plan_data)
        self.status_label.setProperty("error", False)
        if has_plan_data:
            state = "已儲存" if snapshot.plan_generation_id is not None else "預覽 (尚未儲存)"
            self.status_label.setText(
                f"已載入 {snapshot.plan_date.isoformat()} 的本機規劃 · {state}。"
                if snapshot.farming is None or snapshot.plan_generation_id is not None
                else "已更新技能與無音區建議。"
            )
        else:
            self.status_label.setText("目前沒有可規劃的養成項目。")
        self._set_controls_enabled(True)
        self._loading = False
        _repolish(self.status_label)

    def set_activity_notices(self, notices: tuple[str, ...]) -> None:
        self.activity_label.setText(
            "\n".join(notices)
            if notices
            else "目前沒有已查核的加倍活動; 即使沒有技能缺口, 仍可依活動獎勵選擇刷取。"
        )

    def set_farming(self, snapshot: TodayFarmingSnapshot) -> None:
        self.skill_model.set_rows(
            tuple(
                (
                    row.character_name,
                    row.skill_name,
                    f"{row.current_level} → {row.target_level}",
                )
                for row in snapshot.skills
            )
        )
        self.skill_summary_label.setText(
            f"{len(snapshot.skills)} 項技能尚未達到你設定的目標; 實際素材請依遊戲指引。"
            if snapshot.skills
            else "目前記錄的技能已達目標; 技能素材加倍時仍可預先儲備。"
            if snapshot.character_count
            else "請先建立角色, 並在角色頁記錄目前與目標技能等級。"
        )
        recommendations = snapshot.farming.recommendations
        positive = tuple(row for row in recommendations if row.points > 0)
        needing_farm = tuple(row for row in positive if not row.has_unleveled_stock)
        shown = recommendations
        self._field_ids = tuple(row.zone.identifier for row in shown)
        details = tuple(
            f"{row.zone.region} · {row.zone.name}\n"
            f"掉落: {'、'.join(row.zone.sets)}\n"
            f"受益 {row.character_count} 位角色 / {row.echo_count} 件聲骸\n"
            + (
                "庫存: 還有可培養, 先練現有庫存, 刷取順位後移。\n"
                if row.has_unleveled_stock
                else "庫存: 需要的至少一種已用完, 保留完整刷取需求。\n"
            )
            + f"養成排序點 {row.points:.3f} = 加權需求 {row.weighted_base_points:.3f}"
            f" x 角色覆蓋 {row.coverage_multiplier:.3f}"
            f" x 雙套裝利用 {row.dual_set_multiplier:.3f}\n"
            + (
                "\n".join(
                    f"{benefit.character_name}/{benefit.set_name}: {benefit.reason}\n"
                    f"  {benefit.importance_reason} · 重要性 {benefit.importance_multiplier:.2f} 倍"
                    f" · 同角色件數遞減 {benefit.piece_multiplier:.2f} 倍"
                    f" · 貢獻 {benefit.points:.3f} 點"
                    for benefit in row.benefits
                )
                if row.benefits
                else "目前沒有可確認的替換缺口; 加倍時可依想儲備的套裝選擇。"
            )
            for row in shown
        )
        self.zone_model.set_rows(
            tuple(
                (
                    f"{index + 1}. {row.zone.name}",
                    "\n".join(row.zone.sets),
                    (
                        f"{row.character_count} 人 / {row.echo_count} 件\n"
                        + "、".join(dict.fromkeys(b.character_name for b in row.benefits))
                        if row.benefits
                        else "儲備備選"
                    ),
                    "還有可培養" if row.has_unleveled_stock else "需要的已用完",
                )
                for index, row in enumerate(shown)
            ),
            details,
        )
        for index, row in enumerate(shown):
            button = QPushButton("還有\n可培養" if row.has_unleveled_stock else "需要的\n已用完")
            button.setObjectName(f"TodayPlanStock_{row.zone.identifier}")
            button.setAccessibleName(
                f"{row.zone.name}: "
                f"{'還有可培養' if row.has_unleveled_stock else '需要的已用完'}, 點此切換"
            )
            button.setToolTip(self.stock_hint.text())
            button.setStyleSheet("QPushButton { padding: 4px; }")
            button.setMinimumHeight(48)
            button.clicked.connect(
                partial(self._edit_stock, row.zone.identifier, not row.has_unleveled_stock)
            )
            self.zone_table.setIndexWidget(self.zone_model.index(index, 3), button)
        self.zone_summary_label.setText(
            f"優先 {needing_farm[0].zone.name} · "
            f"{needing_farm[0].character_count} 位角色 / {needing_farm[0].echo_count} 件受益 · "
            + (
                "兩種套裝皆可受益。"
                if len(needing_farm[0].useful_sets) > 1
                else "有一種套裝可受益。"
            )
            if needing_farm
            else "有改善需求的無音區都還有可培養庫存, 先練現有聲骸; 需要的用完後點狀態改回來。"
            if positive
            else "目前沒有已確認的無音區替換缺口; 加倍時可從下列套裝儲備備選選擇。"
        )
        rules = (
            "排序為可解釋的養成優先度, 不代表實際傷害或掉落機率。\n"
            "庫存只影響刷取順位, 不改品質分數; "
            "需要的任一種類已用完即保留整區需求, 其他庫存不抵銷。\n"
            "使用各件現有分數、角色優先級 (越小越優先)、最優先隊伍、受益角色/件數與雙套裝利用。\n"
            "同角色多件需求遞減; 已達標的微幅改善只計少量覆蓋, 替代套裝與跨隊角色不重複計件。\n"
            "主輸出持續提升並遞減收益; 副輸出達標後降權; 治療以套裝、主詞條及必要效率為先。\n"
            "未知分數不視為零分; 未開滿先看強化潛力; COST 4 另找實際取得來源。"
        )
        self.zone_details.set_details(
            "無音區推薦理由",
            "\n\n".join((rules, *details, *snapshot.farming.notices)),
        )
        self.zone_details.set_recommendations(recommendations)
        self.catalog_label.setText(snapshot.catalog_note)
        self.skill_table.resizeRowsToContents()
        self.zone_table.resizeRowsToContents()
        for index in range(len(shown)):
            self.zone_table.setRowHeight(index, max(52, self.zone_table.rowHeight(index)))

    def _edit_all_stock(self, has_stock: bool) -> None:
        if self._field_ids and not self._loading:
            self.tacet_stock_edit_requested.emit(TacetStockEditRequest(self._field_ids, has_stock))

    def _edit_stock(self, field_id: str, has_stock: bool, _checked: bool = False) -> None:
        if not self._loading:
            self.tacet_stock_edit_requested.emit(TacetStockEditRequest((field_id,), has_stock))

    @Slot(bool, str)
    def set_loading(self, loading: bool, message: str = "正在計算養成建議…") -> None:
        self._loading = loading
        self._set_controls_enabled(not loading)
        if loading:
            self.status_label.setProperty("error", False)
            self.status_label.setText(message)
            _repolish(self.status_label)

    @Slot(str)
    def show_error(self, message: str) -> None:
        self._loading = False
        self._set_controls_enabled(True)
        self.status_label.setProperty("error", True)
        self.status_label.setText(f"無法產生養成規劃: {message}")
        _repolish(self.status_label)

    @Slot(str)
    def show_status(self, message: str) -> None:
        self.status_label.setProperty("error", False)
        self.status_label.setText(message)
        _repolish(self.status_label)

    def current_request(self) -> TodayPlanRequest:
        # Retain the date only for the legacy resource-planning API. The active
        # farming presenter always uses its current clock and exposes no date UI.
        value = self.plan_date_edit.date()
        return TodayPlanRequest(
            date(value.year(), value.month(), value.day()), self.waveplates_spin.value()
        )

    def _request_plan(self) -> None:
        if self._loading:
            return
        request = self.current_request()
        self.set_loading(True)
        self.plan_requested.emit(request)

    @Slot()
    def _request_complete_plan(self) -> None:
        if self._loading or self._plan_generation_id is None:
            return
        self.set_loading(True, "正在標記養成規劃已執行…")
        self.complete_plan_requested.emit(self._plan_generation_id)

    @Slot(object)
    def _forward_inventory_edit(self, request: object) -> None:
        if not isinstance(request, InventoryEditRequest):
            raise TypeError("request must be InventoryEditRequest")
        self.status_label.setProperty("error", False)
        self.status_label.setText("正在保存本機素材庫存…")
        _repolish(self.status_label)
        self.inventory_edit_requested.emit(request)

    def _set_controls_enabled(self, enabled: bool) -> None:
        self.plan_date_edit.setEnabled(enabled)
        self.waveplates_spin.setEnabled(enabled)
        self.plan_button.setEnabled(enabled)
        self.manage_data_button.setEnabled(enabled)
        self.complete_plan_button.setEnabled(enabled and self._plan_generation_id is not None)
        self.inventory_table.setEnabled(enabled)
        self.zone_table.setEnabled(enabled)
        self.stock_all_available.setEnabled(enabled and bool(self._field_ids))
        self.stock_all_depleted.setEnabled(enabled and bool(self._field_ids))


def _table(object_name: str, model: QAbstractTableModel, *, editable: bool = False) -> QTableView:
    table = QTableView()
    table.setObjectName(object_name)
    table.setModel(model)
    table.setAlternatingRowColors(True)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    if editable:
        table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
    else:
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    table.setMinimumHeight(150)
    return table


def _coerce_quantity(value: object) -> float:
    if type(value) is bool:
        raise TypeError("boolean is not a quantity")
    if not isinstance(value, (int, float, str)):
        raise TypeError("quantity must be numeric")
    try:
        quantity = float(value.strip()) if isinstance(value, str) else float(value)
    except ValueError as exc:
        raise ValueError("quantity must be numeric") from exc
    _validate_quantity(quantity, "quantity")
    return round(quantity, 4)


def _display_quantity(value: float) -> int | float:
    normalized = round(float(value), 4)
    return int(normalized) if normalized.is_integer() else normalized


def _to_qdate(value: date) -> QDate:
    return QDate(value.year, value.month, value.day)


def _repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


__all__ = [
    "CharacterTargetSummaryView",
    "CharacterTargetTableModel",
    "InventoryEditRequest",
    "InventoryItemView",
    "InventoryTableModel",
    "PlanRecommendationView",
    "TacetStockEditRequest",
    "TodayPlanPage",
    "TodayPlanRequest",
    "TodayPlanSnapshot",
    "WeeklyShortageTableModel",
    "WeeklyShortageView",
]
