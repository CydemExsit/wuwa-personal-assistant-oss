from __future__ import annotations

from dataclasses import replace

import pytest
from PySide6.QtWidgets import QDialog, QLabel, QTreeWidget
from pytestqt.qtbot import QtBot

from tests.gui.test_equipment_overview_page import _character
from wuwa_assistant.application.equipment_alerts import equipment_fit_alert
from wuwa_assistant.application.equipment_analysis import EchoTrainingRecommendation
from wuwa_assistant.application.equipment_overview import EquippedEchoScore
from wuwa_assistant.planning.farming import (
    FarmCharacter,
    FarmEcho,
    FarmRole,
    FarmZone,
    recommend_zones,
)
from wuwa_assistant.ui.controllers.characters import _equipment_piece
from wuwa_assistant.ui.pages.equipment_detail import (
    EquipmentDetailPage,
    GearCharacterDetailView,
    GearPieceDetailView,
)
from wuwa_assistant.ui.pages.equipment_overview import EquipmentOverviewPage
from wuwa_assistant.ui.widgets.farming_reasons import FarmingReasonsButton, reason_rows


def wrong_main_piece() -> EquippedEchoScore:
    return EquippedEchoScore(
        "echo",
        "shore",
        "守岸人",
        5,
        "測試聲骸",
        "隱世回光",
        25,
        1,
        95,
        95,
        None,
        None,
        None,
        None,
        "scored",
        (),
        set_is_valid=True,
        main_stat_is_valid=False,
        main_stats=(
            {"name": "攻擊", "unit": "%", "value": 18, "stat_kind": "primary"},
            {"name": "生命", "unit": "", "value": 2280, "stat_kind": "fixed"},
        ),
        training_recommendation=EchoTrainingRecommendation(
            "檢查配裝方向", "主詞條需修正", target_main_stats=("生命%",)
        ),
    )


def test_fit_alert_uses_primary_evidence_and_keeps_manual_and_unknown_states() -> None:
    piece = wrong_main_piece()
    text = equipment_fit_alert(piece)
    assert "主詞條不符" in text and "目前 攻擊" in text and "建議 生命%" in text
    assert "2280" not in text
    assert equipment_fit_alert(replace(piece, main_stat_is_valid=True)) == ""
    assert equipment_fit_alert(replace(piece, main_stat_is_valid=None)) == "主詞條待確認"
    assert equipment_fit_alert(replace(piece, echo_id=None)) == ""
    manual = equipment_fit_alert(replace(piece, main_stat_fit_reason="依自訂配裝判定"))
    assert "自訂" in manual and "生命%" not in manual


def test_high_score_wrong_main_is_visible_in_overview_piece_list_and_detail(qtbot: QtBot) -> None:
    piece = _equipment_piece(wrong_main_piece())
    assert piece.score == 95 and piece.fit_summary == "主詞條不符"
    character = replace(_character("shore", 95), name="守岸人", pieces=(piece,))
    page = EquipmentOverviewPage()
    qtbot.addWidget(page)
    page.apply_data((character,), (piece,), profiles=(), profile_id=None, summary="")
    page.show()
    assert any(
        "第5件 主詞條不符" in w.text() and w.isVisible()
        for w in page.findChildren(QLabel, "EquipmentFitAlert")
    )
    page.view_combo.setCurrentIndex(1)
    qtbot.waitUntil(
        lambda: any(
            "目前 攻擊" in w.text() and w.isVisible()
            for w in page.findChildren(QLabel, "EquipmentFitAlert")
        )
    )
    detail = EquipmentDetailPage()
    qtbot.addWidget(detail)
    detail.apply_detail(
        GearCharacterDetailView(
            "shore",
            "守岸人",
            "治療",
            "95分",
            "隱世回光",
            "參考",
            "",
            (
                GearPieceDetailView(
                    5,
                    "echo",
                    "測試聲骸",
                    25,
                    1,
                    "隱世回光",
                    ("攻擊18%",),
                    95,
                    (),
                    "",
                    "檢查配裝方向",
                    "",
                    "",
                    fit_alert=piece.fit_alert,
                ),
            ),
        )
    )
    detail.show()
    assert any(
        "主詞條不符" in w.text() and w.isVisible()
        for w in detail.findChildren(QLabel, "EquipmentFitAlert")
    )
    corrected = _equipment_piece(replace(wrong_main_piece(), main_stat_is_valid=True))
    other = replace(piece, echo_id="other-echo", character_id="other", character_name="其他角色")
    remaining = replace(_character("other", 95), name="其他角色", pieces=(other,))
    page.apply_data(
        (replace(character, pieces=(corrected,)), remaining),
        (corrected, other),
        profiles=(),
        profile_id=None,
        summary="",
    )
    assert "守岸人" not in page.fit_alert_summary.text()
    assert "其他角色第5件 主詞條不符" in page.fit_alert_summary.text()


def sample_plan():
    chars = tuple(
        FarmCharacter(
            str(score),
            f"角色{score}",
            FarmRole.MAIN,
            "主C",
            ("A",),
            (FarmEcho(3, 3, 25, score, "A", True, True),),
        )
        for score in (40, 65)
    )
    return recommend_zones(chars, (FarmZone("a", "測試無音區", "地區", ("A", "B")),))


def test_continuous_score_gap_and_readable_reasons_keep_diagnostics_secondary(qtbot: QtBot) -> None:
    zone = sample_plan().recommendations[0]
    assert zone.benefits[0].need_points == pytest.approx(1.091142857)
    assert zone.benefits[1].need_points == pytest.approx(0.222482143)
    rows = reason_rows(zone)
    assert rows[0][:3] == ("角色40", "主C", "第3件")
    assert "差 30 分" in rows[0][3] and "差 5 分" in rows[1][3]
    owner = EquipmentOverviewPage()
    qtbot.addWidget(owner)
    button = FarmingReasonsButton("理由", owner)
    button.set_details("技術", "角色優先級100 重要性1.00")
    button.set_recommendations((zone,))
    button.click()
    dialog = button.findChild(QDialog, "FarmingReasonsDialog")
    assert dialog is not None
    tree = dialog.findChild(QTreeWidget, "FarmingReasonRows")
    assert tree is not None and tree.columnCount() == 4
    item = tree.topLevelItem(0).child(0)
    assert item.text(0) == "角色40" and "差 30 分" in item.text(3)
    assert "優先級" not in " ".join(item.text(c) for c in range(4))
    assert not dialog.findChildren(QDialog, "TextDetailsDialog")
    dialog.close()
