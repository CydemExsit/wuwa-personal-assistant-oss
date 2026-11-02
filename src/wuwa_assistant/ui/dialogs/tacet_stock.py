"""Compact, shared manual availability editor (never individual Echo counts)."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from wuwa_assistant.application.tacet_catalog import TacetCatalog
from wuwa_assistant.application.tacet_stock import TacetStockEditRequest


class _StockTable(QTableWidget):
    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.resizeRowsToContents()


class TacetStockDialog(QDialog):
    edit_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("TacetStockDialog")
        self.setWindowTitle("無音區庫存")
        available = self.screen().availableGeometry()
        self.resize(min(860, available.width() - 40), min(600, available.height() - 60))
        self._field_ids: tuple[str, ...] = ()
        self._busy = False
        layout = QVBoxLayout(self)
        hint = QLabel("手動標記各無音區需要的聲骸是否還有可培養; 用於刷取順位, 不是單顆聲骸數量。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.table = _StockTable(0, 3)
        self.table.setHorizontalHeaderLabels(("無音區", "套裝", "庫存狀態 (點擊切換)"))
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(True)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, 1)
        bulk = QHBoxLayout()
        self.all_available = QPushButton("全部還有可培養")
        self.all_exhausted = QPushButton("全部需要的已用完")
        self.all_available.clicked.connect(lambda: self._edit(self._field_ids, True))
        self.all_exhausted.clicked.connect(lambda: self._edit(self._field_ids, False))
        bulk.addWidget(self.all_available)
        bulk.addWidget(self.all_exhausted)
        layout.addLayout(bulk)
        self.status = QLabel("正在載入…")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        close = QPushButton("關閉")
        close.clicked.connect(self.close)
        layout.addWidget(close)

    def set_stock(self, catalog: TacetCatalog, stocked: frozenset[str]) -> None:
        fields = tuple(f for f in catalog.fields if f.verified)
        self._field_ids = tuple(f.field_id for f in fields)
        self.table.setRowCount(len(fields))
        for row, field in enumerate(fields):
            self.table.setItem(row, 0, QTableWidgetItem(field.name))
            self.table.setItem(row, 1, QTableWidgetItem(" / ".join(field.set_names)))
            available = field.field_id in stocked
            button = QPushButton("還有可培養" if available else "需要的已用完")
            button.setObjectName(f"TacetStock_{field.field_id}")
            button.clicked.connect(
                lambda _checked=False, identifier=field.field_id, value=not available: self._edit(
                    (identifier,), value
                )
            )
            self.table.setCellWidget(row, 2, button)
        self.table.resizeRowsToContents()
        self.set_busy(self._busy)

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.table.setEnabled(not busy)
        self.all_available.setEnabled(not busy and bool(self._field_ids))
        self.all_exhausted.setEnabled(not busy and bool(self._field_ids))

    def _edit(self, field_ids: tuple[str, ...], has_stock: bool) -> None:
        if not self._busy and field_ids:
            self.edit_requested.emit(TacetStockEditRequest(field_ids, has_stock))
