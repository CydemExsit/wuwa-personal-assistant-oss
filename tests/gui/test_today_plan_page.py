from __future__ import annotations

from datetime import date

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDoubleSpinBox, QStyleOptionViewItem

from wuwa_assistant.application.farming import SkillGap, TodayFarmingSnapshot
from wuwa_assistant.planning.farming import (
    FarmCharacter,
    FarmEcho,
    FarmingPlan,
    FarmRole,
    FarmZone,
    ZoneRecommendation,
    recommend_zones,
)
from wuwa_assistant.ui.pages.today_plan import (
    CharacterTargetSummaryView,
    CharacterTargetTableModel,
    InventoryEditRequest,
    InventoryItemView,
    InventoryTableModel,
    PlanRecommendationView,
    TodayPlanPage,
    TodayPlanRequest,
    TodayPlanSnapshot,
    WeeklyShortageTableModel,
    WeeklyShortageView,
)


def _snapshot() -> TodayPlanSnapshot:
    return TodayPlanSnapshot(
        plan_date=date(2026, 8, 2),
        available_waveplates=240,
        first_priority=PlanRecommendationView(
            "recommendation-1",
            "刷取凝素領域: 技能素材",
            "技能素材",
            4,
            160,
            "緋雪的核心技能仍未達 10 級,且今天素材開放。",
            "緋雪",
        ),
        second_priority=PlanRecommendationView(
            "recommendation-2",
            "刷取武器經驗",
            "武器養成",
            2,
            80,
            "補足主力武器升至 90 級所需經驗。",
            "秧秧",
        ),
        weekly_shortages=(
            WeeklyShortageView(
                "material-skill",
                "技能素材(高級)",
                8,
                20,
                ("緋雪", "秧秧"),
                date(2026, 8, 5),
            ),
            WeeklyShortageView("material-credit", "貝幣", 120_000, 300_000),
        ),
        character_summaries=(
            CharacterTargetSummaryView("character-1", "緋雪", 72.4, False, "核心技能升至 10"),
            CharacterTargetSummaryView("character-2", "秧秧", 100, True, "已達目前目標"),
        ),
        inventory=(
            InventoryItemView("material-skill", "技能素材(高級)", "技能素材", 8),
            InventoryItemView("material-credit", "貝幣", "通用", 120_000),
        ),
        weekly_summary="本週優先補齊 2 種素材,預估 8 月 5 日完成第一輪。",
        plan_generation_id="generation-1",
    )


