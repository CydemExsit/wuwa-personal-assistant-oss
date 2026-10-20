from __future__ import annotations

from dataclasses import replace

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QStyleOptionViewItem,
    QTextBrowser,
    QWidget,
)

from tests.gui.helpers import MemorySettings
from wuwa_assistant.ui.main_window import MainWindow
from wuwa_assistant.ui.navigation import PageId
from wuwa_assistant.ui.pages.characters import (
    CharacterDetailView,
    CharacterEditRequest,
    CharactersPage,
    CharacterSummaryView,
    ChoiceOptionView,
    PermanentEchoSlotView,
    SkillProgressView,
    WeaponProgressView,
)


def _summaries() -> tuple[CharacterSummaryView, ...]:
    return (
        CharacterSummaryView("character-1", "緋雪", "冷凝", "主輸出", True, 1, 72),
        CharacterSummaryView("character-2", "秧秧", "氣動", "副輸出", True, 2, 58),
    )


def test_archived_visibility_preserves_active_draft_and_variant_identity(
    qapp: QApplication,
) -> None:
    page = CharactersPage()
    active = replace(_summaries()[0], name="秧秧·玄翎")
    archived = replace(_summaries()[1], name="秧秧・玄翎", archived=True)
    page.set_characters((active, archived))
    assert page.character_model.rowCount() == 1
    assert "(1)" in page.show_archived_check.text()
    page.select_character(active.identifier)
    page.apply_character(replace(_detail(), name=active.name))
    page.name_edit.setText("尚未儲存的編輯")
    page.show_archived_check.setChecked(True)
    assert page.character_model.rowCount() == 2
    assert page.name_edit.text() == "尚未儲存的編輯"
    page.show_archived_check.setChecked(False)
    assert page.current_character_id() == active.identifier
    assert page.name_edit.text() == "尚未儲存的編輯"
    page.close()


def test_external_refresh_cannot_restore_hidden_archived_detail(qapp: QApplication) -> None:
    page = CharactersPage()
    active = _summaries()[0]
    page.set_characters((active,))
    page.select_character(active.identifier)
    page.apply_character(_detail())
    page.apply_external_characters(
        (replace(active, archived=True),), (replace(_detail(), archived=True),)
    )
    assert page.character_model.rowCount() == 0
    assert page.current_character_id() is None
    page.show_archived_check.setChecked(True)
    assert page.character_model.rowCount() == 1
    page.close()


def _skills() -> tuple[SkillProgressView, ...]:
    return (
        SkillProgressView("normal_attack", "普攻", 6, 6, False),
        SkillProgressView("resonance_skill", "共鳴技能", 8, 10, True),
        SkillProgressView("forte_circuit", "共鳴迴路", 9, 10, True),
        SkillProgressView("resonance_liberation", "共鳴解放", 10, 10, True),
        SkillProgressView("intro_skill", "變奏技能", 6, 6, False),
    )


def _echo_slots() -> tuple[PermanentEchoSlotView, ...]:
    return (
        PermanentEchoSlotView(
            1,
            "echo-1",
            "不熄獵手",
            "羽落空塵之歌",
            "生命 15.5%",
            1,
            15,
            76.5,
            True,
            True,
            True,
        ),
        PermanentEchoSlotView(2),
        PermanentEchoSlotView(3),
        PermanentEchoSlotView(4),
        PermanentEchoSlotView(5),
    )


def _detail() -> CharacterDetailView:
    return CharacterDetailView(
        identifier="character-1",
        name="緋雪",
        element="冷凝",
        role="主輸出",
        is_candidate=True,
        priority=1,
        level=90,
        resonance_chain=0,
        scoring_profile_id="profile-1",
        scoring_profile_name="通用輸出型",
        weapon=WeaponProgressView("長離之刃", 90, 1),
        team_id="team-1",
        team_name="緋雪隊",
        skills=_skills(),
        attribute_nodes=(True, True, True, True, True, True, True, True, False, False),
        echo_slots=_echo_slots(),
        cultivation_goal="核心技能 10, 五顆永久聲骸及格。",
        notes="優先補齊第三位置。",
        completion_percent=72,
        passing_echoes=1,
        weakest_position="位置 3",
        next_suggestion="先替換主詞條錯誤的第三顆聲骸。",
    )


