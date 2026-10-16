from __future__ import annotations

from datetime import date

import pytest
from PySide6.QtWidgets import QApplication

from wuwa_assistant.ui.pages.gacha import (
    GachaIncomeSaveRequest,
    GachaIncomeView,
    GachaPage,
    GachaProjectionView,
    GachaSnapshot,
    GachaStatusSaveRequest,
    GachaStatusView,
    GachaTargetSaveRequest,
    GachaTargetView,
    _TargetEditor,
)


def test_short_window_shows_countdowns_and_hides_resource_editors(qapp: QApplication) -> None:
    page = GachaPage()
    page.resize(1100, 700)
    page.set_snapshot(_snapshot())
    page.show()
    qapp.processEvents()
    fields = (
        page.astrite_spin,
        page.character_pulls_spin,
        page.weapon_pulls_spin,
        page.character_pity_spin,
        page.weapon_pity_spin,
    )
    for field in fields:
        assert not field.isVisible()
    assert page.countdown_feed.isVisible()
    assert page.official_feed.browser.height() >= 120
    assert not page.target_table.isVisible()
    page.scroll_area.ensureWidgetVisible(page.refresh_button)
    qapp.processEvents()
    assert page.rect().contains(
        page.refresh_button.mapTo(page, page.refresh_button.rect().center())
    )
    page.close()


def _snapshot() -> GachaSnapshot:
    return GachaSnapshot(
        status=GachaStatusView(16_000, 10, 5, 20, 10, True, False),
        targets=(
            GachaTargetView(
                "target",
                "character",
                "目標角色",
                1,
                "2.0",
                date(2026, 9, 1),
                1,
                "planned",
                140,
                100,
                80,
            ),
        ),
        incomes=(
            GachaIncomeView(
                "income",
                "版本活動",
                date(2026, 8, 20),
                1_600,
                3,
                "character",
                "typical",
            ),
        ),
        projections=(
            GachaProjectionView("conservative", 110, 5, 0, 30),
            GachaProjectionView(
                "typical",
                123,
                5,
                320,
                0,
                remaining_character_pulls=7,
                remaining_weapon_pulls=2,
            ),
            GachaProjectionView("optimistic", 130, 5, 0, 0),
        ),
        warnings=("未設定日期的收入不會自動納入。",),
    )


def test_legacy_gacha_values_remain_readable_without_visible_mutation_controls(
    qapp: QApplication,
) -> None:
    page = GachaPage()
    status_requests: list[GachaStatusSaveRequest] = []
    page.status_save_requested.connect(status_requests.append)
    page.set_snapshot(_snapshot())
    page.show()
    qapp.processEvents()

    assert page.astrite_spin.value() == 16_000
    assert page.character_guaranteed_check.isChecked()
    assert page.target_model.rowCount() == 1
    assert page.income_model.rowCount() == 1
    assert page.target_model.data(page.target_model.index(0, 5)) == "140 / 100 / 80"
    assert "角色目標已配置 123 抽" in page.projection_cards["typical"].value_label.text()
    assert "剩餘角色券 7" in page.projection_cards["typical"].value_label.text()
    assert "可換 2 抽" in page.projection_cards["typical"].value_label.text()
    assert "未設定日期" in page.warning_label.text()

    page.astrite_spin.setValue(20_000)
    assert not page.status_save_button.isVisible()
    assert page.status_request().astrite == 20_000
    assert status_requests == []
    page.close()


def test_gacha_dtos_keep_unknown_requirements_and_reject_unknown_enums() -> None:
    target = GachaTargetSaveRequest(
        None,
        "weapon",
        "目標武器",
        1,
        None,
        None,
        2,
        "planned",
        None,
        None,
        None,
        "",
    )
    assert target.typical_required is None
    GachaIncomeSaveRequest(
        None,
        "手動收入",
        date(2026, 8, 20),
        160,
        1,
        "unknown",
        "optimistic",
        False,
        "",
    )
    GachaIncomeSaveRequest(
        None,
        "所有情境收入",
        date(2026, 8, 20),
        160,
        0,
        "unknown",
        "general",
        False,
        "",
    )

    with pytest.raises(ValueError, match="pull_type"):
        GachaIncomeView("income", "錯誤", None, 0, 1, "both", "typical")
    with pytest.raises(ValueError, match="scenario"):
        GachaIncomeView("income", "錯誤", None, 0, 1, "unknown", "future")


def test_target_editor_preserves_partial_requirements_and_active_status() -> None:
    editor = _TargetEditor(
        GachaTargetView(
            "target",
            "character",
            "部分人工需求",
            1,
            None,
            date(2026, 9, 1),
            1,
            "active",
            20,
            None,
            10,
        )
    )

    request = editor.request()

    assert request.status == "active"
    assert (
        request.conservative_required,
        request.typical_required,
        request.optimistic_required,
    ) == (20, None, 10)
    editor.close()
