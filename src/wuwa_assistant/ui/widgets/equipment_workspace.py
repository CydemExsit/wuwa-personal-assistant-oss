"""Responsive equipment browser that preserves logical overview/detail routes."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QResizeEvent, QShowEvent
from PySide6.QtWidgets import QSplitter, QVBoxLayout, QWidget

from wuwa_assistant.ui.pages.equipment_detail import EquipmentDetailPage
from wuwa_assistant.ui.pages.equipment_overview import EquipmentOverviewPage


class EquipmentWorkspace(QWidget):
    """Keep both views mounted; a compact window shows the current route only."""

    def __init__(
        self,
        overview: EquipmentOverviewPage,
        detail: EquipmentDetailPage,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("EquipmentWorkspace")
        self.overview = overview
        self.detail = detail
        self._index = 0
        self._side_by_side = False
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setObjectName("EquipmentBrowserSplitter")
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(12)
        self.splitter.addWidget(overview)
        self.splitter.addWidget(detail)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.splitter)
        self._arrange()

    @property
    def side_by_side(self) -> bool:
        return self._side_by_side

    def currentIndex(self) -> int:
        """Return the navigation route independently of the physical layout."""
        return self._index

    def currentWidget(self) -> QWidget:
        return self.overview if self._index == 0 else self.detail

    def setCurrentIndex(self, index: int) -> None:
        if index not in (0, 1):
            return
        self._index = index
        self._arrange()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._arrange()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._arrange()

    def _arrange(self) -> None:
        wide = self.contentsRect().width() >= 1120
        changed = wide != self._side_by_side
        self._side_by_side = wide
        self.overview.setVisible(wide or self._index == 0)
        self.detail.setVisible(wide or self._index == 1)
        self.detail.set_compact_inspector(wide)
        if wide and changed:
            available = max(1, self.contentsRect().width() - 12)
            detail_width = max(430, min(580, int(available * 0.36)))
            self.splitter.setSizes([available - detail_width, detail_width])
