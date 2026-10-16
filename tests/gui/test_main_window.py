from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QPushButton, QTabWidget, QVBoxLayout

from tests.gui.helpers import MemorySettings
from wuwa_assistant.ui.main_window import MainWindow
from wuwa_assistant.ui.navigation import PageId
from wuwa_assistant.ui.pages.characters import CharactersPage
from wuwa_assistant.ui.pages.gacha import GachaPage
from wuwa_assistant.ui.pages.tasks_events import TasksEventsPage
from wuwa_assistant.ui.pages.today_plan import TodayPlanPage
from wuwa_assistant.ui.window_state import LAST_PAGE_KEY, WINDOW_GEOMETRY_KEY


def test_main_window_switches_all_pages_and_dashboard_actions(
    qapp: QApplication,
) -> None:
    settings = MemorySettings()
    window = MainWindow(settings)
    window.show()
    qapp.processEvents()

    assert not window.characters_page.equipment_workspace.isVisible()

    for page_id in PageId:
        window.navigate_to(page_id)
        assert window.current_page_id is (PageId.SETTINGS if page_id is PageId.HISTORY else page_id)
        assert window.page_stack.currentWidget() is window.page_widget(
            PageId.SETTINGS if page_id is PageId.HISTORY else page_id
        )
        assert window.page_widget(page_id).isVisible()
        assert all(
            not window.page_widget(other).isVisible()
            for other in PageId
            if other != page_id and other not in {PageId.HISTORY, PageId.SETTINGS}
        )

    window.navigate_to(PageId.DASHBOARD)
    echo_button = window.dashboard_page.findChild(QPushButton, "DashboardEchoButton")
    assert echo_button is not None
    echo_button.click()
    assert window.current_page_id is PageId.ECHO_ENHANCEMENT

    assert isinstance(window.page_widget(PageId.SETTINGS), QTabWidget)
    assert window.settings_tabs.count() == 3
    assert window.settings_tabs.widget(0) is window.settings_page
    assert window.settings_tabs.widget(1) is window.scoring_profiles_page
    assert window.settings_tabs.widget(2) is window.history_page
    assert isinstance(window.page_widget(PageId.CHARACTERS), CharactersPage)
    assert isinstance(window.page_widget(PageId.TODAY_PLAN), TodayPlanPage)
    assert isinstance(window.page_widget(PageId.GACHA), GachaPage)
    assert isinstance(window.page_widget(PageId.TASKS_EVENTS), TasksEventsPage)

    window.close()
    assert settings.values[LAST_PAGE_KEY] == PageId.ECHO_ENHANCEMENT.value
    assert WINDOW_GEOMETRY_KEY in settings.values
    assert settings.sync_count == 1


def test_main_window_restores_saved_page_and_geometry(qapp: QApplication) -> None:
    settings = MemorySettings()
    first = MainWindow(settings)
    first.resize(1260, 780)
    first.navigate_to(PageId.SETTINGS)
    first.show()
    qapp.processEvents()
    first.close()

    second = MainWindow(settings)
    assert second.current_page_id is PageId.SETTINGS
    assert second.geometry_restored
    assert WINDOW_GEOMETRY_KEY in settings.values
    second.show()
    qapp.processEvents()
    assert second.size().width() >= second.minimumWidth()
    assert second.size().height() >= second.minimumHeight()
    second.close()


def test_mouse_button_four_returns_from_child_and_preserves_draft(qapp: QApplication) -> None:
    window = MainWindow(MemorySettings())
    window.show()
    window.navigate_to(PageId.CHARACTERS)
    window.characters_page.notes_edit.setPlainText("unsaved local draft")
    window.navigate_to(PageId.SETTINGS)
    qapp.processEvents()
    QTest.mouseClick(window.settings_page, Qt.MouseButton.BackButton)
    assert window.current_page_id is PageId.CHARACTERS
    assert window.characters_page.notes_edit.toPlainText() == "unsaved local draft"
    QTest.mouseClick(window.characters_page, Qt.MouseButton.BackButton)
    assert window.current_page_id is PageId.DASHBOARD
    assert not window.back_button.isEnabled()
    window.close()