def test_today_plan_models_expose_stable_roles_and_edit_requests(
    qapp: QApplication,
) -> None:
    del qapp
    snapshot = _snapshot()

    shortages = WeeklyShortageTableModel()
    shortages.set_rows(snapshot.weekly_shortages)
    assert shortages.rowCount() == 2
    assert shortages.columnCount() == 6
    assert shortages.data(shortages.index(0, 0)) == "技能素材(高級)"
    assert shortages.data(shortages.index(0, 3)) == 12
    assert shortages.data(shortages.index(0, 4)) == "緋雪、秧秧"
    assert shortages.data(shortages.index(1, 5)) == "資料不足"
    assert shortages.data(shortages.index(0, 2), int(Qt.ItemDataRole.UserRole)) == "material-skill"
    assert shortages.data(shortages.index(0, 2), int(Qt.ItemDataRole.EditRole)) == 20

    characters = CharacterTargetTableModel()
    characters.set_rows(snapshot.character_summaries)
    assert characters.rowCount() == 2
    assert characters.data(characters.index(0, 1)) == "72%"
    assert characters.data(characters.index(0, 2)) == "尚未達標"
    assert characters.data(characters.index(1, 2)) == "已達目前目標"
    assert characters.data(characters.index(0, 0), int(Qt.ItemDataRole.UserRole)) == "character-1"

    inventory = InventoryTableModel()
    requests: list[InventoryEditRequest] = []
    inventory.inventory_edit_requested.connect(requests.append)
    inventory.set_rows(snapshot.inventory)
    quantity_index = inventory.index(0, 2)
    assert inventory.data(quantity_index) == 8
    assert inventory.flags(quantity_index) & Qt.ItemFlag.ItemIsEditable
    assert not inventory.flags(inventory.index(0, 0)) & Qt.ItemFlag.ItemIsEditable
    assert inventory.setData(quantity_index, "15")
    assert inventory.data(quantity_index) == 8
    assert requests == [InventoryEditRequest("material-skill", 15)]
    assert not inventory.setData(quantity_index, -1)
    assert not inventory.setData(quantity_index, True)
    assert not inventory.setData(quantity_index, 15)

    inventory.set_rows((InventoryItemView("unknown", "尚未盤點", "測試", 0, False),))
    unknown_index = inventory.index(0, 2)
    assert inventory.data(unknown_index) == "尚未輸入"
    assert inventory.data(unknown_index, int(Qt.ItemDataRole.EditRole)) == 0
    assert inventory.setData(unknown_index, 0)
    assert inventory.data(unknown_index) == "尚未輸入"

    shortages.set_rows(
        (
            WeeklyShortageView(
                "unknown",
                "尚未盤點",
                0,
                10,
                current_count_confirmed=False,
            ),
        )
    )
    assert shortages.data(shortages.index(0, 1)) == "尚未輸入"
    assert shortages.data(shortages.index(0, 3)) == "資料不足"
    assert shortages.data(shortages.index(0, 3), int(Qt.ItemDataRole.EditRole)) is None


def test_inventory_editor_preserves_four_decimal_storage_precision(
    qapp: QApplication,
) -> None:
    page = TodayPlanPage()
    page.inventory_model.set_rows((InventoryItemView("material", "素材", "測試", 12.3457),))
    index = page.inventory_model.index(0, 2)
    delegate = page.inventory_table.itemDelegateForColumn(2)
    editor = delegate.createEditor(
        page.inventory_table.viewport(),
        QStyleOptionViewItem(),
        index,
    )
    assert isinstance(editor, QDoubleSpinBox)
    assert editor.decimals() == 4
    delegate.setEditorData(editor, index)
    assert editor.value() == 12.3457

    requests: list[InventoryEditRequest] = []
    page.inventory_edit_requested.connect(requests.append)
    delegate.setModelData(editor, page.inventory_model, index)
    assert requests == []
    editor.setValue(12.3456)
    delegate.setModelData(editor, page.inventory_model, index)
    assert requests == [InventoryEditRequest("material", 12.3456)]
    page.close()
    qapp.processEvents()


def test_today_plan_page_emits_typed_plan_and_inventory_intents(
    qapp: QApplication,
) -> None:
    page = TodayPlanPage()
    plans: list[TodayPlanRequest] = []
    inventory_edits: list[InventoryEditRequest] = []
    manage_requests: list[bool] = []
    complete_requests: list[str] = []
    page.plan_requested.connect(plans.append)
    page.inventory_edit_requested.connect(inventory_edits.append)
    page.manage_data_requested.connect(lambda: manage_requests.append(True))
    page.complete_plan_requested.connect(complete_requests.append)
    page.set_snapshot(_snapshot())
    page.show()
    qapp.processEvents()

    page.plan_date_edit.setDate(page.plan_date_edit.date().addDays(1))
    page.waveplates_spin.setValue(180)
    QTest.mouseClick(page.plan_button, Qt.MouseButton.LeftButton)

    assert plans == [TodayPlanRequest(date(2026, 8, 3), 180)]
    assert not page.plan_button.isEnabled()
    assert not page.inventory_table.isEnabled()
    assert "正在計算" in page.status_label.text()

    page.set_loading(False)
    assert page.plan_button.isEnabled()
    assert page.inventory_table.isEnabled()
    QTest.mouseClick(page.manage_data_button, Qt.MouseButton.LeftButton)
    assert manage_requests == [True]
    QTest.mouseClick(page.complete_plan_button, Qt.MouseButton.LeftButton)
    assert complete_requests == ["generation-1"]
    assert not page.inventory_table.isEnabled()
    page.set_loading(False)
    assert page.inventory_model.setData(page.inventory_model.index(1, 2), 150_000)
    assert inventory_edits == [InventoryEditRequest("material-credit", 150_000)]
    assert "正在保存" in page.status_label.text()
    page.close()


