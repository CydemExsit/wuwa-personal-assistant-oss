from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from wuwa_assistant.ui.dialogs.character_links import (
    CharacterChoiceView,
    EchoAssignmentChoiceView,
    EchoSlotAssignmentDialog,
    EchoSlotAssignmentRequest,
    EchoSlotContextView,
    TeamDetailView,
    TeamEditorDialog,
    TeamSaveRequest,
    TeamSummaryView,
)


def _characters() -> tuple[CharacterChoiceView, ...]:
    return (
        CharacterChoiceView("character-1", "緋雪", "主輸出 / 熱熔"),
        CharacterChoiceView("character-2", "守岸人", "治療 / 衍射"),
        CharacterChoiceView("character-3", "散華", "副輸出 / 冷凝"),
        CharacterChoiceView("character-4", "維里奈", "治療 / 衍射"),
    )


def _echoes() -> tuple[EchoAssignmentChoiceView, ...]:
    return (
        EchoAssignmentChoiceView(
            "echo-1", "不熄獵手", "羽落空塵之歌", 1, 15, 76.5, "character-1", "緋雪", 1
        ),
        EchoAssignmentChoiceView(
            "echo-2", "鳴泣戰士", "輕雲出月", 3, 25, 91.25, "character-2", "守岸人", 3
        ),
        EchoAssignmentChoiceView("echo-3", "無妄者", "沉日劫明", 4, 25, None),
    )


def test_team_dialog_lists_selects_and_emits_an_immutable_update_request(
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialog = TeamEditorDialog()
    selections: list[str] = []
    saves: list[TeamSaveRequest] = []
    deletes: list[str] = []
    dialog.team_selected.connect(selections.append)
    dialog.save_requested.connect(saves.append)
    dialog.delete_requested.connect(deletes.append)
    dialog.set_character_choices(_characters())
    dialog.set_teams(
        (
            TeamSummaryView("team-1", "緋雪隊", 1, ("緋雪", "守岸人", None)),
            TeamSummaryView("team-2", "冷凝隊", 2, ("散華", None, None)),
        )
    )
    dialog.show()
    qapp.processEvents()

    dialog.team_list.setCurrentRow(0)
    assert selections == ["team-1"]
    assert "緋雪隊" in dialog.team_list.item(0).text()
    assert "空位" in dialog.team_list.item(0).text()

    dialog.apply_team(
        TeamDetailView("team-1", "緋雪隊", 1, "主力隊", ("character-1", "character-2", None))
    )
    assert dialog.name_edit.text() == "緋雪隊"
    assert dialog.member_combos[0].currentData() == "character-1"
    assert dialog.member_combos[2].currentData() is None
    assert dialog.delete_button.isEnabled()

    dialog.name_edit.setText("緋雪完成隊")
    dialog.priority_spin.setValue(3)
    dialog.member_combos[2].setCurrentIndex(dialog.member_combos[2].findData("character-3"))
    QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.No,
    )
    QTest.mouseClick(dialog.delete_button, Qt.MouseButton.LeftButton)
    assert deletes == []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    QTest.mouseClick(dialog.delete_button, Qt.MouseButton.LeftButton)

    assert saves == [
        TeamSaveRequest(
            "team-1",
            "緋雪完成隊",
            3,
            "主力隊",
            ("character-1", "character-2", "character-3"),
        )
    ]
    assert deletes == ["team-1"]
    dialog.close()


def test_team_dialog_create_allows_empty_slots_and_rejects_duplicate_members(
    qapp: QApplication,
) -> None:
    dialog = TeamEditorDialog()
    creates: list[bool] = []
    saves: list[TeamSaveRequest] = []
    dialog.create_started.connect(lambda: creates.append(True))
    dialog.save_requested.connect(saves.append)
    dialog.set_character_choices(_characters())
    dialog.show()
    qapp.processEvents()

    QTest.mouseClick(dialog.create_button, Qt.MouseButton.LeftButton)
    assert creates == [True]
    assert dialog.current_team_id() is None
    assert not dialog.delete_button.isEnabled()
    assert all(combo.currentData() is None for combo in dialog.member_combos)

    dialog.name_edit.setText("新隊伍")
    first_index = dialog.member_combos[0].findData("character-1")
    dialog.member_combos[0].setCurrentIndex(first_index)
    dialog.member_combos[1].setCurrentIndex(dialog.member_combos[1].findData("character-1"))
    QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
    assert saves == []
    assert dialog.status_label.property("error") is True
    assert "不可重複" in dialog.status_label.text()

    dialog.member_combos[1].setCurrentIndex(0)
    QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
    assert saves[-1].identifier is None
    assert saves[-1].member_ids == ("character-1", None, None)
    dialog.close()


