"""Short, grouped farming reasons with technical diagnostics kept secondary."""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, QRect, QSize, Qt
from PySide6.QtWidgets import (
    QDialog,
    QHeaderView,
    QLabel,
    QPushButton,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from wuwa_assistant.planning.farming import ZoneRecommendation

from .text_details import TextDetailsButton

_ROLES = {"主輸出": "主C", "副輸出": "副C", "治療輔助": "治療"}


def reason_rows(zone: ZoneRecommendation) -> tuple[tuple[str, str, str, str], ...]:
    rows: dict[tuple[str, int], tuple[str, str, str, str]] = {}
    for benefit in sorted(zone.benefits, key=lambda row: -row.points):
        key = (benefit.character_id, benefit.slot_index)
        role = benefit.role or benefit.reason.split(" · ", 1)[0]
        reason = benefit.reason.split(": ", 1)[-1]
        if key not in rows:
            rows[key] = (
                benefit.character_name,
                _ROLES.get(role, role),
                f"第{benefit.slot_index}件",
                reason,
            )
        elif reason not in rows[key][3]:
            old = rows[key]
            rows[key] = (*old[:3], old[3] + "; " + reason)
    return tuple(rows.values())


class _WrappedReasons(QStyledItemDelegate):
    def __init__(self, tree: QTreeWidget) -> None:
        super().__init__(tree)
        self._tree = tree

    def sizeHint(
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QSize:
        base = super().sizeHint(option, index)
        if not index.parent().isValid():
            return QSize(base.width(), max(32, base.height()))
        model = index.model()
        if model is None:
            return base
        height = 0
        for column in range(4):
            value = model.index(index.row(), column, index.parent()).data()
            width = max(24, self._tree.columnWidth(column) - (36 if column == 0 else 12))
            box = option.fontMetrics.boundingRect(
                QRect(0, 0, width, 100000), int(Qt.TextFlag.TextWordWrap), str(value or "")
            )
            height = max(height, box.height())
        return QSize(base.width(), height + 16)


class FarmingReasonsButton(TextDetailsButton):
    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        # Keep the same button API and diagnostic text for existing integrations.
        super().__init__(label, parent)
        self._recommendations: tuple[ZoneRecommendation, ...] = ()

    def set_recommendations(self, rows: tuple[ZoneRecommendation, ...]) -> None:
        self._recommendations = rows

    def _show_details(self) -> None:
        dialog = QDialog(self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setObjectName("FarmingReasonsDialog")
        dialog.setWindowTitle("無音區推薦理由")
        available = dialog.screen().availableGeometry()
        dialog.resize(min(1000, available.width() - 60), min(680, available.height() - 80))
        layout = QVBoxLayout(dialog)
        hint = QLabel(
            "依無音區分組。先修正配裝問題; 分數差距依各角色門檻顯示。展開可查看其他區域。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        tree = QTreeWidget(dialog)
        tree.setObjectName("FarmingReasonRows")
        tree.setHeaderLabels(("角色", "定位", "位置", "原因"))
        tree.setWordWrap(True)
        tree.setUniformRowHeights(False)
        tree.setItemDelegate(_WrappedReasons(tree))
        tree.header().setStretchLastSection(True)
        tree.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in enumerate((140, 70, 75)):
            tree.setColumnWidth(column, width)
        tree.header().sectionResized.connect(lambda: tree.doItemsLayout())
        count = 0
        for zone in self._recommendations:
            if zone.points <= 0:
                continue
            status = "先培養現有庫存" if zone.has_unleveled_stock else "需要刷取"
            group = QTreeWidgetItem(tree, [f"{zone.zone.name} · {status}"])
            group.setFirstColumnSpanned(True)
            for values in reason_rows(zone):
                QTreeWidgetItem(group, list(values))
            group.setExpanded(count == 0)
            count += 1
        if not count:
            layout.addWidget(QLabel("目前沒有已確認的刷取缺口; 資料待確認項目可查看計算細節。"))
        layout.addWidget(tree, 1)
        diagnostics = TextDetailsButton("計算細節與資料待確認項目", dialog)
        diagnostics.set_details("計算細節", self._details_text)
        layout.addWidget(diagnostics)
        close = QPushButton("關閉", dialog)
        close.clicked.connect(dialog.close)
        layout.addWidget(close)
        dialog.show()