def test_character_page_starts_empty_with_resizable_master_detail(
    qapp: QApplication,
) -> None:
    page = CharactersPage()
    page.resize(1100, 760)
    page.show()
    qapp.processEvents()

    assert page.objectName() == "CharactersPage"
    assert page.character_table.objectName() == "CharacterMasterTable"
    assert page.character_model.rowCount() == 0
    assert page.detail_stack.currentIndex() == 0
    assert page.equipment_workspace.isHidden()
    assert page.equipment_workspace.currentWidget() is page.overview_page
    qapp.processEvents()
    assert page.empty_state_label.isVisible()
    assert page.catalog_button.property("role") == "primary"
    assert not page.save_button.isEnabled()
    assert page.detail_scroll.widgetResizable()
    assert page.splitter.orientation() == Qt.Orientation.Horizontal
    assert page.splitter.childrenCollapsible() is False
    sizes = page.splitter.sizes()
    assert len(sizes) == 2 and all(size > 0 for size in sizes)
    page.close()


def test_manual_progress_shortcuts_keep_draft_and_save_visible(qapp: QApplication) -> None:
    page = CharactersPage()
    page.resize(1200, 800)
    page.set_characters(_summaries())
    page.select_character("character-1")
    page.apply_character(replace(_detail(), level=60, weapon=WeaponProgressView("測試武器", 50, 2)))
    page.show()
    qapp.processEvents()
    edits: list[CharacterEditRequest] = []
    page.edit_requested.connect(edits.append)
    page.level_max_button.click()
    page.weapon_level_max_button.click()
    page.skills_target_button.click()
    assert not edits
    assert "技能 5/5" in page.progress_label.text()
    assert all(s.current_level >= s.target_level for s in page.skill_model.rows())
    page.edit_mode_button.setChecked(True)
    qapp.processEvents()
    scroll = page.detail_scroll.verticalScrollBar()
    assert scroll.maximum() > 0
    scroll.setValue(scroll.maximum())
    qapp.processEvents()
    assert page.save_button.isVisible()
    assert page.rect().contains(page.save_button.mapTo(page, page.save_button.rect().center()))
    page.save_button.click()
    assert edits[0].level == 90
    assert edits[0].weapon == WeaponProgressView("測試武器", 90, 2)
    assert edits[0].skills[0].target_level == 6
    assert not hasattr(page, "completion_label")
    page.close()


@pytest.mark.parametrize("width", (1200, 1440))
def test_unscored_echo_notes_are_clickable_without_widening_cultivation_page(
    qapp: QApplication, width: int
) -> None:
    page = CharactersPage()
    page.resize(width, 800)
    note = (
        "暫估: 未填角色面板共鳴效率, 採攻略參考 120% 達標前權重。"
        "副詞條 2 數值信心 40%, 請確認數值; " + "詳細評分規則與計算依據 " * 80 + "RAW_" * 300
    )
    slot = replace(_echo_slots()[0], score=None, score_note=note)
    page.set_characters(_summaries())
    page.select_character("character-1")
    page.apply_character(replace(_detail(), echo_slots=(slot, *_echo_slots()[1:])))
    page.edit_mode_button.setChecked(True)
    page.echo_management_toggle.setChecked(True)
    page.show()
    for _ in range(3):
        qapp.processEvents()
    card = page.echo_cards[0]
    assert card.score_label.text() == "尚未評分"
    assert not any(note in label.text() for label in page.findChildren(QLabel))
    assert page.detail_scroll.horizontalScrollBar().maximum() == 0
    assert page.detail_scroll.widget().width() <= page.detail_scroll.viewport().width()
    card.score_details_button.click()
    qapp.processEvents()
    dialog = card.findChild(QDialog, "TextDetailsDialog")
    assert dialog is not None and dialog.isVisible()
    browser = dialog.findChild(QTextBrowser, "TextDetailsContent")
    assert browser is not None and browser.toPlainText() == note
    assert browser.horizontalScrollBar().maximum() == 0
    assert browser.verticalScrollBar().maximum() > 0
    dialog.close()
    page.apply_character(_detail())
    qapp.processEvents()
    assert card.score_details_button.isHidden()
    assert card.score_label.text() == "76.5 分 · 及格"
    page.close()


