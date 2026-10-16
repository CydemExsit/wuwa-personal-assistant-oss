from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from wuwa_assistant.application.characters import (
    CharacterCultivationService,
    CreateCharacter,
)
from wuwa_assistant.application.scoring_profiles import ScoringProfileService
from wuwa_assistant.scoring import load_template_seed_definitions
from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.storage.orm import DataSetRecord, EchoRecord
from wuwa_assistant.ui.controllers.characters import (
    CharactersPresenter,
    _DialogJobResult,
    _DialogOperation,
)
from wuwa_assistant.ui.dialogs import (
    EchoSlotAssignmentDialog,
    EchoSlotAssignmentRequest,
    TeamEditorDialog,
    TeamSaveRequest,
)
from wuwa_assistant.ui.pages.characters import CharactersPage


def test_team_and_echo_dialog_intents_persist_through_presenter(
    qapp: QApplication,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    del qapp
    database = create_database(AppPaths.from_root(tmp_path / "character-links-presenter"))
    page = CharactersPage()
    try:
        with database.session_factory.begin() as session:
            data_set = DataSetRecord(name="主要資料", kind="primary", is_active=True)
            session.add(data_set)
            session.flush()
            data_set_id = data_set.id
        service = CharacterCultivationService(database.session_factory)
        character = service.create_character(
            data_set_id=data_set_id,
            command=CreateCharacter("角色一"),
        )
        other = service.create_character(
            data_set_id=data_set_id,
            command=CreateCharacter("角色二"),
        )
        with database.session_factory.begin() as session:
            echo = EchoRecord(
                data_set_id=data_set_id,
                name="測試聲骸",
                set_name="凝夜白霜",
                cost=1,
                level=25,
                score=82.0,
                main_stats_json=[{"name": "暴擊", "value": 22.0, "unit": "%"}],
            )
            session.add(echo)
            replacement = EchoRecord(
                data_set_id=data_set_id,
                name="替換聲骸",
                set_name="凝夜白霜",
                cost=1,
                level=25,
                score=84.0,
                main_stats_json=[{"name": "暴擊", "value": 22.0, "unit": "%"}],
            )
            session.add(replacement)
            session.flush()
            echo_id = echo.id
            replacement_id = replacement.id
        profiles = ScoringProfileService(database.session_factory)
        profiles.ensure_system_templates(load_template_seed_definitions())
        presenter = CharactersPresenter(
            service=service,
            scoring_profiles=profiles,
            page=page,
            data_set_id=data_set_id,
        )
        presenter.initialize()
        qtbot.waitUntil(lambda: page.character_model.rowCount() == 2)

        page.team_edit_requested.emit(character.summary.id)
        qtbot.waitUntil(lambda: page.findChild(TeamEditorDialog) is not None)
        team_dialog = page.findChild(TeamEditorDialog)
        assert team_dialog is not None
        qtbot.waitUntil(lambda: team_dialog.create_button.isEnabled())
        team_dialog.save_requested.emit(
            TeamSaveRequest(
                None,
                "日常隊伍",
                1,
                "本機手動設定",
                (character.summary.id, other.summary.id, None),
            )
        )
        qtbot.waitUntil(lambda: len(service.list_teams(data_set_id=data_set_id)) == 1)
        qtbot.waitUntil(lambda: team_dialog.team_list.count() == 1)

        page.echo_slot_edit_requested.emit(character.summary.id, 1)
        qtbot.waitUntil(lambda: page.findChild(EchoSlotAssignmentDialog) is not None)
        echo_dialog = page.findChild(EchoSlotAssignmentDialog)
        assert echo_dialog is not None
        qtbot.waitUntil(lambda: echo_dialog.echo_combo.count() >= 2)
        echo_dialog.save_requested.emit(
            EchoSlotAssignmentRequest(character.summary.id, 1, echo_id, False)
        )
        qtbot.waitUntil(
            lambda: (
                service.get_character(
                    character.summary.id,
                    data_set_id=data_set_id,
                )
                .echo_slots[0]
                .echo_id
                == echo_id
            )
        )
        qtbot.waitUntil(lambda: page.current_character_id() == character.summary.id)
        qtbot.waitUntil(lambda: page.echo_cards[0].name_label.text() == "測試聲骸")
        page.echo_cards[0].set_validity_combo.setCurrentIndex(
            page.echo_cards[0].set_validity_combo.findData(True)
        )
        page.echo_cards[0].main_validity_combo.setCurrentIndex(
            page.echo_cards[0].main_validity_combo.findData(True)
        )
        page.edit_requested.emit(page.build_edit_request())

        def confirmation_is_bound_to_echo() -> bool:
            goal = service.get_character(
                character.summary.id,
                data_set_id=data_set_id,
            ).cultivation_goal
            confirmations = goal.get("echo_slot_confirmations")
            if not isinstance(confirmations, dict):
                return False
            slot_confirmation = confirmations.get("1")
            return isinstance(slot_confirmation, dict) and (
                slot_confirmation.get("echo_id") == echo_id
            )

        qtbot.waitUntil(confirmation_is_bound_to_echo)

        page.echo_slot_edit_requested.emit(character.summary.id, 1)
        qtbot.waitUntil(lambda: echo_dialog.target_label.text() == "角色一 / 位置 1")
        echo_dialog.save_requested.emit(
            EchoSlotAssignmentRequest(character.summary.id, 1, replacement_id, True)
        )
        qtbot.waitUntil(
            lambda: (
                service.get_character(
                    character.summary.id,
                    data_set_id=data_set_id,
                )
                .echo_slots[0]
                .echo_id
                == replacement_id
            )
        )
        qtbot.waitUntil(lambda: page.echo_cards[0].name_label.text() == "替換聲骸")
        assert page.echo_cards[0].set_validity_combo.currentData() is None
        assert page.echo_cards[0].main_validity_combo.currentData() is None

        page.echo_slot_edit_requested.emit(character.summary.id, 1)
        page.echo_slot_edit_requested.emit(character.summary.id, 2)
        qtbot.waitUntil(lambda: echo_dialog.target_label.text() == "角色一 / 位置 2")
        assert presenter._active_echo_context is not None
        generation = presenter._active_echo_context[0]
        status_before = echo_dialog.status_label.text()
        presenter._handle_dialog_job_result(
            _DialogJobResult(
                _DialogOperation.ECHO_LOAD,
                error="stale load error",
                generation=generation - 1,
            )
        )
        presenter._handle_dialog_job_result(
            _DialogJobResult(
                _DialogOperation.ECHO_SAVE,
                value=(character.summary.id, 1),
                generation=generation - 1,
            )
        )
        assert echo_dialog.target_label.text() == "角色一 / 位置 2"
        assert echo_dialog.status_label.text() == status_before
        assert presenter.shutdown()
    finally:
        page.close()
        database.dispose()
