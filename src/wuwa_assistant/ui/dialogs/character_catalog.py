"""Searchable, multi-select character catalog dialog."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

_INVALID_INDEX = QModelIndex()
type ModelIndex = QModelIndex | QPersistentModelIndex


@dataclass(frozen=True, slots=True)
class CharacterCatalogItemView:
    identifier: str
    name: str
    element: str | None
    weapon_type: str | None
    rarity: int | None
    source_status: str
    already_added: bool = False

    def __post_init__(self) -> None:
        if not self.identifier.strip() or not self.name.strip():
            raise ValueError("catalog item identifier and name cannot be empty")
        if self.rarity is not None and self.rarity not in {4, 5}:
            raise ValueError("catalog item rarity must be 4, 5, or null")


@dataclass(frozen=True, slots=True)
class CharacterCatalogSelectionRequest:
    entry_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.entry_ids or any(not identifier.strip() for identifier in self.entry_ids):
            raise ValueError("catalog selection requires non-empty identifiers")
        if len(self.entry_ids) != len(set(self.entry_ids)):
            raise ValueError("catalog selection identifiers must be unique")


class CharacterCatalogTableModel(QAbstractTableModel):
    _HEADERS = ("加入", "角色", "屬性", "武器", "稀有度", "資料狀態")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[CharacterCatalogItemView] = []
        self._selected: set[str] = set()

    def rowCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._HEADERS)

    def data(self, index: ModelIndex, role: int = int(Qt.ItemDataRole.DisplayRole)) -> object:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        item = self._rows[index.row()]
        if role == int(Qt.ItemDataRole.UserRole):
            return item.identifier
        if role == int(Qt.ItemDataRole.CheckStateRole) and index.column() == 0:
            return (
                Qt.CheckState.Checked
                if item.identifier in self._selected
                else Qt.CheckState.Unchecked
            )
        if role == int(Qt.ItemDataRole.DisplayRole):
            source = (
                "官方已確認"
                if item.source_status == "official_current_character"
                else "僅名稱已收錄"
            )
            values = (
                "已加入" if item.already_added else "",
                item.name,
                item.element or "未確認",
                item.weapon_type or "未確認",
                "未確認" if item.rarity is None else f"{item.rarity} 星",
                source,
            )
            return values[index.column()]
        if role == int(Qt.ItemDataRole.ToolTipRole):
            if item.already_added:
                return "這名角色已存在於目前資料, 不會重複建立。"
            if item.source_status != "official_current_character":
                return "公開索引只確認名稱; 未知欄位不會被猜測。"
        return None

    def setData(self, index: ModelIndex, value: object, role: int = 0) -> bool:
        if (
            not index.isValid()
            or role != int(Qt.ItemDataRole.CheckStateRole)
            or index.column() != 0
            or not 0 <= index.row() < len(self._rows)
        ):
            return False
        item = self._rows[index.row()]
        if item.already_added:
            return False
        checked = value == Qt.CheckState.Checked or value == 2
        if checked:
            self._selected.add(item.identifier)
        else:
            self._selected.discard(item.identifier)
        self.dataChanged.emit(index, index, [int(Qt.ItemDataRole.CheckStateRole)])
        return True

    def flags(self, index: ModelIndex) -> Qt.ItemFlag:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return Qt.ItemFlag.NoItemFlags
        item = self._rows[index.row()]
        flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
        if index.column() == 0 and not item.already_added:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

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

    def set_rows(self, rows: tuple[CharacterCatalogItemView, ...]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        valid = {row.identifier for row in rows if not row.already_added}
        self._selected.intersection_update(valid)
        self.endResetModel()

    def selected_ids(self) -> tuple[str, ...]:
        return tuple(row.identifier for row in self._rows if row.identifier in self._selected)

    def set_all_filtered(self, identifiers: set[str], checked: bool) -> None:
        selectable = {
            row.identifier
            for row in self._rows
            if row.identifier in identifiers and not row.already_added
        }
        if checked:
            self._selected.update(selectable)
        else:
            self._selected.difference_update(selectable)
        if self._rows:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self._rows) - 1, 0),
                [int(Qt.ItemDataRole.CheckStateRole)],
            )


class CharacterCatalogDialog(QDialog):
    """Database-free dialog that emits only stable catalog identifiers."""

    import_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("CharacterCatalogDialog")
        self.setWindowTitle("從角色目錄加入")
        self.setModal(False)
        self.resize(900, 650)

        title = QLabel("一次勾選會上場的角色")
        title.setObjectName("DialogTitle")
        description = QLabel(
            "只建立角色名稱與來源可確認的基本欄位; 等級、共鳴鏈、技能與個人優先度可之後再編輯。"
        )
        description.setObjectName("PageDescription")
        description.setWordWrap(True)
        self.version_label = QLabel("角色目錄尚未載入")
        self.version_label.setObjectName("CharacterCatalogVersion")
        self.version_label.setWordWrap(True)

        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("CharacterCatalogSearch")
        self.search_edit.setPlaceholderText("搜尋角色名稱、屬性或武器…")

        self.model = CharacterCatalogTableModel(self)
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.proxy.setFilterKeyColumn(-1)
        self.table = QTableView()
        self.table.setObjectName("CharacterCatalogTable")
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)

        self.select_visible_button = QPushButton("勾選目前搜尋結果")
        self.select_visible_button.setObjectName("CharacterCatalogSelectVisible")
        self.clear_button = QPushButton("清除勾選")
        self.clear_button.setObjectName("CharacterCatalogClearSelection")
        self.status_label = QLabel("請勾選你實際可能上場的角色。")
        self.status_label.setObjectName("CharacterCatalogStatus")
        self.status_label.setWordWrap(True)
        self.cancel_button = QPushButton("關閉")
        self.cancel_button.setObjectName("CharacterCatalogCancel")
        self.import_button = QPushButton("加入勾選角色")
        self.import_button.setObjectName("CharacterCatalogImport")
        self.import_button.setProperty("role", "primary")

        tools = QHBoxLayout()
        tools.addWidget(self.select_visible_button)
        tools.addWidget(self.clear_button)
        tools.addStretch(1)
        actions = QHBoxLayout()
        actions.addWidget(self.status_label, 1)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.import_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(title)
        layout.addWidget(description)
        layout.addWidget(self.version_label)
        layout.addWidget(self.search_edit)
        layout.addLayout(tools)
        layout.addWidget(self.table, 1)
        layout.addLayout(actions)

        self.search_edit.textChanged.connect(self.proxy.setFilterFixedString)
        self.select_visible_button.clicked.connect(self._select_visible)
        self.clear_button.clicked.connect(self._clear_selection)
        self.cancel_button.clicked.connect(self.close)
        self.import_button.clicked.connect(self._request_import)

    def set_catalog(
        self,
        rows: tuple[CharacterCatalogItemView, ...],
        *,
        version: str,
        as_of_date: str,
        disclaimer: str,
    ) -> None:
        self.model.set_rows(rows)
        self.version_label.setText(f"內建目錄 {version} · 資料日期 {as_of_date}\n{disclaimer}")
        self.set_loading(False)

    def selected_entry_ids(self) -> tuple[str, ...]:
        return self.model.selected_ids()

    @Slot(bool)
    def set_loading(self, loading: bool) -> None:
        self.search_edit.setEnabled(not loading)
        self.table.setEnabled(not loading)
        self.select_visible_button.setEnabled(not loading)
        self.clear_button.setEnabled(not loading)
        self.import_button.setEnabled(not loading)
        if loading:
            self.status_label.setProperty("error", False)
            self.status_label.setText("正在建立角色資料…")
            _repolish(self.status_label)

    @Slot(str)
    def show_error(self, message: str) -> None:
        self.set_loading(False)
        self.status_label.setProperty("error", True)
        self.status_label.setText(f"無法加入角色: {message}")
        _repolish(self.status_label)

    def show_result(self, created_names: tuple[str, ...], skipped_names: tuple[str, ...]) -> None:
        self.set_loading(False)
        self.status_label.setProperty("error", False)
        if created_names:
            message = f"已加入 {len(created_names)} 名: {'、'.join(created_names)}。"
        else:
            message = "沒有建立新角色。"
        if skipped_names:
            message += f" 已存在而略過: {'、'.join(skipped_names)}。"
        self.status_label.setText(message)
        _repolish(self.status_label)

    @Slot()
    def _select_visible(self) -> None:
        identifiers = {
            str(self.proxy.data(self.proxy.index(row, 0), int(Qt.ItemDataRole.UserRole)))
            for row in range(self.proxy.rowCount())
        }
        self.model.set_all_filtered(identifiers, True)

    @Slot()
    def _clear_selection(self) -> None:
        self.model.set_all_filtered(set(self.model.selected_ids()), False)

    @Slot()
    def _request_import(self) -> None:
        selected = self.selected_entry_ids()
        if not selected:
            self.status_label.setProperty("error", True)
            self.status_label.setText("請先勾選至少一名角色。")
            _repolish(self.status_label)
            return
        self.set_loading(True)
        self.import_requested.emit(CharacterCatalogSelectionRequest(selected))


def _repolish(widget: QWidget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
