"""Manual, offline gacha resource planning page."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from PySide6.QtCore import (
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
    QCheckBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableView,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from wuwa_assistant.public_data.models import OfficialNotice
from wuwa_assistant.ui.widgets import OfficialFeedWidget
from wuwa_assistant.ui.widgets.choice_selector import ChoiceSelector as QComboBox
from wuwa_assistant.ui.widgets.official_feed import BannerCountdownWidget

_INVALID_INDEX = QModelIndex()
type ModelIndex = QModelIndex | QPersistentModelIndex


def _required(value: str, label: str) -> None:
    if not value.strip():
        raise ValueError(f"{label}不可空白")


def _nonnegative(value: int, label: str, *, maximum: int = 999_999_999) -> None:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{label}必須介於 0 到 {maximum}")


@dataclass(frozen=True, slots=True)
class GachaStatusView:
    astrite: int = 0
    character_pulls: int = 0
    weapon_pulls: int = 0
    character_pity: int = 0
    weapon_pity: int = 0
    character_guaranteed: bool = False
    weapon_guaranteed: bool = False

    def __post_init__(self) -> None:
        for value, label in (
            (self.astrite, "astrite"),
            (self.character_pulls, "character_pulls"),
            (self.weapon_pulls, "weapon_pulls"),
            (self.character_pity, "character_pity"),
            (self.weapon_pity, "weapon_pity"),
        ):
            _nonnegative(value, label)


@dataclass(frozen=True, slots=True)
class GachaStatusSaveRequest:
    astrite: int
    character_pulls: int
    weapon_pulls: int
    character_pity: int
    weapon_pity: int
    character_guaranteed: bool
    weapon_guaranteed: bool

    def __post_init__(self) -> None:
        GachaStatusView(
            self.astrite,
            self.character_pulls,
            self.weapon_pulls,
            self.character_pity,
            self.weapon_pity,
            self.character_guaranteed,
            self.weapon_guaranteed,
        )


@dataclass(frozen=True, slots=True)
class GachaTargetView:
    identifier: str
    target_type: str
    name: str
    copies: int
    target_version: str | None
    target_date: date | None
    priority: int
    status: str
    conservative_required: int | None = None
    typical_required: int | None = None
    optimistic_required: int | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        _required(self.identifier, "目標識別碼")
        _required(self.target_type, "目標類型")
        _required(self.name, "目標名稱")
        _required(self.status, "目標狀態")
        if self.copies < 1:
            raise ValueError("copies must be positive")
        _nonnegative(self.priority, "priority", maximum=9_999)
        values = (
            self.conservative_required,
            self.typical_required,
            self.optimistic_required,
        )
        if any(value is not None and value < 0 for value in values):
            raise ValueError("scenario requirements must be nonnegative or null")


@dataclass(frozen=True, slots=True)
class GachaTargetSaveRequest:
    identifier: str | None
    target_type: str
    name: str
    copies: int
    target_version: str | None
    target_date: date | None
    priority: int
    status: str
    conservative_required: int | None
    typical_required: int | None
    optimistic_required: int | None
    notes: str

    def __post_init__(self) -> None:
        GachaTargetView(
            self.identifier or "new-target",
            self.target_type,
            self.name,
            self.copies,
            self.target_version,
            self.target_date,
            self.priority,
            self.status,
            self.conservative_required,
            self.typical_required,
            self.optimistic_required,
            self.notes,
        )


@dataclass(frozen=True, slots=True)
class GachaIncomeView:
    identifier: str
    label: str
    expected_date: date | None
    astrite_amount: int
    pull_amount: int
    pull_type: str
    scenario: str
    received: bool = False
    notes: str = ""

    def __post_init__(self) -> None:
        _required(self.identifier, "收入識別碼")
        _required(self.label, "收入名稱")
        if self.pull_type not in {"character", "weapon", "unknown"}:
            raise ValueError("pull_type is invalid")
        if self.scenario not in {"general", "conservative", "typical", "optimistic"}:
            raise ValueError("scenario is invalid")
        _nonnegative(self.astrite_amount, "astrite_amount")
        _nonnegative(self.pull_amount, "pull_amount")


@dataclass(frozen=True, slots=True)
class GachaIncomeSaveRequest:
    identifier: str | None
    label: str
    expected_date: date | None
    astrite_amount: int
    pull_amount: int
    pull_type: str
    scenario: str
    received: bool
    notes: str

    def __post_init__(self) -> None:
        GachaIncomeView(
            self.identifier or "new-income",
            self.label,
            self.expected_date,
            self.astrite_amount,
            self.pull_amount,
            self.pull_type,
            self.scenario,
            self.received,
            self.notes,
        )


@dataclass(frozen=True, slots=True)
class GachaProjectionView:
    scenario: str
    character_available: int
    weapon_available: int
    unallocated_astrite: int
    shortfall: int
    warning_count: int = 0
    remaining_character_pulls: int = 0
    remaining_weapon_pulls: int = 0
    unknown_target_count: int = 0

    def __post_init__(self) -> None:
        if self.scenario not in {"conservative", "typical", "optimistic"}:
            raise ValueError("projection scenario is invalid")
        for value in (
            self.character_available,
            self.weapon_available,
            self.unallocated_astrite,
            self.shortfall,
            self.warning_count,
            self.remaining_character_pulls,
            self.remaining_weapon_pulls,
            self.unknown_target_count,
        ):
            _nonnegative(value, "projection value")


@dataclass(frozen=True, slots=True)
class GachaSnapshot:
    status: GachaStatusView = GachaStatusView()
    targets: tuple[GachaTargetView, ...] = ()
    incomes: tuple[GachaIncomeView, ...] = ()
    projections: tuple[GachaProjectionView, ...] = ()
    warnings: tuple[str, ...] = ()


class _TargetTableModel(QAbstractTableModel):
    _HEADERS = ("目標", "類型", "版本 / 日期", "份數", "優先級", "三情境需求", "狀態")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[GachaTargetView] = []

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
            destination = row.target_version or ""
            if row.target_date is not None:
                destination = f"{destination} · {row.target_date.isoformat()}".strip(" ·")
            requirements = " / ".join(
                "?" if value is None else str(value)
                for value in (
                    row.conservative_required,
                    row.typical_required,
                    row.optimistic_required,
                )
            )
            return (
                row.name,
                _target_type_label(row.target_type),
                destination or "未設定",
                row.copies,
                row.priority,
                requirements,
                row.status,
            )[index.column()]
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
            and 0 <= section < len(self._HEADERS)
        ):
            return self._HEADERS[section]
        return None

    def set_rows(self, rows: Sequence[GachaTargetView]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def row(self, index: ModelIndex) -> GachaTargetView | None:
        return (
            self._rows[index.row()]
            if index.isValid() and 0 <= index.row() < len(self._rows)
            else None
        )


class _IncomeTableModel(QAbstractTableModel):
    _HEADERS = ("收入", "日期", "星聲", "抽數", "券種", "情境", "狀態")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[GachaIncomeView] = []

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
            return (
                row.label,
                "未設定" if row.expected_date is None else row.expected_date.isoformat(),
                row.astrite_amount,
                row.pull_amount,
                _pull_type_label(row.pull_type),
                _scenario_label(row.scenario),
                "已領取" if row.received else "預計",
            )[index.column()]
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
            and 0 <= section < len(self._HEADERS)
        ):
            return self._HEADERS[section]
        return None

    def set_rows(self, rows: Sequence[GachaIncomeView]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def row(self, index: ModelIndex) -> GachaIncomeView | None:
        return (
            self._rows[index.row()]
            if index.isValid() and 0 <= index.row() < len(self._rows)
            else None
        )


class _ProjectionCard(QFrame):
    def __init__(self, title: str, object_name: str) -> None:
        super().__init__()
        self.setObjectName("MetricCard")
        heading = QLabel(title)
        heading.setObjectName("MetricTitle")
        self.value_label = QLabel("資料不足")
        self.value_label.setObjectName(object_name)
        self.value_label.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addWidget(self.value_label)

    def set_projection(self, value: GachaProjectionView | None) -> None:
        if value is None:
            self.value_label.setText("資料不足")
            return
        gap_text = (
            f"已知缺口 {value.shortfall} · {value.unknown_target_count} 個目標資料不足"
            if value.unknown_target_count
            else f"缺口 {value.shortfall}"
        )
        self.value_label.setText(
            f"角色目標已配置 {value.character_available} 抽 · "
            f"武器目標已配置 {value.weapon_available} 抽\n"
            f"剩餘角色券 {value.remaining_character_pulls} · "
            f"剩餘武器券 {value.remaining_weapon_pulls}\n"
            f"未分配星聲 {value.unallocated_astrite}"
            f" (可換 {value.unallocated_astrite // 160} 抽) · {gap_text}"
        )


class GachaPage(QWidget):
    """Signal-only manual gacha planning workspace."""

    refresh_requested = Signal()
    status_save_requested = Signal(object)
    target_save_requested = Signal(object)
    target_delete_requested = Signal(str)
    income_save_requested = Signal(object)
    income_delete_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("GachaPage")
        self._targets: tuple[GachaTargetView, ...] = ()
        self._incomes: tuple[GachaIncomeView, ...] = ()

        title = QLabel("卡池倒數")
        title.setObjectName("PageTitle")
        description = QLabel("當期角色與結束倒數、下一期角色與開啟倒數。時間依 Asia 伺服器顯示。")
        description.setObjectName("PageDescription")
        description.setWordWrap(True)

        self.astrite_spin = _large_spin()
        self.character_pulls_spin = _large_spin()
        self.weapon_pulls_spin = _large_spin()
        self.character_pity_spin = _large_spin(999)
        self.weapon_pity_spin = _large_spin(999)
        self.character_guaranteed_check = QCheckBox("角色池已保證")
        self.weapon_guaranteed_check = QCheckBox("武器池已保證")
        self.status_save_button = QPushButton("保存目前資源")
        self.status_save_button.clicked.connect(self._save_status)
        status_form = QFormLayout()
        status_form.setVerticalSpacing(12)
        status_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        status_form.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        status_form.addRow("星聲", self.astrite_spin)
        status_form.addRow("限定角色抽數", self.character_pulls_spin)
        status_form.addRow("限定武器抽數", self.weapon_pulls_spin)
        status_form.addRow("角色池墊抽", self.character_pity_spin)
        status_form.addRow("武器池墊抽", self.weapon_pity_spin)
        status_form.addRow(self.character_guaranteed_check)
        status_form.addRow(self.weapon_guaranteed_check)
        status_form.addRow(self.status_save_button)
        status_frame = QFrame()
        status_frame.setObjectName("Panel")
        status_frame.setLayout(status_form)

        self.official_feed = OfficialFeedWidget("官方卡池公告")
        self.official_feed.setMaximumHeight(220)

        self.projection_cards = {
            "conservative": _ProjectionCard("最保守", "GachaConservativeProjection"),
            "typical": _ProjectionCard("一般", "GachaTypicalProjection"),
            "optimistic": _ProjectionCard("樂觀", "GachaOptimisticProjection"),
        }
        projection_grid = QGridLayout()
        for column, card in enumerate(self.projection_cards.values()):
            projection_grid.addWidget(card, 0, column)

        self.target_model = _TargetTableModel(self)
        self.target_table = _table("GachaTargetsTable", self.target_model)
        self.target_add_button = QPushButton("新增目標")
        self.target_edit_button = QPushButton("編輯")
        self.target_delete_button = QPushButton("刪除")
        self.target_add_button.clicked.connect(self._add_target)
        self.target_edit_button.clicked.connect(self._edit_target)
        self.target_delete_button.clicked.connect(self._delete_target)
        target_buttons = QHBoxLayout()
        for button in (
            self.target_add_button,
            self.target_edit_button,
            self.target_delete_button,
        ):
            target_buttons.addWidget(button)
        target_buttons.addStretch(1)

        self.income_model = _IncomeTableModel(self)
        self.income_table = _table("GachaIncomeTable", self.income_model)
        self.income_add_button = QPushButton("新增收入")
        self.income_edit_button = QPushButton("編輯")
        self.income_delete_button = QPushButton("刪除")
        self.income_add_button.clicked.connect(self._add_income)
        self.income_edit_button.clicked.connect(self._edit_income)
        self.income_delete_button.clicked.connect(self._delete_income)
        income_buttons = QHBoxLayout()
        for button in (
            self.income_add_button,
            self.income_edit_button,
            self.income_delete_button,
        ):
            income_buttons.addWidget(button)
        income_buttons.addStretch(1)

        self.warning_label = QLabel("尚無資料警告。")
        self.warning_label.setObjectName("GachaWarnings")
        self.warning_label.setWordWrap(True)
        self.status_label = QLabel("正在載入本機抽卡資料…")
        self.status_label.setObjectName("GachaStatus")
        self.status_label.setWordWrap(True)
        self.refresh_button = QPushButton("重新整理")
        self.refresh_button.clicked.connect(self.refresh_requested)
        status_row = QHBoxLayout()
        status_row.addWidget(self.refresh_button)
        status_row.addWidget(self.status_label, 1)

        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("GachaScrollArea")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 10, 0)
        layout.setSpacing(14)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        layout.addWidget(self.official_feed)
        layout.addWidget(status_frame)
        layout.addLayout(projection_grid)
        layout.addWidget(QLabel("抽卡目標"))
        layout.addLayout(target_buttons)
        layout.addWidget(self.target_table, 1)
        layout.addWidget(QLabel("預估收入"))
        layout.addLayout(income_buttons)
        layout.addWidget(self.income_table, 1)
        layout.addWidget(self.warning_label)
        # Keep the old local editor objects compatible with stored-data services;
        # none of the resource/pity controls are exposed by the countdown page.
        self._legacy_content = content
        content.setParent(self)
        content.hide()
        countdown_content = QWidget()
        countdown_layout = QVBoxLayout(countdown_content)
        countdown_layout.setContentsMargins(0, 0, 10, 0)
        self.countdown_feed = BannerCountdownWidget()
        countdown_layout.addWidget(self.countdown_feed)
        countdown_layout.addWidget(self.official_feed)
        countdown_layout.addLayout(status_row)
        countdown_layout.addStretch(1)
        self.scroll_area.setWidget(countdown_content)
        self.status_label.setText("連線時更新官方時程; 離線保留上次資料。")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 30, 40, 24)
        outer.setSpacing(14)
        outer.addWidget(title)
        outer.addWidget(description)
        outer.addWidget(self.scroll_area, 1)

    @Slot(object)
    def set_official_notices(self, notices: object) -> None:
        if not isinstance(notices, tuple) or not all(
            isinstance(item, OfficialNotice) for item in notices
        ):
            raise TypeError("notices must be a tuple of OfficialNotice")
        self.official_feed.set_notices(
            tuple(
                item
                for item in notices
                if item.category == "gacha" or any(row.kind == "gacha" for row in item.schedules)
            )
        )
        self.countdown_feed.set_notices(notices)

    @Slot(object)
    def set_snapshot(self, snapshot: object) -> None:
        if not isinstance(snapshot, GachaSnapshot):
            raise TypeError("snapshot must be GachaSnapshot")
        self._targets = snapshot.targets
        self._incomes = snapshot.incomes
        self.astrite_spin.setValue(snapshot.status.astrite)
        self.character_pulls_spin.setValue(snapshot.status.character_pulls)
        self.weapon_pulls_spin.setValue(snapshot.status.weapon_pulls)
        self.character_pity_spin.setValue(snapshot.status.character_pity)
        self.weapon_pity_spin.setValue(snapshot.status.weapon_pity)
        self.character_guaranteed_check.setChecked(snapshot.status.character_guaranteed)
        self.weapon_guaranteed_check.setChecked(snapshot.status.weapon_guaranteed)
        self.target_model.set_rows(snapshot.targets)
        self.income_model.set_rows(snapshot.incomes)
        projections = {item.scenario: item for item in snapshot.projections}
        for scenario, card in self.projection_cards.items():
            card.set_projection(projections.get(scenario))
        self.warning_label.setText("\n".join(snapshot.warnings) or "尚無資料警告。")
        self.set_loading(False)
        self.status_label.setText("Asia 卡池時程 · 舊有個人規劃資料仍保存在本機。")
        self.status_label.setProperty("error", False)

    @Slot(bool, str)
    def set_loading(self, loading: bool, message: str = "正在處理本機抽卡資料…") -> None:
        for widget in (
            self.status_save_button,
            self.target_add_button,
            self.target_edit_button,
            self.target_delete_button,
            self.income_add_button,
            self.income_edit_button,
            self.income_delete_button,
            self.refresh_button,
        ):
            widget.setEnabled(not loading)
        if loading:
            self.status_label.setText(message)
            self.status_label.setProperty("error", False)

    @Slot(str)
    def show_error(self, message: str) -> None:
        self.set_loading(False)
        self.status_label.setText(f"抽卡規劃操作失敗: {message}")
        self.status_label.setProperty("error", True)

    @Slot(str)
    def show_status(self, message: str) -> None:
        self.set_loading(False)
        self.status_label.setText(message)
        self.status_label.setProperty("error", False)

    def status_request(self) -> GachaStatusSaveRequest:
        return GachaStatusSaveRequest(
            self.astrite_spin.value(),
            self.character_pulls_spin.value(),
            self.weapon_pulls_spin.value(),
            self.character_pity_spin.value(),
            self.weapon_pity_spin.value(),
            self.character_guaranteed_check.isChecked(),
            self.weapon_guaranteed_check.isChecked(),
        )

    def _save_status(self) -> None:
        self.status_save_requested.emit(self.status_request())

    def _selected_target(self) -> GachaTargetView | None:
        return self.target_model.row(self.target_table.currentIndex())

    def _selected_income(self) -> GachaIncomeView | None:
        return self.income_model.row(self.income_table.currentIndex())

    def _add_target(self) -> None:
        dialog = _TargetEditor(parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.target_save_requested.emit(dialog.request())

    def _edit_target(self) -> None:
        selected = self._selected_target()
        if selected is None:
            self.show_error("請先選擇目標")
            return
        dialog = _TargetEditor(selected, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.target_save_requested.emit(dialog.request())

    def _delete_target(self) -> None:
        selected = self._selected_target()
        if selected is not None and _confirm(self, f"確定刪除「{selected.name}」?"):
            self.target_delete_requested.emit(selected.identifier)

    def _add_income(self) -> None:
        dialog = _IncomeEditor(parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.income_save_requested.emit(dialog.request())

    def _edit_income(self) -> None:
        selected = self._selected_income()
        if selected is None:
            self.show_error("請先選擇收入")
            return
        dialog = _IncomeEditor(selected, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.income_save_requested.emit(dialog.request())

    def _delete_income(self) -> None:
        selected = self._selected_income()
        if selected is not None and _confirm(self, f"確定刪除「{selected.label}」?"):
            self.income_delete_requested.emit(selected.identifier)


class _TargetEditor(QDialog):
    def __init__(
        self,
        value: GachaTargetView | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._identifier = None if value is None else value.identifier
        self.setWindowTitle("新增抽卡目標" if value is None else "編輯抽卡目標")
        self.type_combo = QComboBox()
        self.type_combo.addItem("限定角色", "character")
        self.type_combo.addItem("限定武器", "weapon")
        if value is not None:
            self.type_combo.setCurrentIndex(max(0, self.type_combo.findData(value.target_type)))
        self.name_edit = QLineEdit("" if value is None else value.name)
        self.copies_spin = _large_spin(99)
        self.copies_spin.setMinimum(1)
        self.copies_spin.setValue(1 if value is None else value.copies)
        self.version_edit = QLineEdit("" if value is None else value.target_version or "")
        self.date_enabled = QCheckBox("設定目標日期")
        self.date_edit = QDateEdit(QDate.currentDate())
        self.date_edit.setCalendarPopup(True)
        self.date_enabled.toggled.connect(self.date_edit.setEnabled)
        self.date_enabled.setChecked(value is not None and value.target_date is not None)
        self.date_edit.setEnabled(self.date_enabled.isChecked())
        if value is not None and value.target_date is not None:
            self.date_edit.setDate(_qdate(value.target_date))
        self.priority_spin = _large_spin(9_999)
        self.priority_spin.setValue(100 if value is None else value.priority)
        self.status_combo = QComboBox()
        for code, label in (
            ("planned", "規劃中"),
            ("active", "進行中"),
            ("obtained", "已取得"),
            ("acquired", "已取得 (相容狀態)"),
            ("completed", "已完成"),
            ("cancelled", "已取消"),
            ("skipped", "已略過"),
        ):
            self.status_combo.addItem(label, code)
        if value is not None:
            status_index = self.status_combo.findData(value.status)
            if status_index < 0:
                self.status_combo.addItem(f"{value.status} (需人工確認)", value.status)
                status_index = self.status_combo.count() - 1
            self.status_combo.setCurrentIndex(status_index)
        self.requirement_enabled = QCheckBox("三情境需求抽數已人工確認")
        self.conservative_spin = _large_spin()
        self.typical_spin = _large_spin()
        self.optimistic_spin = _large_spin()
        self._original_requirements = (
            None if value is None else value.conservative_required,
            None if value is None else value.typical_required,
            None if value is None else value.optimistic_required,
        )
        has_requirements = value is not None and all(
            item is not None for item in self._original_requirements
        )
        self.requirement_enabled.setChecked(has_requirements)
        for spin in (self.conservative_spin, self.typical_spin, self.optimistic_spin):
            spin.setEnabled(has_requirements)
        self.requirement_enabled.toggled.connect(self.conservative_spin.setEnabled)
        self.requirement_enabled.toggled.connect(self.typical_spin.setEnabled)
        self.requirement_enabled.toggled.connect(self.optimistic_spin.setEnabled)
        self._requirements_touched = False
        self.requirement_enabled.toggled.connect(self._mark_requirements_touched)
        if value is not None:
            self.conservative_spin.setValue(value.conservative_required or 0)
            self.typical_spin.setValue(value.typical_required or 0)
            self.optimistic_spin.setValue(value.optimistic_required or 0)
        self.notes_edit = QTextEdit("" if value is None else value.notes)
        self.notes_edit.setMaximumHeight(90)
        form = QFormLayout()
        form.addRow("目標類型", self.type_combo)
        form.addRow("名稱", self.name_edit)
        form.addRow("份數", self.copies_spin)
        form.addRow("版本", self.version_edit)
        form.addRow(self.date_enabled, self.date_edit)
        form.addRow("優先級", self.priority_spin)
        form.addRow("狀態", self.status_combo)
        form.addRow(self.requirement_enabled)
        form.addRow("最保守需求", self.conservative_spin)
        form.addRow("一般需求", self.typical_spin)
        form.addRow("樂觀需求", self.optimistic_spin)
        form.addRow("備註", self.notes_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_valid)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def request(self) -> GachaTargetSaveRequest:
        confirmed = self.requirement_enabled.isChecked()
        requirements: tuple[int | None, int | None, int | None]
        if confirmed:
            requirements = (
                self.conservative_spin.value(),
                self.typical_spin.value(),
                self.optimistic_spin.value(),
            )
        elif self._identifier is not None and not self._requirements_touched:
            requirements = self._original_requirements
        else:
            requirements = (None, None, None)
        selected = self.date_edit.date()
        return GachaTargetSaveRequest(
            self._identifier,
            str(self.type_combo.currentData()),
            self.name_edit.text().strip(),
            self.copies_spin.value(),
            self.version_edit.text().strip() or None,
            date(selected.year(), selected.month(), selected.day())
            if self.date_enabled.isChecked()
            else None,
            self.priority_spin.value(),
            str(self.status_combo.currentData()),
            requirements[0],
            requirements[1],
            requirements[2],
            self.notes_edit.toPlainText().strip(),
        )

    def _mark_requirements_touched(self, _checked: bool) -> None:
        self._requirements_touched = True

    def _accept_valid(self) -> None:
        try:
            self.request()
        except ValueError as exc:
            QMessageBox.warning(self, "資料不完整", str(exc))
            return
        self.accept()


class _IncomeEditor(QDialog):
    def __init__(
        self,
        value: GachaIncomeView | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._identifier = None if value is None else value.identifier
        self.setWindowTitle("新增預估收入" if value is None else "編輯預估收入")
        self.label_edit = QLineEdit("" if value is None else value.label)
        self.date_enabled = QCheckBox("設定預計日期")
        self.date_edit = QDateEdit(QDate.currentDate())
        self.date_edit.setCalendarPopup(True)
        self.date_enabled.toggled.connect(self.date_edit.setEnabled)
        self.date_enabled.setChecked(value is not None and value.expected_date is not None)
        self.date_edit.setEnabled(self.date_enabled.isChecked())
        if value is not None and value.expected_date is not None:
            self.date_edit.setDate(_qdate(value.expected_date))
        self.astrite_spin = _large_spin()
        self.astrite_spin.setValue(0 if value is None else value.astrite_amount)
        self.pull_spin = _large_spin()
        self.pull_spin.setValue(0 if value is None else value.pull_amount)
        self.pull_type_combo = QComboBox()
        self.pull_type_combo.addItem("角色券", "character")
        self.pull_type_combo.addItem("武器券", "weapon")
        self.pull_type_combo.addItem("未知 / 待確認", "unknown")
        if value is not None:
            self.pull_type_combo.setCurrentIndex(
                max(0, self.pull_type_combo.findData(value.pull_type))
            )
        self.scenario_combo = QComboBox()
        self.scenario_combo.addItem("所有情境皆包含", "general")
        self.scenario_combo.addItem("最保守即包含", "conservative")
        self.scenario_combo.addItem("一般情境起包含", "typical")
        self.scenario_combo.addItem("僅樂觀情境", "optimistic")
        if value is not None:
            self.scenario_combo.setCurrentIndex(
                max(0, self.scenario_combo.findData(value.scenario))
            )
        self.received_check = QCheckBox("已領取 (不再計入未來收入)")
        self.received_check.setChecked(False if value is None else value.received)
        self.notes_edit = QTextEdit("" if value is None else value.notes)
        self.notes_edit.setMaximumHeight(90)
        form = QFormLayout()
        form.addRow("收入名稱", self.label_edit)
        form.addRow(self.date_enabled, self.date_edit)
        form.addRow("星聲", self.astrite_spin)
        form.addRow("直接抽數", self.pull_spin)
        form.addRow("直接抽券種", self.pull_type_combo)
        form.addRow("收入情境", self.scenario_combo)
        form.addRow(self.received_check)
        form.addRow("備註", self.notes_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_valid)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def request(self) -> GachaIncomeSaveRequest:
        selected = self.date_edit.date()
        return GachaIncomeSaveRequest(
            self._identifier,
            self.label_edit.text().strip(),
            date(selected.year(), selected.month(), selected.day())
            if self.date_enabled.isChecked()
            else None,
            self.astrite_spin.value(),
            self.pull_spin.value(),
            str(self.pull_type_combo.currentData()),
            str(self.scenario_combo.currentData()),
            self.received_check.isChecked(),
            self.notes_edit.toPlainText().strip(),
        )

    def _accept_valid(self) -> None:
        try:
            self.request()
        except ValueError as exc:
            QMessageBox.warning(self, "資料不完整", str(exc))
            return
        self.accept()


def _large_spin(maximum: int = 999_999_999) -> QSpinBox:
    spin = QSpinBox()
    spin.setRange(0, maximum)
    spin.setGroupSeparatorShown(True)
    return spin


def _table(name: str, model: QAbstractTableModel) -> QTableView:
    table = QTableView()
    table.setObjectName(name)
    table.setModel(model)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.setMinimumHeight(160)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    return table


def _confirm(parent: QWidget, text: str) -> bool:
    return QMessageBox.question(parent, "確認刪除", text) == QMessageBox.StandardButton.Yes


def _qdate(value: date) -> QDate:
    return QDate(value.year, value.month, value.day)


def _target_type_label(value: str) -> str:
    return {"character": "限定角色", "weapon": "限定武器"}.get(value, value)


def _pull_type_label(value: str) -> str:
    return {"character": "角色券", "weapon": "武器券", "unknown": "待確認"}.get(value, value)


def _scenario_label(value: str) -> str:
    return {
        "general": "所有情境",
        "conservative": "最保守起",
        "typical": "一般起",
        "optimistic": "僅樂觀",
    }.get(value, value)


__all__ = [
    "GachaIncomeSaveRequest",
    "GachaIncomeView",
    "GachaPage",
    "GachaProjectionView",
    "GachaSnapshot",
    "GachaStatusSaveRequest",
    "GachaStatusView",
    "GachaTargetSaveRequest",
    "GachaTargetView",
]
