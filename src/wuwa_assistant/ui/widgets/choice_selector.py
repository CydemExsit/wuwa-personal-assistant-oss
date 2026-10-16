"""Keep Qt's combo data/signals, expose two non-editable choices directly."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPaintEvent, QResizeEvent, QWheelEvent
from PySide6.QtWidgets import QButtonGroup, QComboBox, QPushButton, QWidget


class ChoiceSelector(QComboBox):
    """A compatible selector that becomes two keyboard-accessible buttons at count 2."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._segments = (QPushButton(self), QPushButton(self))
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for index, button in enumerate(self._segments):
            button.setCheckable(True)
            button.setObjectName("ChoiceSegment")
            self._group.addButton(button, index)
            button.clicked.connect(lambda _checked=False, i=index: self.setCurrentIndex(i))
        self.currentIndexChanged.connect(self._sync)
        self.model().rowsInserted.connect(self._sync)
        self.model().rowsRemoved.connect(self._sync)
        self.model().modelReset.connect(self._sync)
        self.model().dataChanged.connect(self._sync)
        self._sync()

    def _segmented(self) -> bool:
        return self.count() == 2 and not self.isEditable()

    def _sync(self, *_args: object) -> None:
        if not hasattr(self, "_segments"):
            return
        active = self._segmented()
        for index, button in enumerate(self._segments):
            button.setVisible(active)
            button.setText(self.itemText(index))
            button.setChecked(self.currentIndex() == index)
        self._position()
        self.updateGeometry()
        self.update()

    def _position(self) -> None:
        if not hasattr(self, "_segments"):
            return
        half = self.width() // 2
        self._segments[0].setGeometry(0, 0, half, self.height())
        self._segments[1].setGeometry(half, 0, self.width() - half, self.height())
        for index, button in enumerate(self._segments):
            label = self.itemText(index)
            button.setText(
                button.fontMetrics().elidedText(
                    label, Qt.TextElideMode.ElideRight, max(20, button.width() - 20)
                )
            )
            button.setToolTip(label)

    def setCurrentIndex(self, index: int) -> None:
        super().setCurrentIndex(index)
        self._sync()

    def setEditable(self, editable: bool) -> None:
        super().setEditable(editable)
        self._sync()

    def sizeHint(self) -> QSize:
        if self._segmented():
            metrics = self.fontMetrics()
            width = 2 * (
                min(160, max(metrics.horizontalAdvance(self.itemText(i)) for i in (0, 1))) + 36
            )
            return QSize(width, max(38, super().sizeHint().height()))
        return super().sizeHint()

    def minimumSizeHint(self) -> QSize:
        return (
            QSize(120, self.sizeHint().height()) if self._segmented() else super().minimumSizeHint()
        )

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._position()

    def paintEvent(self, event: QPaintEvent) -> None:
        if not self._segmented():
            super().paintEvent(event)

    def showPopup(self) -> None:
        if not self._segmented():
            super().showPopup()

    def wheelEvent(self, event: QWheelEvent) -> None:
        event.ignore()