def test_echo_dialog_shows_full_choice_context_and_requires_explicit_replace(
    qapp: QApplication,
) -> None:
    dialog = EchoSlotAssignmentDialog()
    saves: list[EchoSlotAssignmentRequest] = []
    dialog.save_requested.connect(saves.append)
    dialog.set_echo_choices(_echoes())
    dialog.set_context(EchoSlotContextView("character-1", "緋雪", 1, "echo-1", "不熄獵手"))
    dialog.show()
    qapp.processEvents()

    assert dialog.target_label.text() == "緋雪 / 位置 1"
    assert "不熄獵手" in dialog.current_label.text()
    echo_two_index = dialog.echo_combo.findData("echo-2")
    assert "輕雲出月" in dialog.echo_combo.itemText(echo_two_index)
    assert "COST 3" in dialog.echo_combo.itemText(echo_two_index)
    assert "+25" in dialog.echo_combo.itemText(echo_two_index)
    assert "91.2 分" in dialog.echo_combo.itemText(echo_two_index)
    assert "守岸人 / 位置 3" in dialog.echo_combo.itemText(echo_two_index)

    dialog.echo_combo.setCurrentIndex(echo_two_index)
    assert dialog.replace_check.isVisible()
    assert "目前位置已有其他聲骸" in dialog.replacement_reason_label.text()
    assert "守岸人" in dialog.replacement_reason_label.text()
    assert not dialog.save_button.isEnabled()

    dialog.replace_check.setChecked(True)
    assert dialog.save_button.isEnabled()
    QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
    assert saves == [EchoSlotAssignmentRequest("character-1", 1, "echo-2", True)]
    dialog.close()


def test_echo_dialog_unassign_is_available_and_also_requires_confirmation(
    qapp: QApplication,
) -> None:
    dialog = EchoSlotAssignmentDialog()
    saves: list[EchoSlotAssignmentRequest] = []
    dialog.save_requested.connect(saves.append)
    dialog.set_echo_choices(_echoes())
    dialog.set_context(EchoSlotContextView("character-1", "緋雪", 1, "echo-1"))
    dialog.show()
    qapp.processEvents()

    dialog.echo_combo.setCurrentIndex(0)
    assert dialog.selected_echo_id() is None
    assert dialog.replace_check.isVisible()
    assert "解除" in dialog.replacement_reason_label.text()
    assert not dialog.save_button.isEnabled()
    dialog.replace_check.setChecked(True)
    QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)

    assert saves[0].is_unassign is True
    assert saves[0].replace_confirmed is True
    dialog.close()


def test_echo_dialog_assigns_unoccupied_echo_without_replace_confirmation(
    qapp: QApplication,
) -> None:
    dialog = EchoSlotAssignmentDialog()
    saves: list[EchoSlotAssignmentRequest] = []
    dialog.save_requested.connect(saves.append)
    dialog.set_echo_choices(_echoes())
    dialog.set_context(EchoSlotContextView("character-3", "散華", 2))
    dialog.set_selected_echo("echo-3")
    dialog.show()
    qapp.processEvents()

    assert not dialog.replace_check.isVisible()
    assert dialog.save_button.isEnabled()
    QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
    assert saves == [EchoSlotAssignmentRequest("character-3", 2, "echo-3", False)]
    dialog.close()


def test_dialog_dtos_reject_invalid_fixed_shapes_and_assignments(qapp: QApplication) -> None:
    del qapp
    with pytest.raises(ValueError, match="three member labels"):
        TeamSummaryView("team-1", "隊伍", 1, ("一", "二"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="multiple team slots"):
        TeamSaveRequest("team-1", "隊伍", 1, "", ("character-1", "character-1", None))
    linked_without_slot = EchoAssignmentChoiceView(
        "echo-1", "聲骸", None, 1, 0, None, "character-1", "角色", None
    )
    assert linked_without_slot.assignment_text == "已關聯: 角色 / 未指定位置"
    with pytest.raises(ValueError, match=r"within 1\.\.5"):
        EchoSlotContextView("character-1", "角色", 6)
