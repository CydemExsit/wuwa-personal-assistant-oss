from __future__ import annotations

import pytest
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QTextBrowser
from pytestqt.qtbot import QtBot

from wuwa_assistant.ui.pages.characters import CharactersPage
from wuwa_assistant.ui.pages.equipment_detail import (
    EquipmentDetailPage,
    GearCharacterDetailView,
    GearPieceDetailView,
    SubstatLineView,
)


@pytest.mark.parametrize("width", (1200, 1440))
def test_verbose_equipment_reasons_require_click_and_remain_complete(
    qtbot: QtBot, width: int
) -> None:
    page = EquipmentDetailPage()
    qtbot.addWidget(page)
    note = "副詞條 2 數值信心不足。" + "詳細評分依據 " * 100 + "RAW_" * 300
    reason = "缺少核心詞條。" * 100
    context = "未填面板效率, 依參考值計算。" * 100
    piece = GearPieceDetailView(
        1,
        "echo-1",
        "心傀·喜",
        25,
        1,
        "銜夢照世之心",
        ("攻擊 18%",),
        None,
        (),
        "符合攻略方向",
        "補齊辨識資料",
        reason,
        "暴擊 > 攻擊",
        note,
    )
    page.apply_detail(
        GearCharacterDetailView(
            "心",
            "心",
            "導電",
            "資料待補",
            "銜夢照世之心 5/5",
            "角色需求參考",
            context,
            (piece,),
        )
    )
    page.resize(width, 800)
    page.show()
    qtbot.waitUntil(lambda: page.scroll_area.viewport().width() > 0)
    labels = [label.text() for label in page.findChildren(QLabel)]
    assert note not in labels and reason not in labels and context not in labels
    assert "補齊辨識資料" in labels
    assert page.scroll_area.horizontalScrollBar().maximum() == 0
    for name, expected in (
        ("EquipmentSlot1Details", (note, reason)),
        ("EquipmentEffectiveWeightsDetails", (context,)),
    ):
        button = page.findChild(QPushButton, name)
        assert button is not None
        button.click()
        dialog = button.findChild(QDialog, "TextDetailsDialog")
        assert dialog is not None and dialog.isVisible()
        browser = dialog.findChild(QTextBrowser, "TextDetailsContent")
        assert browser is not None
        assert all(text in browser.toPlainText() for text in expected)
        assert browser.horizontalScrollBar().maximum() == 0
        dialog.close()


def test_offline_detail_exposes_raw_stats_points_and_target_without_editing(qtbot: QtBot) -> None:
    page = CharactersPage()
    qtbot.addWidget(page)
    pieces = tuple(
        GearPieceDetailView(
            position=i,
            echo_id=f"echo-{i}",
            name="共鳴迴響 · 鳴式 · 虛造神型",
            level=25,
            cost=4,
            set_name="雪落無聲之願",
            main_stats=("暴擊傷害 44%", "攻擊 150"),
            score=25.25,
            stats=(
                SubstatLineView("暴擊", "7.5%", "核心", 20, "權重 1"),
                SubstatLineView("共鳴效率", "10%", "次要", 5.25),
            ),
            fit="符合攻略方向",
            action="優先找替換",
            reason="缺少核心詞條",
            target="暴擊 > 攻擊",
        )
        for i in range(1, 6)
    )
    page.set_equipment_details(
        (
            GearCharacterDetailView(
                "character",
                "緋雪",
                "冷凝",
                "25.25 均分 · 5/5 件",
                "雪落無聲之願 5/5",
                "逐條加總",
                "未填面板共鳴效率",
                pieces,
            ),
        )
    )
    page.resize(1100, 760)
    page.show()
    page.open_equipment_piece("character", 3)
    assert page.equipment_workspace.currentWidget() is page.equipment_detail_page
    assert len(page.equipment_detail_page.cards) == 5
    labels = [label.text() for label in page.equipment_detail_page.findChildren(QLabel)]
    assert "7.5%" in labels and "+20.00" in labels and "+5.25" in labels
    assert not any("套裝確認" in text or "主詞條確認" in text for text in labels)
    button = page.equipment_detail_page.findChild(QPushButton, "TrainReplacementSlot3")
    assert button is not None
    with qtbot.waitSignal(page.training_requested) as event:
        button.click()
    assert event.args == ["character", 3]


def test_shared_priority_and_weights_appear_once_and_cards_reflow(qtbot: QtBot) -> None:
    page = EquipmentDetailPage()
    qtbot.addWidget(page)
    pieces = tuple(
        GearPieceDetailView(
            i,
            f"echo-{i}",
            "共鳴迴響",
            25,
            3,
            "雪落無聲之願",
            ("攻擊 30%",),
            80.0,
            (SubstatLineView("暴擊", "7.5%", "核心", 20),),
            "符合角色需求",
            "",
            "",
            "替換方向: 攻擊 | 優先詞條: 暴擊、攻擊",
        )
        for i in range(1, 6)
    )
    page.apply_detail(
        GearCharacterDetailView(
            "character",
            "緋雪",
            "冷凝",
            "400.0 / 500",
            "雪落無聲之願 5/5",
            "",
            "",
            pieces,
            priority_stats="暴擊、攻擊",
            effective_weights="暴擊 1.0 · 攻擊 0.6",
        )
    )
    page.resize(1120, 760)
    page.show()
    qtbot.waitUntil(lambda: page._columns == 2)
    labels = [label.text() for label in page.findChildren(QLabel) if label.isVisible()]
    assert labels.count("優先詞條: 暴擊、攻擊") == 1
    assert not any("替換方向" in label for label in labels)
    assert page.scroll_area.horizontalScrollBar().maximum() == 0
    button = page.findChild(QPushButton, "EquipmentEffectiveWeightsDetails")
    assert button is not None
    button.click()
    dialog = button.findChild(QDialog, "TextDetailsDialog")
    assert dialog is not None
    browser = dialog.findChild(QTextBrowser, "TextDetailsContent")
    assert browser is not None and "暴擊 1.0 · 攻擊 0.6" in browser.toPlainText()
    dialog.close()
    page.resize(720, 760)
    qtbot.waitUntil(lambda: page._columns == 1)
    assert page.scroll_area.horizontalScrollBar().maximum() == 0
    page.focus_slot(4)
    assert page._focused_slot == 4
