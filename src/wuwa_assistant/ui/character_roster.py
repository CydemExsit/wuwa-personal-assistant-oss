"""Portrait row delegate for the compact character editor roster."""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, QRect, QSize, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem, QWidget

from .character_portraits import character_portrait


class CharacterRosterDelegate(QStyledItemDelegate):
    def __init__(self, parent: QWidget | None = None, *, portrait_tile: bool = False) -> None:
        super().__init__(parent)
        self._portrait_tile = portrait_tile

    def sizeHint(
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QSize:
        if self._portrait_tile:
            return QSize(88, 104)
        return super().sizeHint(option, index)

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        name = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        rect = option.rect.adjusted(4, 4, -4, -4)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#55305f" if selected else "#3b294c"))
        painter.drawRoundedRect(rect, 9, 9)
        clean_name = name.removeprefix("已封存 · ")
        device = painter.device()
        pixel_ratio = device.devicePixelRatioF() if device is not None else 1.0
        portrait_size = 60 if self._portrait_tile else 48
        painter.drawPixmap(
            rect.x() + (rect.width() - portrait_size) // 2 if self._portrait_tile else rect.x() + 9,
            rect.y() + 9,
            character_portrait(clean_name, portrait_size, device_pixel_ratio=pixel_ratio),
        )
        painter.setPen(QColor("#f5edf8"))
        if self._portrait_tile:
            title_rect = QRect(rect.x() + 3, rect.y() + 72, rect.width() - 6, 21)
            painter.drawText(
                title_rect,
                Qt.AlignmentFlag.AlignCenter,
                painter.fontMetrics().elidedText(
                    clean_name, Qt.TextElideMode.ElideRight, title_rect.width()
                ),
            )
            painter.restore()
            return
        title_rect = QRect(rect.x() + 68, rect.y() + 10, rect.width() - 76, 24)
        painter.drawText(
            title_rect,
            Qt.AlignmentFlag.AlignVCenter,
            painter.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, title_rect.width()),
        )
        element = index.sibling(index.row(), 1).data() or "屬性待設定"
        painter.setPen(QColor("#cbb7d8"))
        painter.drawText(
            QRect(rect.x() + 68, rect.y() + 34, rect.width() - 76, 22),
            Qt.AlignmentFlag.AlignVCenter,
            str(element),
        )
        painter.restore()
