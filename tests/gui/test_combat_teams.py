from dataclasses import replace

import pytest
from PySide6.QtCore import Qt

from tests.application.test_character_stats import panel_fixture
from tests.gui.test_characters_presenter import character_workspace as workspace_fixture
from wuwa_assistant.application.combat_build import CombatTeam, team_to_json, teams_from_goal
from wuwa_assistant.ui.dialogs.combat_teams import CombatTeamsDialog


@pytest.fixture
def character_workspace(tmp_path):
    yield from workspace_fixture.__wrapped__(tmp_path)


def test_two_teams_primary_modes_and_shared_teammate_are_editable_without_long_prose(qtbot):
    detail = panel_fixture()
    teammate = replace(detail, summary=replace(detail.summary, id="support", name="琳奈"))
    teams = [
        CombatTeam("a", "主隊", ("support",), 120, confirmed=True, outro_id="support"),
        CombatTeam("b", "次隊", ("support",), 130, confirmed=True),
    ]
    detail = replace(
        detail,
        cultivation_goal={
            **detail.cultivation_goal,
            "combat_teams": [team_to_json(t) for t in teams],
            "combat_primary_team": "a",
        },
    )
    dialog = CombatTeamsDialog(detail, {detail.summary.id: detail, "support": teammate})
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.team_list.count() == 2
    assert dialog.primary_check.isChecked()
    assert dialog.effects.topLevelItemCount() > 0
    assert len(dialog.status.text()) < 100
    dialog.team_list.setCurrentRow(1)
    dialog.primary_check.setChecked(True)
    dialog.energy_spin.setValue(135)
    with qtbot.waitSignal(dialog.save_requested, timeout=1000) as emitted:
        dialog.save_button.click()
    settings = emitted.args[0]
    assert settings.primary_id == "b"
    assert settings.teams[1].energy_target == 135
    assert settings.teams[0].member_ids == settings.teams[1].member_ids == ("support",)
    assert (
        teams_from_goal({"combat_teams": [team_to_json(t) for t in settings.teams]})
        == settings.teams
    )


def test_effect_exclusion_and_empty_settings_do_not_mutate_character_draft(qtbot):
    detail = panel_fixture()
    dialog = CombatTeamsDialog(detail, {detail.summary.id: detail})
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.team_list.count() == 1
    assert not dialog.confirm_check.isChecked()
    item = dialog.effects.topLevelItem(0)
    assert item is not None
    item.setCheckState(0, Qt.CheckState.Unchecked)
    assert dialog._read().excluded_effects
    dialog._remove()
    with qtbot.waitSignal(dialog.save_requested, timeout=1000) as emitted:
        dialog.save_button.click()
    assert emitted.args[0].teams == ()
    assert "combat_teams" not in detail.cultivation_goal


def test_presenter_persists_combat_teams_and_retains_unsaved_character_fields(
    qtbot,
    request,
    character_workspace,
):
    from wuwa_assistant.application.characters import CreateCharacter
    from wuwa_assistant.ui.controllers.characters import CharactersPresenter

    _, service, profiles, data_set_id, page = character_workspace
    owner = service.create_character(data_set_id=data_set_id, command=CreateCharacter("愛彌斯"))
    teammate = service.create_character(data_set_id=data_set_id, command=CreateCharacter("千咲"))
    presenter = CharactersPresenter(
        service=service, scoring_profiles=profiles, page=page, data_set_id=data_set_id
    )
    request.addfinalizer(presenter.shutdown)
    presenter.initialize()
    qtbot.waitUntil(lambda: page.current_character_id() is not None, timeout=30000)
    presenter.select_character(owner.summary.id)
    page.notes_edit.setPlainText("尚未儲存的角色筆記")
    presenter.open_combat_editor(owner.summary.id)
    dialog = presenter._combat_dialog
    assert dialog is not None
    dialog.member_combos[0].setCurrentIndex(dialog.member_combos[0].findData(teammate.summary.id))
    dialog.confirm_check.setChecked(True)
    dialog.save_button.click()
    qtbot.waitUntil(lambda: not dialog.isVisible(), timeout=30000)
    saved = service.get_character(owner.summary.id, data_set_id=data_set_id)
    assert len(teams_from_goal(saved.cultivation_goal)) == 1
    assert page.notes_edit.toPlainText() == "尚未儲存的角色筆記"
