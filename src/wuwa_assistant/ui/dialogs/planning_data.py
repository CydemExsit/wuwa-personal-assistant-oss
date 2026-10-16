"""Modeless material and cultivation-target editor for the local planner.

The dialog is a passive Qt view.  It accepts immutable presenter snapshots and
emits immutable requests; it does not import a repository, ORM, or database.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date
from math import isfinite

from PySide6.QtCore import (
    QAbstractTableModel,
    QDate,
    QModelIndex,
    QPersistentModelIndex,
    QSignalBlocker,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDateEdit,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableView,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from wuwa_assistant.ui.widgets.choice_selector import ChoiceSelector as QComboBox

_INVALID_INDEX = QModelIndex()
type ModelIndex = QModelIndex | QPersistentModelIndex

_WEEKDAY_LABELS = {
    1: "一",
    2: "二",
    3: "三",
    4: "四",
    5: "五",
    6: "六",
    7: "日",
}
_SOURCE_STATUS_OPTIONS = (
    ("manual", "手動資料"),
    ("manual_unverified", "手動資料 / 未驗證"),
    ("manual_package", "本機匯入包"),
    ("user_observed", "使用者紀錄"),
    ("community_unverified", "社群資料 / 未驗證"),
    ("official", "官方資料"),
    ("verified", "已驗證資料"),
    ("unknown", "來源未知"),
)
_TARGET_TYPE_OPTIONS = (
    ("character_level", "角色等級"),
    ("weapon_level", "武器等級"),
    ("skill_level", "技能等級"),
    ("attribute_nodes", "小型屬性節點"),
    ("echo_slot", "永久聲骸位置"),
    ("custom", "自訂"),
)


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} cannot be empty")


def _validate_nonnegative(value: int, field_name: str, *, maximum: int = 999_999_999) -> None:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{field_name} must be an integer within 0..{maximum}")


def _validate_nonnegative_quantity(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite nonnegative number")
    if not isfinite(float(value)) or not 0 <= float(value) <= 999_999_999:
        raise ValueError(f"{field_name} must be a finite nonnegative number")


def _validate_weekdays(values: tuple[int, ...]) -> None:
    if any(type(day) is not int or day not in _WEEKDAY_LABELS for day in values):
        raise ValueError("open_weekdays must contain ISO weekdays within 1..7")
    if len(values) != len(set(values)) or tuple(sorted(values)) != values:
        raise ValueError("open_weekdays must be sorted and unique")


@dataclass(frozen=True, slots=True)
class PlanningCharacterChoiceView:
    identifier: str
    name: str

    def __post_init__(self) -> None:
        _require_text(self.identifier, "character identifier")
        _require_text(self.name, "character name")


@dataclass(frozen=True, slots=True)
class MaterialView:
    identifier: str
    code: str
    name: str
    category: str
    waveplate_cost_per_run: int
    average_output: float
    open_weekdays: tuple[int, ...]
    weekly_limit: int | None
    source_status: str
    version: str | None
    inventory_count: float | None = None
    notes: str = ""
    archived: bool = False

    def __post_init__(self) -> None:
        _require_text(self.identifier, "material identifier")
        _validate_material_fields(
            self.code,
            self.name,
            self.category,
            self.waveplate_cost_per_run,
            self.average_output,
            self.open_weekdays,
            self.weekly_limit,
            self.source_status,
            self.version,
            self.inventory_count,
        )


@dataclass(frozen=True, slots=True)
class MaterialSaveRequest:
    identifier: str | None
    code: str
    name: str
    category: str
    waveplate_cost_per_run: int
    average_output: float
    open_weekdays: tuple[int, ...]
    weekly_limit: int | None
    source_status: str
    version: str | None
    inventory_count: float | None
    notes: str = ""

    def __post_init__(self) -> None:
        if self.identifier is not None:
            _require_text(self.identifier, "material identifier")
        _validate_material_fields(
            self.code,
            self.name,
            self.category,
            self.waveplate_cost_per_run,
            self.average_output,
            self.open_weekdays,
            self.weekly_limit,
            self.source_status,
            self.version,
            self.inventory_count,
        )


@dataclass(frozen=True, slots=True)
class RequiredMaterialView:
    material_id: str
    material_name: str
    quantity: float

    def __post_init__(self) -> None:
        _require_text(self.material_id, "required material identifier")
        _require_text(self.material_name, "required material name")
        _validate_nonnegative_quantity(self.quantity, "required material quantity")


@dataclass(frozen=True, slots=True)
class RequiredMaterialRequest:
    material_id: str
    quantity: float

    def __post_init__(self) -> None:
        _require_text(self.material_id, "required material identifier")
        _validate_nonnegative_quantity(self.quantity, "required material quantity")


@dataclass(frozen=True, slots=True)
class CultivationTargetView:
    identifier: str
    character_id: str | None
    character_name: str | None
    target_type: str
    target_key: str | None
    current_value: str
    target_value: str
    priority: int
    completed: bool
    due_date: date | None
    notes: str = ""
    required_materials: tuple[RequiredMaterialView, ...] = ()
    archived: bool = False

    def __post_init__(self) -> None:
        _require_text(self.identifier, "target identifier")
        _validate_target_fields(
            self.character_id,
            self.character_name,
            self.target_type,
            self.target_key,
            self.target_value,
            self.priority,
            self.required_materials,
        )


@dataclass(frozen=True, slots=True)
class CultivationTargetSaveRequest:
    identifier: str | None
    character_id: str | None
    target_type: str
    target_key: str | None
    current_value: str
    target_value: str
    priority: int
    completed: bool
    due_date: date | None
    notes: str = ""
    required_materials: tuple[RequiredMaterialRequest, ...] = ()

    def __post_init__(self) -> None:
        if self.identifier is not None:
            _require_text(self.identifier, "target identifier")
        required_views = tuple(
            RequiredMaterialView(row.material_id, row.material_id, row.quantity)
            for row in self.required_materials
        )
        _validate_target_fields(
            self.character_id,
            None,
            self.target_type,
            self.target_key,
            self.target_value,
            self.priority,
            required_views,
        )


@dataclass(frozen=True, slots=True)
class PlanningDataBundle:
    materials: tuple[MaterialView, ...] = ()
    characters: tuple[PlanningCharacterChoiceView, ...] = ()
    targets: tuple[CultivationTargetView, ...] = ()

    def __post_init__(self) -> None:
        _require_unique((row.identifier for row in self.materials), "material identifiers")
        _require_unique((row.code for row in self.materials), "material codes")
        _require_unique((row.identifier for row in self.characters), "character identifiers")
        _require_unique((row.identifier for row in self.targets), "target identifiers")


def _validate_material_fields(
    code: str,
    name: str,
    category: str,
    waveplate_cost_per_run: int,
    average_output: float,
    open_weekdays: tuple[int, ...],
    weekly_limit: int | None,
    source_status: str,
    version: str | None,
    inventory_count: float | None,
) -> None:
    _require_text(code, "material code")
    _require_text(name, "material name")
    _require_text(category, "material category")
    _validate_nonnegative(waveplate_cost_per_run, "waveplate_cost_per_run", maximum=9_999)
    if not isfinite(average_output) or average_output < 0:
        raise ValueError("average_output must be finite and nonnegative")
    _validate_weekdays(open_weekdays)
    if weekly_limit is not None and (type(weekly_limit) is not int or weekly_limit < 0):
        raise ValueError("weekly_limit must be a nonnegative integer or null")
    _require_text(source_status, "source_status")
    if version is not None:
        _require_text(version, "version")
        if len(version) > 80:
            raise ValueError("version must contain at most 80 characters")
    if inventory_count is not None:
        _validate_nonnegative_quantity(inventory_count, "inventory_count")


def _validate_target_fields(
    character_id: str | None,
    character_name: str | None,
    target_type: str,
    target_key: str | None,
    target_value: str,
    priority: int,
    required_materials: tuple[RequiredMaterialView, ...],
) -> None:
    if character_id is not None:
        _require_text(character_id, "character identifier")
    elif character_name is not None:
        raise ValueError("character_name requires character_id")
    _require_text(target_type, "target_type")
    if target_key is not None:
        _require_text(target_key, "target_key")
    _require_text(target_value, "target_value")
    _validate_nonnegative(priority, "priority", maximum=9_999)
    _require_unique((row.material_id for row in required_materials), "required materials")


def _require_unique(values: Iterable[str], field_name: str) -> None:
    items = tuple(values)
    if len(items) != len(set(items)):
        raise ValueError(f"{field_name} must be unique")


class MaterialTableModel(QAbstractTableModel):
    _HEADERS = ("代碼", "素材", "類別", "庫存", "開放日", "來源", "版本", "狀態")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[MaterialView] = []

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
            values: tuple[object, ...] = (
                row.code,
                row.name,
                row.category,
                row.inventory_count if row.inventory_count is not None else "尚未輸入",
                _format_weekdays(row.open_weekdays),
                _source_label(row.source_status),
                row.version or "未設定",
                "已封存" if row.archived else "使用中",
            )
            return values[index.column()]
        if role == int(Qt.ItemDataRole.EditRole):
            values = (
                row.code,
                row.name,
                row.category,
                row.inventory_count,
                row.open_weekdays,
                row.source_status,
                row.version,
                row.archived,
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

    def set_rows(self, rows: Sequence[MaterialView]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def identifier_at(self, row: int) -> str | None:
        return self._rows[row].identifier if 0 <= row < len(self._rows) else None

    def row_for(self, identifier: str) -> int | None:
        return next(
            (index for index, row in enumerate(self._rows) if row.identifier == identifier), None
        )


class CultivationTargetTableModel(QAbstractTableModel):
    _HEADERS = ("角色", "目標", "目前 / 目標", "優先級", "到期日", "狀態")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[CultivationTargetView] = []

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
        status = "已封存" if row.archived else "已完成" if row.completed else "進行中"
        if role == int(Qt.ItemDataRole.DisplayRole):
            values: tuple[object, ...] = (
                row.character_name or "全帳號 / 未指定",
                f"{row.target_type}: {row.target_key or '—'}",
                f"{row.current_value or '—'} / {row.target_value}",
                row.priority,
                row.due_date.isoformat() if row.due_date is not None else "未設定",
                status,
            )
            return values[index.column()]
        if role == int(Qt.ItemDataRole.EditRole):
            values = (
                row.character_name or "",
                (row.target_type, row.target_key),
                (row.current_value, row.target_value),
                row.priority,
                row.due_date or date.max,
                (row.archived, row.completed),
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

    def set_rows(self, rows: Sequence[CultivationTargetView]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def identifier_at(self, row: int) -> str | None:
        return self._rows[row].identifier if 0 <= row < len(self._rows) else None

    def row_for(self, identifier: str) -> int | None:
        return next(
            (index for index, row in enumerate(self._rows) if row.identifier == identifier), None
        )


class RequiredMaterialTableModel(QAbstractTableModel):
    _HEADERS = ("素材", "所需數量")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[RequiredMaterialView] = []

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
        if role in (int(Qt.ItemDataRole.DisplayRole), int(Qt.ItemDataRole.EditRole)):
            return (row.material_name, row.quantity)[index.column()]
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
        return flags | Qt.ItemFlag.ItemIsEditable if index.column() == 1 else flags

    def setData(
        self,
        index: ModelIndex,
        value: object,
        role: int = int(Qt.ItemDataRole.EditRole),
    ) -> bool:
        if (
            role != int(Qt.ItemDataRole.EditRole)
            or not index.isValid()
            or index.column() != 1
            or not 0 <= index.row() < len(self._rows)
        ):
            return False
        try:
            quantity = _stored_quantity(value)
        except (TypeError, ValueError):
            return False
        old = self._rows[index.row()]
        if old.quantity == quantity:
            return False
        self._rows[index.row()] = replace(old, quantity=quantity)
        self.dataChanged.emit(index, index, [role, int(Qt.ItemDataRole.DisplayRole)])
        return True

    def set_rows(self, rows: Sequence[RequiredMaterialView]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def add_row(self, row: RequiredMaterialView) -> bool:
        existing = next(
            (index for index, item in enumerate(self._rows) if item.material_id == row.material_id),
            None,
        )
        if existing is not None:
            index = self.index(existing, 1)
            return self.setData(index, row.quantity)
        position = len(self._rows)
        self.beginInsertRows(_INVALID_INDEX, position, position)
        self._rows.append(row)
        self.endInsertRows()
        return True

    def remove_row(self, row: int) -> bool:
        if not 0 <= row < len(self._rows):
            return False
        self.beginRemoveRows(_INVALID_INDEX, row, row)
        self._rows.pop(row)
        self.endRemoveRows()
        return True

    def rows(self) -> tuple[RequiredMaterialView, ...]:
        return tuple(self._rows)


class PlanningDataDialog(QDialog):
    """Modeless editor for versioned material facts and cultivation targets."""

    material_selected = Signal(str)
    material_save_requested = Signal(object)
    material_archive_requested = Signal(str)
    target_selected = Signal(str)
    target_save_requested = Signal(object)
    target_archive_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("PlanningDataDialog")
        self.setWindowTitle("素材與養成目標管理")
        self.setModal(False)
        self.resize(1180, 780)
        self._bundle = PlanningDataBundle()
        self._material_id: str | None = None
        self._target_id: str | None = None
        self._loading = False

        self.tabs = QTabWidget()
        self.tabs.setObjectName("PlanningDataTabs")
        self.tabs.addTab(self._build_material_tab(), "素材與庫存")
        self.tabs.addTab(self._build_target_tab(), "養成目標")

        self.status_label = QLabel("資料只會保存在本機; 請先載入規劃資料。")
        self.status_label.setObjectName("PlanningDataStatus")
        self.status_label.setWordWrap(True)
        self.close_button = QPushButton("關閉")
        self.close_button.setObjectName("PlanningDataCloseButton")
        self.close_button.clicked.connect(self.close)
        footer = QHBoxLayout()
        footer.addWidget(self.status_label, 1)
        footer.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs, 1)
        layout.addLayout(footer)
        self.clear_material_form()
        self.clear_target_form()

    def _build_material_tab(self) -> QWidget:
        self.material_model = MaterialTableModel(self)
        self.material_table = _table("PlanningMaterialTable", self.material_model)
        material_selection = self.material_table.selectionModel()
        if material_selection is not None:
            material_selection.currentRowChanged.connect(self._on_material_selected)
        self.material_new_button = QPushButton("新增素材")
        self.material_new_button.setObjectName("PlanningMaterialNewButton")
        self.material_new_button.clicked.connect(self.clear_material_form)

        list_group = QGroupBox("素材資料")
        list_layout = QVBoxLayout(list_group)
        list_layout.addWidget(self.material_table, 1)
        list_layout.addWidget(self.material_new_button)

        self.material_code_edit = QLineEdit()
        self.material_code_edit.setObjectName("PlanningMaterialCodeEdit")
        self.material_name_edit = QLineEdit()
        self.material_name_edit.setObjectName("PlanningMaterialNameEdit")
        self.material_category_edit = QLineEdit()
        self.material_category_edit.setObjectName("PlanningMaterialCategoryEdit")
        self.material_waveplate_spin = QSpinBox()
        self.material_waveplate_spin.setObjectName("PlanningMaterialWaveplateSpin")
        self.material_waveplate_spin.setRange(0, 9_999)
        self.material_average_spin = QDoubleSpinBox()
        self.material_average_spin.setObjectName("PlanningMaterialAverageOutputSpin")
        self.material_average_spin.setRange(0, 999_999_999)
        # Storage normalizes material quantities to four decimal places.  Keep
        # the editor at the same precision so a load/edit/save round-trip does
        # not silently rewrite a valid persisted value.
        self.material_average_spin.setDecimals(4)
        self.material_average_spin.setSingleStep(0.1)
        self.material_weekday_checks = tuple(self._weekday_check(day) for day in range(1, 8))
        weekdays_widget = QWidget()
        weekdays_layout = QHBoxLayout(weekdays_widget)
        weekdays_layout.setContentsMargins(0, 0, 0, 0)
        for check in self.material_weekday_checks:
            weekdays_layout.addWidget(check)
        weekdays_layout.addStretch(1)
        self.material_weekly_limit_spin = QSpinBox()
        self.material_weekly_limit_spin.setObjectName("PlanningMaterialWeeklyLimitSpin")
        self.material_weekly_limit_spin.setRange(0, 999_999_999)
        self.material_weekly_limit_enabled_check = QCheckBox("啟用每週上限")
        self.material_weekly_limit_enabled_check.setObjectName(
            "PlanningMaterialWeeklyLimitEnabledCheck"
        )
        self.material_weekly_limit_enabled_check.toggled.connect(
            self.material_weekly_limit_spin.setEnabled
        )
        self.material_weekly_limit_spin.setEnabled(False)
        self.material_source_combo = QComboBox()
        self.material_source_combo.setObjectName("PlanningMaterialSourceStatusCombo")
        self.material_source_combo.setEditable(True)
        for value, label in _SOURCE_STATUS_OPTIONS:
            self.material_source_combo.addItem(label, value)
        self.material_version_edit = QLineEdit()
        self.material_version_edit.setObjectName("PlanningMaterialVersionEdit")
        self.material_version_edit.setMaxLength(80)
        self.material_inventory_spin = QDoubleSpinBox()
        self.material_inventory_spin.setObjectName("PlanningMaterialInventorySpin")
        self.material_inventory_spin.setRange(0, 999_999_999)
        self.material_inventory_spin.setDecimals(4)
        self.material_inventory_confirmed_check = QCheckBox("庫存數量已人工確認")
        self.material_inventory_confirmed_check.setObjectName(
            "PlanningMaterialInventoryConfirmedCheck"
        )
        self.material_inventory_confirmed_check.toggled.connect(
            self.material_inventory_spin.setEnabled
        )
        self.material_inventory_spin.setEnabled(False)
        self.material_notes_edit = QTextEdit()
        self.material_notes_edit.setObjectName("PlanningMaterialNotesEdit")
        self.material_notes_edit.setMaximumHeight(90)

        material_form = QFormLayout()
        material_form.addRow("素材代碼", self.material_code_edit)
        material_form.addRow("名稱", self.material_name_edit)
        material_form.addRow("類別", self.material_category_edit)
        material_form.addRow("單次結晶波片", self.material_waveplate_spin)
        material_form.addRow("平均產出", self.material_average_spin)
        material_form.addRow("開放星期", weekdays_widget)
        weekly_limit_widget = QWidget()
        weekly_limit_layout = QHBoxLayout(weekly_limit_widget)
        weekly_limit_layout.setContentsMargins(0, 0, 0, 0)
        weekly_limit_layout.addWidget(self.material_weekly_limit_spin)
        weekly_limit_layout.addWidget(self.material_weekly_limit_enabled_check)
        material_form.addRow("每週上限", weekly_limit_widget)
        material_form.addRow("資料來源狀態", self.material_source_combo)
        material_form.addRow("資料版本", self.material_version_edit)
        inventory_widget = QWidget()
        inventory_layout = QHBoxLayout(inventory_widget)
        inventory_layout.setContentsMargins(0, 0, 0, 0)
        inventory_layout.addWidget(self.material_inventory_spin)
        inventory_layout.addWidget(self.material_inventory_confirmed_check)
        material_form.addRow("目前庫存", inventory_widget)
        material_form.addRow("備註", self.material_notes_edit)

        self.material_archive_button = QPushButton("刪除未使用素材")
        self.material_archive_button.setObjectName("PlanningMaterialArchiveButton")
        self.material_archive_button.clicked.connect(self._archive_material)
        self.material_clear_button = QPushButton("清空表單")
        self.material_clear_button.setObjectName("PlanningMaterialClearButton")
        self.material_clear_button.clicked.connect(self.clear_material_form)
        self.material_save_button = QPushButton("儲存素材與庫存")
        self.material_save_button.setObjectName("PlanningMaterialSaveButton")
        self.material_save_button.clicked.connect(self._save_material)
        actions = QHBoxLayout()
        actions.addWidget(self.material_archive_button)
        actions.addStretch(1)
        actions.addWidget(self.material_clear_button)
        actions.addWidget(self.material_save_button)

        editor = QWidget()
        editor_layout = QVBoxLayout(editor)
        editor_layout.addLayout(material_form)
        editor_layout.addStretch(1)
        editor_layout.addLayout(actions)
        editor_scroll = QScrollArea()
        editor_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        editor_scroll.setWidgetResizable(True)
        editor_scroll.setWidget(editor)
        editor_group = QGroupBox("素材內容")
        editor_group_layout = QVBoxLayout(editor_group)
        editor_group_layout.addWidget(editor_scroll)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(list_group)
        splitter.addWidget(editor_group)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.addWidget(splitter)
        return tab

    def _build_target_tab(self) -> QWidget:
        self.target_model = CultivationTargetTableModel(self)
        self.target_table = _table("PlanningTargetTable", self.target_model)
        target_selection = self.target_table.selectionModel()
        if target_selection is not None:
            target_selection.currentRowChanged.connect(self._on_target_selected)
        self.target_new_button = QPushButton("新增養成目標")
        self.target_new_button.setObjectName("PlanningTargetNewButton")
        self.target_new_button.clicked.connect(self.clear_target_form)

        list_group = QGroupBox("養成目標")
        list_layout = QVBoxLayout(list_group)
        list_layout.addWidget(self.target_table, 1)
        list_layout.addWidget(self.target_new_button)

        self.target_character_combo = QComboBox()
        self.target_character_combo.setObjectName("PlanningTargetCharacterCombo")
        self.target_character_combo.addItem("全帳號 / 未指定角色", None)
        self.target_type_combo = QComboBox()
        self.target_type_combo.setObjectName("PlanningTargetTypeCombo")
        self.target_type_combo.setEditable(True)
        for value, label in _TARGET_TYPE_OPTIONS:
            self.target_type_combo.addItem(f"{label} ({value})", value)
        self.target_key_edit = QLineEdit()
        self.target_key_edit.setObjectName("PlanningTargetKeyEdit")
        self.target_current_edit = QTextEdit()
        self.target_current_edit.setObjectName("PlanningTargetCurrentEdit")
        self.target_current_edit.setMaximumHeight(70)
        self.target_current_edit.setPlaceholderText("可輸入 JSON 或簡單文字")
        self.target_value_edit = QTextEdit()
        self.target_value_edit.setObjectName("PlanningTargetValueEdit")
        self.target_value_edit.setMaximumHeight(70)
        self.target_value_edit.setPlaceholderText("可輸入 JSON 或簡單文字")
        self.target_priority_spin = QSpinBox()
        self.target_priority_spin.setObjectName("PlanningTargetPrioritySpin")
        self.target_priority_spin.setRange(0, 9_999)
        self.target_priority_spin.setValue(100)
        self.target_completed_check = QCheckBox("已完成")
        self.target_completed_check.setObjectName("PlanningTargetCompletedCheck")
        self.target_due_enabled = QCheckBox("設定到期日")
        self.target_due_enabled.setObjectName("PlanningTargetDueEnabled")
        self.target_due_date_edit = QDateEdit(QDate.currentDate())
        self.target_due_date_edit.setObjectName("PlanningTargetDueDateEdit")
        self.target_due_date_edit.setCalendarPopup(True)
        self.target_due_date_edit.setDisplayFormat("yyyy-MM-dd")
        self.target_due_date_edit.setEnabled(False)
        self.target_due_enabled.toggled.connect(self.target_due_date_edit.setEnabled)
        due_widget = QWidget()
        due_layout = QHBoxLayout(due_widget)
        due_layout.setContentsMargins(0, 0, 0, 0)
        due_layout.addWidget(self.target_due_enabled)
        due_layout.addWidget(self.target_due_date_edit)
        due_layout.addStretch(1)
        self.target_notes_edit = QTextEdit()
        self.target_notes_edit.setObjectName("PlanningTargetNotesEdit")
        self.target_notes_edit.setMaximumHeight(90)

        form = QFormLayout()
        form.addRow("角色", self.target_character_combo)
        form.addRow("目標類型", self.target_type_combo)
        form.addRow("目標 key", self.target_key_edit)
        form.addRow("目前值", self.target_current_edit)
        form.addRow("目標值", self.target_value_edit)
        form.addRow("優先級", self.target_priority_spin)
        form.addRow("完成狀態", self.target_completed_check)
        form.addRow("到期日", due_widget)
        form.addRow("備註", self.target_notes_edit)

        self.required_material_model = RequiredMaterialTableModel(self)
        self.required_material_table = _table(
            "PlanningRequiredMaterialTable", self.required_material_model, editable=True
        )
        self.required_material_table.setMinimumHeight(120)
        self.required_material_combo = QComboBox()
        self.required_material_combo.setObjectName("PlanningRequiredMaterialCombo")
        self.required_material_quantity_spin = QDoubleSpinBox()
        self.required_material_quantity_spin.setObjectName("PlanningRequiredMaterialQuantitySpin")
        self.required_material_quantity_spin.setRange(0, 999_999_999)
        self.required_material_quantity_spin.setDecimals(4)
        self.required_material_add_button = QPushButton("加入 / 更新")
        self.required_material_add_button.setObjectName("PlanningRequiredMaterialAddButton")
        self.required_material_add_button.clicked.connect(self._add_required_material)
        self.required_material_remove_button = QPushButton("移除選取")
        self.required_material_remove_button.setObjectName("PlanningRequiredMaterialRemoveButton")
        self.required_material_remove_button.clicked.connect(self._remove_required_material)
        required_actions = QHBoxLayout()
        required_actions.addWidget(self.required_material_combo, 1)
        required_actions.addWidget(self.required_material_quantity_spin)
        required_actions.addWidget(self.required_material_add_button)
        required_actions.addWidget(self.required_material_remove_button)
        required_group = QGroupBox("所需素材")
        required_layout = QVBoxLayout(required_group)
        required_layout.addLayout(required_actions)
        required_layout.addWidget(self.required_material_table)

        self.target_archive_button = QPushButton("封存目標")
        self.target_archive_button.setObjectName("PlanningTargetArchiveButton")
        self.target_archive_button.clicked.connect(self._archive_target)
        self.target_clear_button = QPushButton("清空表單")
        self.target_clear_button.setObjectName("PlanningTargetClearButton")
        self.target_clear_button.clicked.connect(self.clear_target_form)
        self.target_save_button = QPushButton("儲存養成目標")
        self.target_save_button.setObjectName("PlanningTargetSaveButton")
        self.target_save_button.clicked.connect(self._save_target)
        actions = QHBoxLayout()
        actions.addWidget(self.target_archive_button)
        actions.addStretch(1)
        actions.addWidget(self.target_clear_button)
        actions.addWidget(self.target_save_button)

        editor = QWidget()
        editor_layout = QVBoxLayout(editor)
        editor_layout.addLayout(form)
        editor_layout.addWidget(required_group)
        editor_layout.addLayout(actions)
        editor_scroll = QScrollArea()
        editor_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        editor_scroll.setWidgetResizable(True)
        editor_scroll.setWidget(editor)
        editor_group = QGroupBox("目標內容")
        editor_group_layout = QVBoxLayout(editor_group)
        editor_group_layout.addWidget(editor_scroll)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(list_group)
        splitter.addWidget(editor_group)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.addWidget(splitter)
        return tab

    def _weekday_check(self, day: int) -> QCheckBox:
        check = QCheckBox(_WEEKDAY_LABELS[day])
        check.setObjectName(f"PlanningMaterialWeekday{day}Check")
        check.setProperty("isoWeekday", day)
        return check

    @Slot(object)
    def set_bundle(self, bundle: object) -> None:
        if not isinstance(bundle, PlanningDataBundle):
            raise TypeError("bundle must be PlanningDataBundle")
        selected_material = self._material_id
        selected_target = self._target_id
        self._bundle = bundle
        self.material_model.set_rows(bundle.materials)
        self.target_model.set_rows(bundle.targets)
        self._set_character_choices(bundle.characters)
        self._set_material_choices(bundle.materials)
        if selected_material is not None and self.select_material(selected_material):
            material = self._material_by_id(selected_material)
            if material is not None:
                self.apply_material(material)
        elif selected_material is not None:
            self.clear_material_form()
        if selected_target is not None and self.select_target(selected_target):
            target = self._target_by_id(selected_target)
            if target is not None:
                self.apply_target(target)
        elif selected_target is not None:
            self.clear_target_form()
        self.set_loading(False)
        self.status_label.setProperty("error", False)
        self.status_label.setText(
            f"已載入 {len(bundle.materials)} 項素材與 {len(bundle.targets)} 個養成目標。"
        )
        _repolish(self.status_label)

    @Slot(object)
    def apply_material(self, material: object) -> None:
        if not isinstance(material, MaterialView):
            raise TypeError("material must be MaterialView")
        self._material_id = material.identifier
        self.material_code_edit.setText(material.code)
        self.material_name_edit.setText(material.name)
        self.material_category_edit.setText(material.category)
        self.material_waveplate_spin.setValue(material.waveplate_cost_per_run)
        self.material_average_spin.setValue(material.average_output)
        weekdays = set(material.open_weekdays)
        for day, check in enumerate(self.material_weekday_checks, start=1):
            check.setChecked(day in weekdays)
        self.material_weekly_limit_enabled_check.setChecked(material.weekly_limit is not None)
        self.material_weekly_limit_spin.setValue(material.weekly_limit or 0)
        _select_editable_combo(self.material_source_combo, material.source_status)
        self.material_version_edit.setText(material.version or "")
        self.material_inventory_confirmed_check.setChecked(material.inventory_count is not None)
        self.material_inventory_spin.setValue(material.inventory_count or 0)
        self.material_notes_edit.setPlainText(material.notes)
        self.material_archive_button.setEnabled(not material.archived and not self._loading)
        self.select_material(material.identifier)

    @Slot(object)
    def apply_target(self, target: object) -> None:
        if not isinstance(target, CultivationTargetView):
            raise TypeError("target must be CultivationTargetView")
        self._target_id = target.identifier
        _select_combo_data(self.target_character_combo, target.character_id)
        _select_editable_combo(self.target_type_combo, target.target_type)
        self.target_key_edit.setText(target.target_key or "")
        self.target_current_edit.setPlainText(target.current_value)
        self.target_value_edit.setPlainText(target.target_value)
        self.target_priority_spin.setValue(target.priority)
        self.target_completed_check.setChecked(target.completed)
        self.target_due_enabled.setChecked(target.due_date is not None)
        if target.due_date is not None:
            self.target_due_date_edit.setDate(_to_qdate(target.due_date))
        self.target_notes_edit.setPlainText(target.notes)
        self.required_material_model.set_rows(target.required_materials)
        self.target_archive_button.setEnabled(not target.archived and not self._loading)
        self.select_target(target.identifier)

    @Slot()
    def clear_material_form(self) -> None:
        self._material_id = None
        _clear_selection(self.material_table)
        self.material_code_edit.clear()
        self.material_name_edit.clear()
        self.material_category_edit.clear()
        self.material_waveplate_spin.setValue(0)
        self.material_average_spin.setValue(0)
        for check in self.material_weekday_checks:
            check.setChecked(False)
        self.material_weekly_limit_enabled_check.setChecked(False)
        self.material_weekly_limit_spin.setValue(0)
        self.material_source_combo.setCurrentIndex(0)
        self.material_version_edit.clear()
        self.material_inventory_confirmed_check.setChecked(False)
        self.material_inventory_spin.setValue(0)
        self.material_notes_edit.clear()
        self.material_archive_button.setEnabled(False)

    @Slot()
    def clear_target_form(self) -> None:
        self._target_id = None
        _clear_selection(self.target_table)
        self.target_character_combo.setCurrentIndex(0)
        self.target_type_combo.setCurrentIndex(0)
        self.target_key_edit.clear()
        self.target_current_edit.clear()
        self.target_value_edit.clear()
        self.target_priority_spin.setValue(100)
        self.target_completed_check.setChecked(False)
        self.target_due_enabled.setChecked(False)
        self.target_due_date_edit.setDate(QDate.currentDate())
        self.target_notes_edit.clear()
        self.required_material_model.set_rows(())
        self.target_archive_button.setEnabled(False)

    @Slot(bool, str)
    def set_loading(self, loading: bool, message: str = "正在處理本機規劃資料…") -> None:
        self._loading = loading
        self.tabs.setEnabled(not loading)
        if loading:
            self.status_label.setProperty("error", False)
            self.status_label.setText(message)
            _repolish(self.status_label)
        else:
            material = self._material_by_id(self._material_id)
            target = self._target_by_id(self._target_id)
            self.material_archive_button.setEnabled(material is not None and not material.archived)
            self.target_archive_button.setEnabled(target is not None and not target.archived)

    @Slot(str)
    def show_error(self, message: str) -> None:
        self.set_loading(False)
        self.status_label.setProperty("error", True)
        self.status_label.setText(message)
        _repolish(self.status_label)

    @Slot(str)
    def show_status(self, message: str) -> None:
        self.set_loading(False)
        self.status_label.setProperty("error", False)
        self.status_label.setText(message)
        _repolish(self.status_label)

    def select_material(self, identifier: str) -> bool:
        row = self.material_model.row_for(identifier)
        if row is None:
            return False
        blocker = QSignalBlocker(self.material_table.selectionModel())
        self.material_table.setCurrentIndex(self.material_model.index(row, 0))
        del blocker
        return True

    def select_target(self, identifier: str) -> bool:
        row = self.target_model.row_for(identifier)
        if row is None:
            return False
        blocker = QSignalBlocker(self.target_table.selectionModel())
        self.target_table.setCurrentIndex(self.target_model.index(row, 0))
        del blocker
        return True

    def build_material_request(self) -> MaterialSaveRequest:
        return MaterialSaveRequest(
            identifier=self._material_id,
            code=self.material_code_edit.text().strip(),
            name=self.material_name_edit.text().strip(),
            category=self.material_category_edit.text().strip(),
            waveplate_cost_per_run=self.material_waveplate_spin.value(),
            average_output=self.material_average_spin.value(),
            open_weekdays=tuple(
                day
                for day, check in enumerate(self.material_weekday_checks, start=1)
                if check.isChecked()
            ),
            weekly_limit=(
                self.material_weekly_limit_spin.value()
                if self.material_weekly_limit_enabled_check.isChecked()
                else None
            ),
            source_status=_editable_combo_value(self.material_source_combo),
            version=self.material_version_edit.text().strip() or None,
            inventory_count=(
                self.material_inventory_spin.value()
                if self.material_inventory_confirmed_check.isChecked()
                else None
            ),
            notes=self.material_notes_edit.toPlainText().strip(),
        )

    def build_target_request(self) -> CultivationTargetSaveRequest:
        due_date: date | None = None
        if self.target_due_enabled.isChecked():
            selected = self.target_due_date_edit.date()
            due_date = date(selected.year(), selected.month(), selected.day())
        character_data = self.target_character_combo.currentData()
        character_id = character_data if isinstance(character_data, str) else None
        target_type = _editable_combo_value(self.target_type_combo)
        return CultivationTargetSaveRequest(
            identifier=self._target_id,
            character_id=character_id,
            target_type=target_type,
            target_key=self.target_key_edit.text().strip() or None,
            current_value=self.target_current_edit.toPlainText().strip(),
            target_value=self.target_value_edit.toPlainText().strip(),
            priority=self.target_priority_spin.value(),
            completed=self.target_completed_check.isChecked(),
            due_date=due_date,
            notes=self.target_notes_edit.toPlainText().strip(),
            required_materials=tuple(
                RequiredMaterialRequest(row.material_id, row.quantity)
                for row in self.required_material_model.rows()
            ),
        )

    def _set_character_choices(self, choices: Sequence[PlanningCharacterChoiceView]) -> None:
        selected = self.target_character_combo.currentData()
        blocker = QSignalBlocker(self.target_character_combo)
        self.target_character_combo.clear()
        self.target_character_combo.addItem("全帳號 / 未指定角色", None)
        for choice in choices:
            self.target_character_combo.addItem(choice.name, choice.identifier)
        _select_combo_data(self.target_character_combo, selected)
        del blocker

    def _set_material_choices(self, materials: Sequence[MaterialView]) -> None:
        selected = self.required_material_combo.currentData()
        blocker = QSignalBlocker(self.required_material_combo)
        self.required_material_combo.clear()
        for material in materials:
            if not material.archived:
                self.required_material_combo.addItem(
                    f"{material.name} ({material.code})", material.identifier
                )
        _select_combo_data(self.required_material_combo, selected)
        del blocker

    def _material_by_id(self, identifier: str | None) -> MaterialView | None:
        return next((row for row in self._bundle.materials if row.identifier == identifier), None)

    def _target_by_id(self, identifier: str | None) -> CultivationTargetView | None:
        return next((row for row in self._bundle.targets if row.identifier == identifier), None)

    def _on_material_selected(self, current: QModelIndex, previous: QModelIndex) -> None:
        del previous
        identifier = self.material_model.identifier_at(current.row()) if current.isValid() else None
        if identifier is None:
            return
        material = self._material_by_id(identifier)
        if material is not None:
            self.apply_material(material)
        self.material_selected.emit(identifier)

    def _on_target_selected(self, current: QModelIndex, previous: QModelIndex) -> None:
        del previous
        identifier = self.target_model.identifier_at(current.row()) if current.isValid() else None
        if identifier is None:
            return
        target = self._target_by_id(identifier)
        if target is not None:
            self.apply_target(target)
        self.target_selected.emit(identifier)

    def _add_required_material(self) -> None:
        value = self.required_material_combo.currentData()
        if not isinstance(value, str) or not value:
            self.show_error("請先建立並選擇素材。")
            return
        material = self._material_by_id(value)
        if material is None:
            self.show_error("選取的素材已不存在, 請重新載入資料。")
            return
        self.required_material_model.add_row(
            RequiredMaterialView(
                material.identifier,
                material.name,
                self.required_material_quantity_spin.value(),
            )
        )

    def _remove_required_material(self) -> None:
        index = self.required_material_table.currentIndex()
        if index.isValid():
            self.required_material_model.remove_row(index.row())

    def _save_material(self) -> None:
        try:
            request = self.build_material_request()
        except ValueError as error:
            self.show_error(_friendly_validation(error))
            return
        self.status_label.setProperty("error", False)
        self.status_label.setText("正在儲存素材與本機庫存…")
        _repolish(self.status_label)
        self.material_save_requested.emit(request)

    def _save_target(self) -> None:
        try:
            request = self.build_target_request()
        except ValueError as error:
            self.show_error(_friendly_validation(error))
            return
        self.status_label.setProperty("error", False)
        self.status_label.setText("正在儲存養成目標…")
        _repolish(self.status_label)
        self.target_save_requested.emit(request)

    def _archive_material(self) -> None:
        if self._material_id is None:
            return
        answer = QMessageBox.question(
            self,
            "刪除未使用素材",
            "確定要刪除此素材與其本機庫存嗎? 若已被養成目標或歷史規劃引用, 系統會拒絕刪除。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer is QMessageBox.StandardButton.Yes:
            self.material_archive_requested.emit(self._material_id)

    def _archive_target(self) -> None:
        if self._target_id is None:
            return
        answer = QMessageBox.question(
            self,
            "封存養成目標",
            "確定要封存此目標嗎? 歷史資料會保留, 且不會再列入今日規劃。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer is QMessageBox.StandardButton.Yes:
            self.target_archive_requested.emit(self._target_id)


def _table(object_name: str, model: QAbstractTableModel, *, editable: bool = False) -> QTableView:
    table = QTableView()
    table.setObjectName(object_name)
    table.setModel(model)
    table.setAlternatingRowColors(True)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setEditTriggers(
        QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed
        if editable
        else QAbstractItemView.EditTrigger.NoEditTriggers
    )
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    return table


def _clear_selection(table: QTableView) -> None:
    blocker = QSignalBlocker(table.selectionModel())
    table.clearSelection()
    table.setCurrentIndex(_INVALID_INDEX)
    del blocker


def _select_combo_data(combo: QComboBox, value: object) -> bool:
    index = combo.findData(value)
    if index < 0:
        return False
    combo.setCurrentIndex(index)
    return True


def _select_editable_combo(combo: QComboBox, value: str) -> None:
    index = combo.findData(value)
    if index >= 0:
        combo.setCurrentIndex(index)
    else:
        combo.setCurrentText(value)


def _editable_combo_value(combo: QComboBox) -> str:
    index = combo.currentIndex()
    data = combo.currentData()
    if (
        index >= 0
        and isinstance(data, str)
        and data
        and combo.currentText() == combo.itemText(index)
    ):
        return data
    return combo.currentText().strip()


def _format_weekdays(days: tuple[int, ...]) -> str:
    return "、".join(_WEEKDAY_LABELS[day] for day in days) if days else "未設定 / 未知"


def _source_label(source_status: str) -> str:
    return next(
        (label for value, label in _SOURCE_STATUS_OPTIONS if value == source_status), source_status
    )


def _stored_quantity(value: object) -> float:
    if type(value) is bool:
        raise TypeError("boolean is not a quantity")
    try:
        quantity = float(value) if isinstance(value, (int, float, str)) else float("nan")
    except ValueError as exc:
        raise TypeError("quantity must be numeric") from exc
    _validate_nonnegative_quantity(quantity, "quantity")
    return round(quantity, 4)


def _to_qdate(value: date) -> QDate:
    return QDate(value.year, value.month, value.day)


def _friendly_validation(error: ValueError) -> str:
    message = str(error)
    replacements = {
        "material code cannot be empty": "素材代碼不可空白。",
        "material name cannot be empty": "素材名稱不可空白。",
        "material category cannot be empty": "素材類別不可空白。",
        "target_type cannot be empty": "目標類型不可空白。",
        "target_key cannot be empty": "目標 key 不可空白。",
        "target_value cannot be empty": "目標值不可空白。",
    }
    return replacements.get(message, message)


def _repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


__all__ = [
    "CultivationTargetSaveRequest",
    "CultivationTargetTableModel",
    "CultivationTargetView",
    "MaterialSaveRequest",
    "MaterialTableModel",
    "MaterialView",
    "PlanningCharacterChoiceView",
    "PlanningDataBundle",
    "PlanningDataDialog",
    "RequiredMaterialRequest",
    "RequiredMaterialTableModel",
    "RequiredMaterialView",
]
