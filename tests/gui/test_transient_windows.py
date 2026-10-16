from __future__ import annotations

from collections.abc import Iterator

import pytest
from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QApplication, QDialog, QVBoxLayout, QWidget
from pytestqt.qtbot import QtBot

from tests.gui.helpers import MemorySettings
from wuwa_assistant.ui.main_window import MainWindow
from wuwa_assistant.ui.navigation import PageId
from wuwa_assistant.ui.pages.equipment_detail import (
    EquipmentDetailPage,
    GearCharacterDetailView,
    GearPieceDetailView,
)
from wuwa_assistant.ui.widgets.text_details import TextDetailsButton


class _TopLevelShows(QObject):
    """Observe even windows that vanish before processEvents or a screenshot."""

    def __init__(self) -> None:
        super().__init__()
        self.names: list[str] = []

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Show and isinstance(watched, QWidget) and watched.isWindow():
            self.names.append(watched.objectName() or watched.metaObject().className())
        return False


@pytest.fixture
def top_level_shows(qapp: QApplication) -> Iterator[_TopLevelShows]:
    probe = _TopLevelShows()
    qapp.installEventFilter(probe)
    yield probe
    qapp.removeEventFilter(probe)


def test_main_window_construction_and_navigation_show_only_the_main_window(
    qtbot: QtBot, qapp: QApplication, top_level_shows: _TopLevelShows
) -> None:
    window = MainWindow(MemorySettings())
    qtbot.addWidget(window)
    assert top_level_shows.names == []
    window.show()
    qapp.processEvents()
    assert top_level_shows.names == ["MainWindow"]
    top_level_shows.names.clear()
    for page_id in PageId:
        window.navigate_to(page_id)
        qapp.processEvents()
    assert top_level_shows.names == []


def test_details_populated_before_layout_never_open_a_standalone_button(
    qtbot: QtBot, top_level_shows: _TopLevelShows
) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    layout = QVBoxLayout(parent)
    button = TextDetailsButton("查看說明")
    button.set_details("評分說明", "完整評分理由")
    assert button.isHidden()
    assert top_level_shows.names == []

    layout.addWidget(button)
    assert not button.isHidden()
    assert not button.isVisible()
    parent.show()
    assert button.isVisible()
    top_level_shows.names.clear()
    button.set_details("評分說明", "")
    assert button.isHidden()
    button.set_details("評分說明", "完整評分理由")
    assert button.isVisible()
    assert top_level_shows.names == []

    button.click()
    dialog = button.findChild(QDialog, "TextDetailsDialog")
    assert dialog is not None and dialog.isVisible()
    assert top_level_shows.names == ["TextDetailsDialog"]
    dialog.close()


def test_empty_details_stay_hidden_when_adopted_by_a_visible_layout(qtbot: QtBot) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    layout = QVBoxLayout(parent)
    parent.show()
    button = TextDetailsButton("查看說明")
    button.set_details("評分說明", "")
    layout.addWidget(button)
    assert button.isHidden()
    button.set_details("評分說明", "完整評分理由")
    assert button.isVisible()


@pytest.mark.parametrize("visible", (False, True))
def test_equipment_rebuild_never_shows_transient_windows(
    qtbot: QtBot, qapp: QApplication, top_level_shows: _TopLevelShows, visible: bool
) -> None:
    page = EquipmentDetailPage()
    qtbot.addWidget(page)
    if visible:
        page.show()
    top_level_shows.names.clear()
    pieces = tuple(
        GearPieceDetailView(
            position=position,
            echo_id=f"echo-{position}",
            name="測試聲骸",
            level=25,
            cost=1,
            set_name="測試套裝",
            main_stats=("攻擊 18%",),
            score=30.0,
            stats=(),
            fit="符合攻略方向",
            action="尋找替換",
            reason="缺少核心詞條",
            target="暴擊 > 攻擊",
            score_note="評分說明",
        )
        for position in range(1, 6)
    )
    detail = GearCharacterDetailView(
        "character-1", "緋雪", "冷凝", "30 / 100", "五件套裝", "角色需求", "未填效率", pieces
    )
    # Recognition publishes refreshed equipment while this page may be hidden;
    # opening a character applies the same immutable detail to a visible page.
    for _ in range(3):
        page.apply_detail(detail)
        qapp.processEvents()
    assert top_level_shows.names == []