def test_character_model_and_selection_emit_stable_identifiers(qapp: QApplication) -> None:
    page = CharactersPage()
    selections: list[str] = []
    page.character_selected.connect(selections.append)
    page.set_characters(_summaries())
    page.show()
    qapp.processEvents()

    assert page.character_model.rowCount() == 2
    assert page.character_model.columnCount() == 5
    assert page.character_model.data(page.character_model.index(0, 0)) == "緋雪"
    assert not page.character_model.index(0, 5).isValid()
    assert (
        page.character_model.data(page.character_model.index(0, 0), int(Qt.ItemDataRole.UserRole))
        == "character-1"
    )

    page.character_table.setCurrentIndex(page.character_model.index(1, 0))
    qapp.processEvents()
    assert selections[-1] == "character-2"
    assert page.current_character_id() == "character-2"
    assert page.select_character("character-1")
    assert not page.select_character("missing")
    page.close()


def test_add_flow_opens_blank_editor_and_emits_create_edit_request(
    qapp: QApplication,
) -> None:
    page = CharactersPage()
    page.set_scoring_profiles((ChoiceOptionView("profile-1", "通用輸出型"),))
    adds: list[bool] = []
    edits: list[CharacterEditRequest] = []
    page.add_requested.connect(lambda: adds.append(True))
    page.edit_requested.connect(edits.append)
    page.show()
    qapp.processEvents()

    QTest.mouseClick(page.add_button, Qt.MouseButton.LeftButton)
    assert adds == [True]
    assert page.detail_stack.currentIndex() == 1
    assert page.save_button.isEnabled()
    assert not page.archive_button.isEnabled()
    assert page.name_edit.text() == ""
    assert page.skill_model.rowCount() == 5
    assert page.skill_model.data(page.skill_model.index(0, 2)) == 6
    assert page.skill_model.data(page.skill_model.index(1, 2)) == 10
    assert (
        page.skill_model.data(page.skill_model.index(1, 3), int(Qt.ItemDataRole.CheckStateRole))
        is None
    )
    assert not any(check.isChecked() for check in page.attribute_node_checks)
    assert all(card.name_label.text() == "尚未裝備" for card in page.echo_cards)

    page.name_edit.setText("新角色")
    page.role_edit.setText("治療")
    page.scoring_profile_combo.setCurrentIndex(1)
    page.skill_model.setData(page.skill_model.index(1, 2), 10)
    page.skill_model.setData(
        page.skill_model.index(1, 3), Qt.CheckState.Checked, int(Qt.ItemDataRole.CheckStateRole)
    )
    page.attribute_node_checks[0].setChecked(True)
    QTest.mouseClick(page.save_button, Qt.MouseButton.LeftButton)

    assert len(edits) == 1
    request = edits[0]
    assert request.identifier is None
    assert request.name == "新角色"
    assert request.element == ""
    assert request.role == "治療"
    assert request.scoring_profile_id == "profile-1"
    assert request.skills[1].target_level == 10
    assert request.skills[1].is_core is True
    assert request.attribute_nodes[0] is True
    page.close()


