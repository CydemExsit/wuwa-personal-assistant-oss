"""Navigation identifiers and the Qt list model used by the main window."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from PySide6.QtCore import (
    QAbstractListModel,
    QByteArray,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    Qt,
)


class PageId(StrEnum):
    DASHBOARD = "dashboard"
    TODAY_PLAN = "today_plan"
    CHARACTERS = "characters"
    EQUIPMENT = "equipment"
    ECHO_ENHANCEMENT = "echo_enhancement"
    GACHA = "gacha"
    TASKS_EVENTS = "tasks_events"
    REDEEM_CODES = "redeem_codes"
    HISTORY = "history"
    SETTINGS = "settings"


@dataclass(frozen=True, slots=True)
class NavigationItem:
    page_id: PageId
    label: str


NAVIGATION_ITEMS: tuple[NavigationItem, ...] = (
    NavigationItem(PageId.DASHBOARD, "總覽"),
    NavigationItem(PageId.TODAY_PLAN, "養成規劃"),
    NavigationItem(PageId.CHARACTERS, "角色"),
    NavigationItem(PageId.EQUIPMENT, "聲骸"),
    NavigationItem(PageId.ECHO_ENHANCEMENT, "聲骸強化"),
    NavigationItem(PageId.GACHA, "抽卡規劃"),
    NavigationItem(PageId.TASKS_EVENTS, "活動與待辦"),
    NavigationItem(PageId.REDEEM_CODES, "兌換碼"),
    NavigationItem(PageId.SETTINGS, "設定"),
)

PAGE_ID_ROLE = int(Qt.ItemDataRole.UserRole) + 1
_INVALID_INDEX = QModelIndex()
type ModelIndex = QModelIndex | QPersistentModelIndex


class NavigationModel(QAbstractListModel):
    """Stable, data-driven list of all V1 desktop pages."""

    def __init__(
        self,
        items: tuple[NavigationItem, ...] = NAVIGATION_ITEMS,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._items = items

    def rowCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._items)

    def data(
        self,
        index: ModelIndex,
        role: int = int(Qt.ItemDataRole.DisplayRole),
    ) -> object | None:
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        item = self._items[index.row()]
        if role == int(Qt.ItemDataRole.DisplayRole):
            return item.label
        if role == PAGE_ID_ROLE:
            return item.page_id.value
        if role == int(Qt.ItemDataRole.ToolTipRole):
            return item.label
        return None

    def roleNames(self) -> dict[int, QByteArray]:
        roles = super().roleNames()
        roles[PAGE_ID_ROLE] = QByteArray(b"pageId")
        return roles

    def page_id_at(self, index: ModelIndex) -> PageId | None:
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        return self._items[index.row()].page_id

    def index_for(self, page_id: PageId) -> QModelIndex:
        for row, item in enumerate(self._items):
            if item.page_id is page_id:
                return self.index(row, 0)
        return QModelIndex()

    def labels(self) -> tuple[str, ...]:
        return tuple(item.label for item in self._items)
