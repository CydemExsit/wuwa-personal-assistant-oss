"""Model/view widgets for persisted echo and capture-session history."""

from __future__ import annotations

from collections.abc import Sequence
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
    QCheckBox,
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
    QVBoxLayout,
    QWidget,
)

from wuwa_assistant.ui.score_colors import ScoreColorDelegate, score_color

_INVALID_INDEX = QModelIndex()
type ModelIndex = QModelIndex | QPersistentModelIndex


@dataclass(frozen=True, slots=True)
class HistorySessionRow:
    session_id: str
    started_at: str
    ended_at: str | None
    echo_name: str | None
    final_level: int | None
    disposition: str
    final_score: float | None = None
    character_name: str | None = None
    capture_run: str = "未分組"
    scan_count: int = 1

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise ValueError("session_id cannot be empty")
        if not self.capture_run.strip():
            raise ValueError("capture_run cannot be empty")
        if self.final_level is not None and not 0 <= self.final_level <= 25:
            raise ValueError("final_level must be within 0..25 or null")
        if self.final_score is not None and not 0 <= self.final_score <= 100:
            raise ValueError("final_score must be within 0..100 or null")


@dataclass(frozen=True, slots=True)
class HistoryObservationRow:
    captured_at: str
    level: int | None
    summary: str
    confidence: float | None
    corrected: bool = False

    def __post_init__(self) -> None:
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be within 0..1 or null")


@dataclass(frozen=True, slots=True)
class HistoryEventRow:
    occurred_at: str
    event_label: str
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.event_label.strip():
            raise ValueError("event_label cannot be empty")


@dataclass(frozen=True, slots=True)
class HistorySessionDetail:
    session_id: str
    echo_name: str | None
    set_name: str | None
    final_level: int | None
    final_disposition: str
    assigned_character: str | None = None
    observations: tuple[HistoryObservationRow, ...] = ()
    events: tuple[HistoryEventRow, ...] = ()

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise ValueError("session_id cannot be empty")