def test_detail_render_edit_and_all_management_intent_signals(qapp: QApplication) -> None:
    page = CharactersPage()
    page.set_characters(_summaries())
    page.set_scoring_profiles((ChoiceOptionView("profile-1", "通用輸出型"),))
    edits: list[CharacterEditRequest] = []
    archives: list[str] = []
    weapons: list[str] = []
    teams: list[str] = []
    echoes: list[tuple[str, int]] = []
    page.edit_requested.connect(edits.append)
    page.archive_requested.connect(archives.append)
    page.weapon_edit_requested.connect(weapons.append)
    page.team_edit_requested.connect(teams.append)
    page.echo_slot_edit_requested.connect(
        lambda character_id, position: echoes.append((character_id, position))
    )
    page.apply_character(_detail())
    page.show()
    qapp.processEvents()

    assert page.detail_stack.currentIndex() == 1
    assert page.name_edit.text() == "緋雪"
    assert page.element_combo.currentText() == "冷凝"
    assert page.level_spin.value() == 90
    assert page.team_label.text() == "緋雪隊"
    assert page.skill_model.rowCount() == 5
    assert page.attribute_node_checks[7].isChecked()
    assert not page.attribute_node_checks[8].isChecked()
    assert page.echo_cards[0].name_label.text() == "不熄獵手"
    assert "76.5" in page.echo_cards[0].score_label.text()
    assert page.echo_cards[0].set_validity_combo.currentData() is True
    assert page.echo_cards[0].main_validity_combo.currentData() is True
    assert "角色 Lv.90" in page.progress_label.text()
    assert page.passing_echoes_label.text() == "1 / 5"
    assert page.weakest_position_label.text() == "位置 3"
    assert "第三顆" in page.next_suggestion_label.text()

    page.name_edit.setText("緋雪・完成")
    page.weapon_rank_spin.setValue(2)
    page.skill_model.setData(page.skill_model.index(0, 1), "7")
    page.echo_cards[0].main_validity_combo.setCurrentIndex(2)
    QTest.mouseClick(page.save_button, Qt.MouseButton.LeftButton)
    QTest.mouseClick(page.archive_button, Qt.MouseButton.LeftButton)
    QTest.mouseClick(page.weapon_edit_button, Qt.MouseButton.LeftButton)
    page.focus_weapon_editor()
    assert "可直接編輯武器" in page.status_label.text()
    QTest.mouseClick(page.team_edit_button, Qt.MouseButton.LeftButton)
    QTest.mouseClick(page.echo_cards[2].edit_button, Qt.MouseButton.LeftButton)

    assert edits[0].identifier == "character-1"
    assert edits[0].name == "緋雪・完成"
    assert edits[0].weapon.resonance_rank == 2
    assert edits[0].skills[0].current_level == 7
    assert edits[0].echo_validations[0] == (True, False)
    assert archives == ["character-1"]
    assert weapons == ["character-1"]
    assert teams == ["character-1"]
    assert echoes == [("character-1", 3)]
    page.close()


def test_loading_error_empty_and_refresh_states_are_presenter_friendly(
    qapp: QApplication,
) -> None:
    page = CharactersPage()
    refreshes: list[bool] = []
    page.refresh_requested.connect(lambda: refreshes.append(True))
    page.show()
    qapp.processEvents()

    QTest.mouseClick(page.refresh_button, Qt.MouseButton.LeftButton)
    assert refreshes == [True]
    assert not page.refresh_button.isEnabled()
    assert not page.add_button.isEnabled()
    assert "載入" in page.status_label.text()

    page.show_error("資料庫暫時無法使用")
    assert page.status_label.property("error") is True
    assert "資料庫暫時無法使用" in page.status_label.text()
    assert page.refresh_button.isEnabled()
    assert page.add_button.isEnabled()

    page.set_characters(())
    assert page.detail_stack.currentIndex() == 0
    assert "掃描已裝備的聲骸" in page.empty_state_label.text()
    assert page.status_label.text() == "已載入 0 名角色。"
    page.close()


def test_character_dtos_reject_incomplete_fixed_shape_data(qapp: QApplication) -> None:
    with pytest.raises(ValueError, match="five skill"):
        CharacterDetailView(
            "character-1",
            "角色",
            "氣動",
            "輸出",
            True,
            1,
            90,
            0,
            None,
            None,
            WeaponProgressView(),
            None,
            None,
            (),
            (False,) * 10,
            _echo_slots(),
        )
    with pytest.raises(ValueError, match="position"):
        PermanentEchoSlotView(6)
    with pytest.raises(ValueError, match="target skill"):
        SkillProgressView("skill", "技能", 0, 0, False)

    page = CharactersPage()
    page.skill_model.set_rows(_skills())
    target_index = page.skill_model.index(0, 2)
    current_index = page.skill_model.index(0, 1)
    assert not page.skill_model.setData(target_index, 0)
    assert page.skill_model.data(target_index) == 6
    assert page.skill_model.setData(target_index, 1)
    assert not page.skill_model.setData(current_index, -1)
    assert page.skill_model.setData(current_index, 0)
    page.close()
    qapp.processEvents()


