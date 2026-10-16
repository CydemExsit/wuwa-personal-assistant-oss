from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from wuwa_assistant.ui.navigation import PAGE_ID_ROLE, NavigationModel, PageId


def test_navigation_model_exposes_all_v1_pages_in_order(qapp: QApplication) -> None:
    del qapp
    model = NavigationModel()

    assert model.rowCount() == 9
    assert model.labels() == (
        "總覽",
        "養成規劃",
        "角色",
        "聲骸",
        "聲骸強化",
        "抽卡規劃",
        "活動與待辦",
        "兌換碼",
        "設定",
    )
    for row, expected in enumerate(item for item in PageId if item is not PageId.HISTORY):
        index = model.index(row, 0)
        assert model.data(index, int(Qt.ItemDataRole.DisplayRole)) == model.labels()[row]
        assert model.data(index, PAGE_ID_ROLE) == expected.value
        assert model.page_id_at(index) is expected
        assert model.index_for(expected) == index