class HistorySessionTableModel(QAbstractTableModel):
    """Read-only session history model, suitable for sorting through a proxy."""

    _HEADERS = (
        "辨識批次",
        "開始時間",
        "聲骸",
        "最終等級",
        "結果",
        "分數",
        "角色",
        "結束時間",
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[HistorySessionRow] = []

    def rowCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._HEADERS)

    def data(self, index: ModelIndex, role: int = int(Qt.ItemDataRole.DisplayRole)) -> object:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        row = self._rows[index.row()]
        if role == int(Qt.ItemDataRole.ForegroundRole) and index.column() == 5:
            return score_color(row.final_score)
        if role == int(Qt.ItemDataRole.UserRole):
            return row.session_id
        if role == int(Qt.ItemDataRole.DisplayRole):
            values = (
                row.capture_run,
                row.started_at,
                (row.echo_name or "需要人工確認")
                + (f" · 掃描 {row.scan_count} 次" if row.scan_count > 1 else ""),
                f"+{row.final_level}" if row.final_level is not None else "—",
                row.disposition,
                f"{row.final_score:.1f}" if row.final_score is not None else "—",
                row.character_name or "未指定",
                row.ended_at or "進行中",
            )
            return values[index.column()]
        if role == int(Qt.ItemDataRole.EditRole):
            sort_values = (
                row.capture_run,
                row.started_at,
                row.echo_name or "",
                row.final_level if row.final_level is not None else -1,
                row.disposition,
                row.final_score if row.final_score is not None else -1.0,
                row.character_name or "",
                row.ended_at or "",
            )
            return sort_values[index.column()]
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

    def set_rows(self, rows: Sequence[HistorySessionRow]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def session_id_at(self, row: int) -> str | None:
        return self._rows[row].session_id if 0 <= row < len(self._rows) else None

    def row_for_session(self, session_id: str) -> int | None:
        for index, row in enumerate(self._rows):
            if row.session_id == session_id:
                return index
        return None


class _RowsTableModel(QAbstractTableModel):
    def __init__(self, headers: Sequence[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._headers = tuple(headers)
        self._rows: list[tuple[str, ...]] = []

    def rowCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._headers)

    def data(self, index: ModelIndex, role: int = int(Qt.ItemDataRole.DisplayRole)) -> object:
        if (
            role == int(Qt.ItemDataRole.DisplayRole)
            and index.isValid()
            and 0 <= index.row() < len(self._rows)
        ):
            return self._rows[index.row()][index.column()]
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
            and 0 <= section < len(self._headers)
        ):
            return self._headers[section]
        return None

    def set_rows(self, rows: Sequence[tuple[str, ...]]) -> None:
        if any(len(row) != len(self._headers) for row in rows):
            raise ValueError("row length must match table headers")
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()


class EchoHistoryPage(QWidget):
    """History browser whose data source is provided by an external service."""

    refresh_requested = Signal()
    session_selected = Signal(str)
    grouping_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("EchoHistoryPage")
        title = QLabel("歷史紀錄")
        title.setObjectName("PageTitle")
        subtitle = QLabel("重複掃描可折疊查看; 取消勾選即可查閱每次掃描與修正紀錄。")
        subtitle.setObjectName("PageDescription")

        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("HistorySearchEdit")
        self.search_edit.setPlaceholderText("搜尋聲骸、角色或結果")
        self.refresh_button = QPushButton("重新整理")
        self.refresh_button.setObjectName("HistoryRefreshButton")
        self.refresh_button.clicked.connect(self._request_refresh)
        self.status_label = QLabel("尚未載入歷史紀錄。")
        self.status_label.setObjectName("HistoryLoadStatus")

        toolbar = QHBoxLayout()
        self.group_duplicates_check = QCheckBox("折疊重複掃描")
        self.group_duplicates_check.setChecked(True)
        self.group_duplicates_check.toggled.connect(self.grouping_changed)
        toolbar.addWidget(self.search_edit, 1)
        toolbar.addWidget(self.group_duplicates_check)
        toolbar.addWidget(self.refresh_button)
        toolbar.addWidget(self.status_label)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setObjectName("HistorySplitter")
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_session_table())
        splitter.addWidget(self._build_detail_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 30, 40, 24)
        layout.setSpacing(12)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addLayout(toolbar)
        layout.addWidget(splitter, 1)

        self.search_edit.textChanged.connect(self.proxy_model.setFilterFixedString)

    def _build_session_table(self) -> QWidget:
        container = QGroupBox("聲骸 Sessions")
        self.session_model = HistorySessionTableModel(self)
        self.proxy_model = QSortFilterProxyModel(self)
        self.proxy_model.setSourceModel(self.session_model)
        self.proxy_model.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.proxy_model.setFilterKeyColumn(-1)
        self.proxy_model.setSortRole(int(Qt.ItemDataRole.EditRole))
        self.session_table = QTableView()
        self.session_table.setObjectName("HistorySessionTable")
        self.session_table.setModel(self.proxy_model)
        self.session_table.setItemDelegateForColumn(5, ScoreColorDelegate(self.session_table))
        self.session_table.setSortingEnabled(True)
        self.session_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.session_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.session_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.session_table.horizontalHeader().setStretchLastSection(True)
        selection_model = self.session_table.selectionModel()
        if selection_model is not None:
            selection_model.currentRowChanged.connect(self._on_current_row_changed)
        layout = QVBoxLayout(container)
        layout.addWidget(self.session_table)
        return container

    def _build_detail_panel(self) -> QWidget:
        group = QGroupBox("Session 詳細資料")
        self.detail_session_label = QLabel("—")
        self.detail_session_label.setObjectName("HistoryDetailSessionId")
        self.detail_name_label = QLabel("—")
        self.detail_name_label.setObjectName("HistoryDetailEchoName")
        self.detail_set_label = QLabel("—")
        self.detail_set_label.setObjectName("HistoryDetailSetName")
        self.detail_level_label = QLabel("—")
        self.detail_level_label.setObjectName("HistoryDetailLevel")
        self.detail_disposition_label = QLabel("—")
        self.detail_disposition_label.setObjectName("HistoryDetailDisposition")
        self.detail_character_label = QLabel("—")
        self.detail_character_label.setObjectName("HistoryDetailCharacter")

        form = QFormLayout()
        form.addRow("Session", self.detail_session_label)
        form.addRow("聲骸", self.detail_name_label)
        form.addRow("套裝", self.detail_set_label)
        form.addRow("最終等級", self.detail_level_label)
        form.addRow("最終結果", self.detail_disposition_label)
        form.addRow("角色", self.detail_character_label)

        self.observation_model = _RowsTableModel(("時間", "等級", "辨識內容", "信心", "修正"), self)
        self.observation_table = _detail_table("HistoryObservationTable", self.observation_model)
        self.event_model = _RowsTableModel(("時間", "事件", "備註"), self)
        self.event_table = _detail_table("HistoryEventTable", self.event_model)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 10, 0)
        content_layout.addLayout(form)
        content_layout.addWidget(QLabel("辨識紀錄 (修正前後值保留於 audit log)"))
        content_layout.addWidget(self.observation_table)
        content_layout.addWidget(QLabel("事件時間線"))
        content_layout.addWidget(self.event_table)

        scroll = QScrollArea()
        scroll.setObjectName("HistoryDetailScrollArea")
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        layout = QVBoxLayout(group)
        layout.addWidget(scroll)
        return group

    @Slot(object)
    def set_sessions(self, rows: object) -> None:
        if not isinstance(rows, (list, tuple)) or not all(
            isinstance(item, HistorySessionRow) for item in rows
        ):
            raise TypeError("rows must be a list or tuple of HistorySessionRow")
        selected_id = self._selected_session_id()
        self.session_model.set_rows(rows)
        self.status_label.setProperty("error", False)
        run_count = len({row.capture_run for row in rows})
        scans = sum(row.scan_count for row in rows)
        self.status_label.setText(
            f"{scans} 筆掃描 · {len(rows)} 組聲骸"
            if self.group_duplicates_check.isChecked()
            else f"共 {len(rows)} 筆掃描, 分為 {run_count} 次辨識。"
        )
        self.refresh_button.setEnabled(True)
        if selected_id is not None:
            self.select_session(selected_id)

    @Slot(object)
    def set_detail(self, detail: object) -> None:
        if not isinstance(detail, HistorySessionDetail):
            raise TypeError("detail must be HistorySessionDetail")
        self.detail_session_label.setText(detail.session_id)
        self.detail_name_label.setText(detail.echo_name or "需要人工確認")
        self.detail_set_label.setText(detail.set_name or "—")
        self.detail_level_label.setText(
            f"+{detail.final_level}" if detail.final_level is not None else "—"
        )
        self.detail_disposition_label.setText(detail.final_disposition)
        self.detail_character_label.setText(detail.assigned_character or "未指定")
        self.observation_model.set_rows(
            [
                (
                    row.captured_at,
                    f"+{row.level}" if row.level is not None else "—",
                    row.summary,
                    "—" if row.confidence is None else f"{row.confidence:.0%}",
                    "是" if row.corrected else "否",
                )
                for row in detail.observations
            ]
        )
        self.event_model.set_rows(
            [(row.occurred_at, row.event_label, row.notes) for row in detail.events]
        )

    @Slot(bool, str)
    def set_loading(self, loading: bool, message: str = "正在讀取本機紀錄…") -> None:
        self.refresh_button.setEnabled(not loading)
        if loading:
            self.status_label.setProperty("error", False)
            self.status_label.setText(message)

    @Slot(str)
    def show_error(self, message: str) -> None:
        self.refresh_button.setEnabled(True)
        self.status_label.setProperty("error", True)
        self.status_label.setText(f"無法載入歷史紀錄: {message}")
        _repolish(self.status_label)

    def select_session(self, session_id: str) -> bool:
        source_row = self.session_model.row_for_session(session_id)
        if source_row is None:
            return False
        source_index = self.session_model.index(source_row, 0)
        proxy_index = self.proxy_model.mapFromSource(source_index)
        if not proxy_index.isValid():
            return False
        self.session_table.setCurrentIndex(proxy_index)
        self.session_table.scrollTo(proxy_index)
        return True

    def _request_refresh(self) -> None:
        self.status_label.setProperty("error", False)
        self.status_label.setText("正在讀取本機紀錄…")
        self.refresh_button.setEnabled(False)
        self.refresh_requested.emit()

    def _on_current_row_changed(self, current: QModelIndex, previous: QModelIndex) -> None:
        del previous
        if not current.isValid():
            return
        source_index = self.proxy_model.mapToSource(current)
        session_id = self.session_model.session_id_at(source_index.row())
        if session_id is not None:
            self.session_selected.emit(session_id)

    def _selected_session_id(self) -> str | None:
        current = self.session_table.currentIndex()
        if not current.isValid():
            return None
        source = self.proxy_model.mapToSource(current)
        return self.session_model.session_id_at(source.row())


def _detail_table(object_name: str, model: QAbstractTableModel) -> QTableView:
    table = QTableView()
    table.setObjectName(object_name)
    table.setModel(model)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.horizontalHeader().setStretchLastSection(True)
    table.setMinimumHeight(120)
    return table


def _repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


__all__ = [
    "EchoHistoryPage",
    "HistoryEventRow",
    "HistoryObservationRow",
    "HistorySessionDetail",
    "HistorySessionRow",
    "HistorySessionTableModel",
]