def test_cultivation_defaults_to_compact_view_with_direct_skill_and_node_edits(
    qapp: QApplication,
) -> None:
    page = CharactersPage()
    page.resize(1100, 680)
    page.set_characters(_summaries())
    page.select_character("character-1")
    page.apply_character(_detail())
    page.show()
    qapp.processEvents()
    assert page.edit_settings.isHidden()
    assert not page.level_spin.isVisible()
    assert not page.weapon_name_edit.isVisible()
    assert all(
        action.isHidden() for action in (page.add_button, page.catalog_button, page.refresh_button)
    )
    assert page.skill_table.isVisible()
    assert page.attribute_node_checks[0].isVisible()
    # The numeric panel adds a section; the compact editor remains vertically scrollable.
    assert page.panel_stat_labels["crit_rate"].isVisible()
    assert page.detail_scroll.horizontalScrollBar().maximum() == 0
    assert page.skill_model.setData(page.skill_model.index(1, 1), 9)
    page.attribute_node_checks[-1].setChecked(True)
    assert page.build_edit_request().skills[1].current_level == 9
    assert page.build_edit_request().attribute_nodes[-1]
    page.edit_mode_button.click()
    qapp.processEvents()
    assert page.level_spin.isVisible()
    page.weapon_name_edit.setText("尚未儲存的武器")
    page.edit_mode_button.click()
    assert page.edit_settings.isHidden()
    assert page.build_edit_request().weapon.name == "尚未儲存的武器"
    assert "尚未儲存的武器" in page.character_equipment_label.text()
    page.close()


def test_portrait_rail_expands_to_three_columns_and_preserves_selection_and_draft(
    qapp: QApplication,
) -> None:
    page = CharactersPage()
    page.resize(1100, 680)
    rows = tuple(replace(_summaries()[0], identifier=f"character-{i}") for i in range(20))
    page.set_characters(rows)
    page.select_character("character-1")
    page.apply_character(_detail())
    page.weapon_name_edit.setText("未儲存")
    page.show()
    qapp.processEvents()
    rail = page.character_table
    rects = [rail.visualRect(page.character_model.index(i, 0)) for i in range(4)]
    assert len({rect.x() for rect in rects}) == 1
    assert rail.verticalScrollBar().maximum() > 0
    page.roster_expand_button.click()
    qapp.processEvents()
    rects = [rail.visualRect(page.character_model.index(i, 0)) for i in range(4)]
    assert len({rect.x() for rect in rects[:3]}) == 3
    assert len({rect.y() for rect in rects[:3]}) == 1
    assert rects[3].x() == rects[0].x() and rects[3].y() > rects[0].y()
    assert rail.horizontalScrollBar().maximum() == 0
    assert page.current_character_id() == "character-1"
    assert page.weapon_name_edit.text() == "未儲存"
    page.close()