def test_today_plan_page_renders_priorities_tables_and_completion_summary(
    qapp: QApplication,
) -> None:
    page = TodayPlanPage()
    page.resize(1100, 760)
    page.show()
    qapp.processEvents()
    page.set_snapshot(_snapshot())
    qapp.processEvents()

    assert page.objectName() == "TodayPlanPage"
    assert page.scroll_area.widgetResizable()
    assert page.plan_date_edit.date().toString("yyyy-MM-dd") == "2026-08-02"
    assert page.waveplates_spin.value() == 240
    assert "凝素領域" in page.first_priority_card.title_label.text()
    assert "緋雪" in page.first_priority_card.title_label.text()
    assert "建議刷取 4 次" in page.first_priority_card.meta_label.text()
    assert "核心技能" in page.first_priority_card.reason_label.text()
    assert "武器經驗" in page.second_priority_card.title_label.text()
    assert page.shortage_model.rowCount() == 2
    assert page.character_model.rowCount() == 2
    assert page.inventory_model.rowCount() == 2
    assert "2 種素材" in page.weekly_summary_label.text()
    assert not page.empty_state_label.isVisible()
    assert "2026-08-02" in page.status_label.text()
    page.close()


def test_today_plan_page_handles_empty_loading_error_and_keeps_event_loop_responsive(
    qapp: QApplication,
) -> None:
    page = TodayPlanPage()
    page.resize(800, 560)
    page.show()
    qapp.processEvents()
    page.set_snapshot(TodayPlanSnapshot(date(2026, 8, 2), 240))
    qapp.processEvents()

    assert page.empty_state_label.isVisible()
    assert page.first_priority_card.title_label.text() == "尚無建議"
    assert page.shortage_model.rowCount() == 0
    assert page.character_model.rowCount() == 0
    assert page.inventory_model.rowCount() == 0
    assert "沒有可規劃" in page.status_label.text()

    event_loop_progress: list[bool] = []
    QTimer.singleShot(0, lambda: event_loop_progress.append(True))
    QTest.mouseClick(page.plan_button, Qt.MouseButton.LeftButton)
    qapp.processEvents()
    assert event_loop_progress == [True]
    assert not page.plan_date_edit.isEnabled()
    assert not page.waveplates_spin.isEnabled()

    page.show_error("素材資料暫時無法讀取")
    assert page.status_label.property("error") is True
    assert "素材資料暫時無法讀取" in page.status_label.text()
    assert page.plan_button.isEnabled()
    assert page.inventory_table.isEnabled()

    page.show_status("庫存已保存,請重新規劃。")
    assert page.status_label.property("error") is False
    assert page.status_label.text() == "庫存已保存,請重新規劃。"
    page.close()


def test_today_plan_dtos_reject_invalid_or_ambiguous_values() -> None:
    with pytest.raises(ValueError, match="available_waveplates"):
        TodayPlanRequest(date(2026, 8, 2), -1)
    with pytest.raises(ValueError, match="material_id"):
        InventoryEditRequest("", 1)
    with pytest.raises(ValueError, match="completion_percent"):
        CharacterTargetSummaryView("character", "角色", float("nan"), False, "下一步")
    with pytest.raises(ValueError, match="second_priority"):
        TodayPlanSnapshot(
            date(2026, 8, 2),
            240,
            second_priority=PlanRecommendationView("recommendation", "項目", "類別", 1, 40, "原因"),
        )
    with pytest.raises(ValueError, match="identifiers"):
        item = InventoryItemView("material", "素材", "類別", 1)
        TodayPlanSnapshot(date(2026, 8, 2), 240, inventory=(item, item))


