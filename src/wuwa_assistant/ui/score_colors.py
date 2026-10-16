"""One continuous, readable color scale for Echo scores across desktop views."""

from __future__ import annotations

from html import escape
from itertools import pairwise
from math import isfinite

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)

# Warm low scores move through green and cyan to violet high scores. Pastel
# anchors retain contrast on both ordinary and selected dark-purple cards.
_STOPS = (
    (0.0, "#ffaeae"),
    (40.0, "#ffb875"),
    (55.0, "#f4db75"),
    (65.0, "#c3e681"),
    (75.0, "#80e0ac"),
    (85.0, "#7cdded"),
    (92.0, "#a7c5ff"),
    (100.0, "#d7b5ff"),
)
_UNKNOWN = "#bfb9c6"


def score_color(score: float | None, *, maximum: float = 100.0) -> QColor:
    """Interpolate by numeric score; fit/pass flags do not alter its color."""
    if not isfinite(maximum) or maximum <= 0:
        raise ValueError("maximum must be positive and finite")
    if score is None or not isfinite(score):
        return QColor(_UNKNOWN)
    value = min(100.0, max(0.0, score / maximum * 100.0))
    for (low, start), (high, end) in pairwise(_STOPS):
        if value <= high:
            fraction = (value - low) / (high - low)
            first, last = QColor(start), QColor(end)
            return QColor.fromRgbF(
                first.redF() + (last.redF() - first.redF()) * fraction,
                first.greenF() + (last.greenF() - first.greenF()) * fraction,
                first.blueF() + (last.blueF() - first.blueF()) * fraction,
            )
    return QColor(_STOPS[-1][1])


def apply_score_color(widget: QWidget, score: float | None, *, maximum: float = 100.0) -> None:
    """Use a local foreground rule so broad theme selectors cannot mask scores."""
    color = score_color(score, maximum=maximum).name()
    # Preserve font, border, padding and button selection styles from the theme.
    selector = f"#{widget.objectName()}" if widget.objectName() else "*"
    background = " background-color: transparent;" if isinstance(widget, QLabel) else ""
    rule = f"\n{selector} {{ color: {color};{background} }}"
    base = widget.styleSheet()
    previous = widget.property("scoreColorRule")
    if isinstance(previous, str) and base.endswith(previous):
        base = base[: -len(previous)]
    stylesheet = base + rule
    if widget.styleSheet() != stylesheet:
        widget.setStyleSheet(stylesheet)
    widget.setProperty("scoreColorRule", rule)


def score_span(text: str, score: float | None) -> str:
    """Color individual absolute scores in labels with multiple numeric values."""
    return f'<span style="color: {score_color(score).name()}">{escape(text)}</span>'


class ScoreColorDelegate(QStyledItemDelegate):
    """Keep numeric colors when a table's selection theme overrides text roles."""

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        color = index.data(Qt.ItemDataRole.ForegroundRole)
        if not isinstance(color, QColor):
            super().paint(painter, option, index)
            return
        styled = QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        style = styled.widget.style() if styled.widget is not None else QApplication.style()
        rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, styled, styled.widget)
        text = styled.fontMetrics.elidedText(styled.text, styled.textElideMode, rect.width())
        styled.text = ""
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, styled, painter, styled.widget)
        painter.save()
        painter.setFont(styled.font)
        painter.setPen(color)
        painter.drawText(rect, styled.displayAlignment | Qt.AlignmentFlag.AlignVCenter, text)
        painter.restore()