def test_numeric_wheel_events_never_modify_character_or_skill_progress(
    qapp: QApplication,
) -> None:
    page = CharactersPage()
    page.apply_character(_detail())
    page.edit_mode_button.setChecked(True)
    page.show()
    qapp.processEvents()
    editor = page.skill_table.itemDelegate().createEditor(
        page.skill_table, QStyleOptionViewItem(), page.skill_model.index(0, 1)
    )
    assert editor is not None
    editors = (
        page.level_spin,
        page.chain_spin,
        page.weapon_level_spin,
        page.weapon_rank_spin,
        page.panel_energy_spin,
        page.energy_target_spin,
        editor,
    )
    for spin in editors:
        spin.setProperty("value", 3)
        spin.setFocus()
        qapp.processEvents()
        event = QWheelEvent(
            QPointF(5, 5),
            QPointF(5, 5),
            QPoint(),
            QPoint(0, 120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        qapp.sendEvent(spin, event)
        assert spin.property("value") == 3
    page.close()


@pytest.mark.parametrize("size", ((1100, 720), (1200, 800), (1440, 900)))
def test_cultivation_fits_real_shell_with_expanded_rail_and_rare_edit_fields(
    qapp: QApplication, size: tuple[int, int]
) -> None:
    window = MainWindow(MemorySettings())
    window.resize(*size)
    page = window.characters_page
    page.set_characters(_summaries())
    page.apply_character(_detail())
    page.set_scoring_profiles((ChoiceOptionView("profile-1", "較長的自動角色攻略評分設定檔"),))
    window.navigate_to(PageId.CHARACTERS)
    window.show()
    for expanded in (False, True, False):
        page.roster_expand_button.setChecked(expanded)
        for editing in (False, True):
            page.edit_mode_button.setChecked(editing)
            page.echo_management_toggle.setChecked(editing)
            for _ in range(3):
                qapp.processEvents()
            assert page.detail_scroll.horizontalScrollBar().maximum() == 0
            assert page.master_panel.geometry().right() == page.splitter.width() - 1
            assert page.skill_table.horizontalScrollBar().maximum() == 0
            assert page.skill_table.verticalScrollBar().maximum() == 0
            last = page.skill_table.visualRect(page.skill_model.index(4, 3))
            assert page.skill_table.viewport().rect().contains(last)
    window.close()


@pytest.mark.parametrize("size", ((1440, 900), (2048, 1089)))
def test_wide_character_page_shows_frequent_content_without_scrolling(
    qapp: QApplication, size: tuple[int, int]
) -> None:
    window = MainWindow(MemorySettings())
    window.resize(*size)
    page = window.characters_page
    page.set_characters(_summaries())
    page.apply_character(_detail())
    page.panel_status_label.setText(
        "已與登記的遊戲面板一致。聲骸紀錄更新時自動重算;角色或武器編輯後請儲存。"
    )
    for key, value in {
        "hp": "生命值　18240",
        "attack": "攻擊力　2310",
        "defense": "防禦力　1290",
        "crit_rate": "暴擊　72.5%",
        "crit_damage": "暴擊傷害　297.0%",
        "energy_regen": "共鳴效率　146.0%",
    }.items():
        page.panel_stat_labels[key].setText(value)
    page.equipment_check_label.setText(
        "最近核對 2/5　1 詞條相符・2 有差異・3 待確認・4 詞條相符・5 待重讀"
    )
    window.navigate_to(PageId.CHARACTERS)
    window.show()
    for _ in range(5):
        qapp.processEvents()
    viewport = page.detail_scroll.viewport()

    def inside(widget: QWidget) -> bool:
        return viewport.rect().contains(widget.mapTo(viewport, widget.rect().topLeft())) and (
            viewport.rect().contains(widget.mapTo(viewport, widget.rect().bottomRight()))
        )

    assert page.detail_scroll.verticalScrollBar().maximum() == 0
    for widget in (
        page.character_portrait,
        *page.panel_stat_labels.values(),
        page.panel_details_button,
        page.equipment_check_button,
        page.equipment_check_details,
        page.skill_table,
        *page.attribute_node_checks,
        *(button for _, _, button in page.equipment_summary_slots.values()),
    ):
        assert inside(widget), widget.objectName()

    # Reflow must preserve edits and keep all actions reachable on smaller windows.
    page.weapon_name_edit.setText("未儲存武器")
    page.skill_model.setData(page.skill_model.index(0, 1), 7)
    page.attribute_node_checks[9].setChecked(True)
    for width in (1100, 1280, size[0]):
        window.resize(width, size[1])
        for _ in range(3):
            qapp.processEvents()
        assert page.detail_scroll.horizontalScrollBar().maximum() == 0
        assert page.skill_table.verticalScrollBar().maximum() == 0
        assert page.weapon_name_edit.text() == "未儲存武器"
        assert page.skill_model.rows()[0].current_level == 7
        assert page.attribute_node_checks[9].isChecked()
    window.close()