def test_today_planner_keeps_skills_and_fields_visible_without_inventory_maintenance(
    qapp: QApplication,
) -> None:
    page = TodayPlanPage()
    page.resize(1100, 760)
    farming = TodayFarmingSnapshot(
        (SkillGap("character", "測試角色", "skill", "共鳴技能", 6, 10, True),),
        FarmingPlan(
            (ZoneRecommendation(FarmZone("field", "山谷", "地區", ("套裝A", "套裝B")), 0, (), ()),),
            (),
        ),
        "已查核雙套裝來源",
        1,
    )
    page.set_snapshot(
        TodayPlanSnapshot(
            date(2026, 10, 8), 240, farming=farming, activity_notices=("無音區雙倍進行中",)
        )
    )
    page.show()
    qapp.processEvents()
    assert page.skill_group.isVisible()
    assert page.zone_group.isVisible()
    assert page.activity_label.isVisible()
    assert "雙倍" in page.activity_label.text()
    assert page.skill_model.data(page.skill_model.index(0, 2)) == "6 → 10"
    assert "套裝A" in str(page.zone_model.data(page.zone_model.index(0, 1)))
    assert not page.legacy_content.isVisible()
    assert not page.manage_data_button.isVisible()
    assert not page.inventory_table.isVisible()
    assert not page.waveplates_spin.isVisible()
    page.set_snapshot(
        TodayPlanSnapshot(
            date(2026, 10, 9),
            240,
            farming=TodayFarmingSnapshot((), farming.farming, farming.catalog_note, 1),
            activity_notices=(),
        )
    )
    qapp.processEvents()
    assert page.skill_group.isVisible() and page.zone_group.isVisible()
    assert "已達目標" in page.skill_summary_label.text()
    assert "雙倍進行中" not in page.activity_label.text()
    assert page.zone_model.rowCount() == 1
    page.close()


def test_field_recommendation_exposes_unique_counts_and_auditable_score_breakdown(
    qapp: QApplication,
) -> None:
    plan = recommend_zones(
        (
            FarmCharacter(
                "a",
                "測試中的高優先主輸出角色",
                FarmRole.MAIN,
                "主C",
                ("A",),
                (FarmEcho(1, 3, 25, 20, "A", True, True),),
                priority=1,
            ),
            FarmCharacter(
                "b",
                "測試中的低優先副輸出角色",
                FarmRole.SECONDARY,
                "副C",
                ("B",),
                (FarmEcho(2, 1, 25, 40, "B", True, True),),
                priority=100,
            ),
        ),
        (FarmZone("ab", "雙收益", "地區", ("A", "B")),),
    )
    page = TodayPlanPage()
    page.set_farming(TodayFarmingSnapshot((), plan, "來源", 2))
    assert "2 人 / 2 件" in page.zone_model.data(page.zone_model.index(0, 2))
    assert "2 位角色 / 2 件受益" in page.zone_summary_label.text()
    explanation = page.zone_details._details_text
    for expected in (
        "養成排序點",
        "角色覆蓋",
        "雙套裝利用",
        "角色優先級 1",
        "重要性 1.60 倍",
        "20 分",
    ):
        assert expected in explanation
    page.resize(960, 760)
    page.show()
    for _ in range(5):
        qapp.processEvents()
    narrow_height = page.zone_table.rowHeight(0)
    assert narrow_height >= page.zone_table.sizeHintForRow(0)
    page.resize(1800, 1000)
    for _ in range(5):
        qapp.processEvents()
    assert page.zone_table.rowHeight(0) >= page.zone_table.sizeHintForRow(0)
    assert page.zone_table.rowHeight(0) < narrow_height
    page.close()