def test_back_restores_equipment_detail_after_replacement_route(qapp: QApplication) -> None:
    from wuwa_assistant.ui.pages.equipment_detail import (
        GearCharacterDetailView,
        GearPieceDetailView,
    )

    window = MainWindow(MemorySettings())
    page = window.characters_page
    piece = GearPieceDetailView(3, "echo", "test", 25, 3, "set", (), 40, (), "", "", "", "")
    page.set_equipment_details(
        (
            GearCharacterDetailView(
                "character",
                "test",
                "",
                "40",
                "",
                "",
                "",
                (piece,),
            ),
        )
    )
    window.show()
    window.navigate_to(PageId.EQUIPMENT)
    page.open_equipment_piece("character", 3)
    assert window.current_page_id is PageId.EQUIPMENT
    assert page.equipment_workspace.currentIndex() == 1
    window.navigate_to(PageId.ECHO_ENHANCEMENT)
    window.go_back()
    assert window.current_page_id is PageId.EQUIPMENT
    assert page.equipment_workspace.currentIndex() == 1
    assert page.equipment_detail_page._focused_slot == 3
    window.go_back()
    assert page.equipment_workspace.currentIndex() == 0
    assert window.current_page_id is PageId.EQUIPMENT
    window.go_back()
    assert window.current_page_id is PageId.DASHBOARD
    window.close()


def test_mouse_back_closes_owned_dialog_without_navigating_underneath(qapp: QApplication) -> None:
    window = MainWindow(MemorySettings())
    window.show()
    window.navigate_to(PageId.CHARACTERS)
    dialog = QDialog(window)
    button = QPushButton("dialog child", dialog)
    QVBoxLayout(dialog).addWidget(button)
    dialog.show()
    qapp.processEvents()
    QTest.mouseClick(button, Qt.MouseButton.BackButton)
    assert not dialog.isVisible()
    assert window.current_page_id is PageId.CHARACTERS
    window.close()


def test_back_to_general_character_card_does_not_restore_old_piece_focus(
    qapp: QApplication,
) -> None:
    from wuwa_assistant.ui.pages.equipment_detail import (
        GearCharacterDetailView,
        GearPieceDetailView,
    )

    window = MainWindow(MemorySettings())
    page = window.characters_page
    piece = GearPieceDetailView(5, "echo", "test", 25, 3, "set", (), 40, (), "", "", "", "")
    page.set_equipment_details(
        (
            GearCharacterDetailView(
                "character",
                "test",
                "",
                "40",
                "",
                "",
                "",
                (piece,),
            ),
        )
    )
    window.navigate_to(PageId.EQUIPMENT)
    page.open_equipment_piece("character", 5)
    window.go_back()
    page.open_equipment_character("character")
    window.navigate_to(PageId.CHARACTERS)
    window.go_back()
    assert page.equipment_detail_page._focused_slot is None
    window.close()


def test_equipment_split_keeps_selection_and_back_history_across_window_sizes(
    qapp: QApplication,
) -> None:
    from wuwa_assistant.ui.pages.equipment_detail import (
        GearCharacterDetailView,
        GearPieceDetailView,
    )

    window = MainWindow(MemorySettings())
    page = window.characters_page
    pieces = tuple(
        GearPieceDetailView(i, f"echo-{i}", "聲骸", 25, 3, "套裝", (), 40, (), "", "", "", "")
        for i in range(1, 6)
    )
    page.set_equipment_details(
        (GearCharacterDetailView("test", "角色", "", "40", "", "", "", pieces),)
    )
    window.resize(2048, 1120)
    window.show()
    window.navigate_to(PageId.EQUIPMENT)
    qapp.processEvents()
    try:
        workspace = page.equipment_workspace
        detail = page.equipment_detail_page
        assert workspace.side_by_side
        assert page.overview_page.isVisible() and detail.isVisible()
        assert detail.cards[1].isVisible()
        assert sum(card.isVisible() for card in detail.cards.values()) == 1
        detail.slot_buttons[3].click()
        qapp.processEvents()
        assert workspace.currentIndex() == 1
        assert detail._focused_slot == 3 and detail.cards[3].isVisible()
        detail.slot_buttons[4].click()
        window.go_back()
        qapp.processEvents()
        assert detail._focused_slot == 3 and detail.slot_buttons[3].isChecked()
        assert page.overview_page._selected_slot == 3
        assert detail.scroll_area.horizontalScrollBar().maximum() == 0
        window.resize(1100, 760)
        qapp.processEvents()
        assert not workspace.side_by_side
        assert not page.overview_page.isVisible() and detail.isVisible()
        assert all(card.isVisible() for card in detail.cards.values())
        assert detail._focused_slot == 3
        window.resize(2048, 1120)
        qapp.processEvents()
        assert workspace.side_by_side
        assert detail.cards[3].isVisible()
        assert sum(card.isVisible() for card in detail.cards.values()) == 1
        page.set_equipment_details(())
        qapp.processEvents()
        assert detail._detail is None and not detail.cards
    finally:
        window.close()
