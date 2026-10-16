from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from wuwa_assistant.ui.dialogs.character_catalog import (
    CharacterCatalogDialog,
    CharacterCatalogItemView,
    CharacterCatalogSelectionRequest,
)


def _rows() -> tuple[CharacterCatalogItemView, ...]:
    return (
        CharacterCatalogItemView("feixue", "緋雪", "冷凝", None, 5, "public_name_index"),
        CharacterCatalogItemView("aimisi", "愛彌斯", "衍射", None, 5, "public_name_index"),
        CharacterCatalogItemView(
            "yangyang", "秧秧", "氣動", "迅刀", 4, "official_current_character", True
        ),
    )


def test_catalog_dialog_filters_selects_visible_and_emits_stable_ids(
    qapp: QApplication,
    qtbot: QtBot,
) -> None:
    del qapp
    dialog = CharacterCatalogDialog()
    qtbot.addWidget(dialog)
    dialog.set_catalog(
        _rows(),
        version="2026.08.20-v1",
        as_of_date="2026-08-20",
        disclaimer="只建立本機清單, 不代表帳號擁有或 Tier。",
    )
    captured: list[object] = []
    dialog.import_requested.connect(captured.append)
    dialog.show()

    dialog.search_edit.setText("愛彌斯")
    assert dialog.proxy.rowCount() == 1
    qtbot.mouseClick(dialog.select_visible_button, Qt.MouseButton.LeftButton)
    assert dialog.selected_entry_ids() == ("aimisi",)
    assert dialog.import_button.property("role") == "primary"

    qtbot.mouseClick(dialog.import_button, Qt.MouseButton.LeftButton)
    assert captured == [CharacterCatalogSelectionRequest(("aimisi",))]


def test_catalog_dialog_cannot_select_an_existing_character(
    qapp: QApplication,
    qtbot: QtBot,
) -> None:
    del qapp
    dialog = CharacterCatalogDialog()
    qtbot.addWidget(dialog)
    dialog.set_catalog(_rows(), version="v1", as_of_date="2026-08-20", disclaimer="local only")

    existing_index = dialog.model.index(2, 0)
    assert not bool(dialog.model.flags(existing_index) & Qt.ItemFlag.ItemIsUserCheckable)
    assert not dialog.model.setData(
        existing_index, Qt.CheckState.Checked, int(Qt.ItemDataRole.CheckStateRole)
    )
    assert dialog.selected_entry_ids() == ()
